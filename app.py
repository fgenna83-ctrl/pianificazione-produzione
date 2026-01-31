import streamlit as st
from datetime import date, timedelta
import json
import os
import hashlib

FILE_DATI = "dati_produzione.json"

# Capacità giornaliera fissa per materiale (minuti/giorno)
CAPACITA_MINUTI_GIORNALIERA = {
    "PVC": 4500,
    "Alluminio": 3000
}

MINUTI_8_ORE = 8 * 60  # 480


# =========================
# LOGIN (Streamlit Secrets)
# =========================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def check_login() -> bool:
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    if st.session_state.logged_in:
        return True

    st.title("🔐 Login")
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    if st.button("Accedi"):
        try:
            user_ok = st.secrets["auth"]["username"]
            pass_hash_ok = st.secrets["auth"]["password_hash"]
        except Exception:
            st.error("Credenziali non configurate in Streamlit Secrets.")
            return False

        if username == user_ok and hash_password(password) == pass_hash_ok:
            st.session_state.logged_in = True
            st.success("Accesso effettuato")
            st.rerun()
        else:
            st.error("Username o password errati")

    return False


# =========================
# STORAGE
# =========================
def carica_dati():
    if os.path.exists(FILE_DATI):
        with open(FILE_DATI, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"capacita_giornaliera": 0, "ordini": []}


def salva_dati(dati):
    with open(FILE_DATI, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=2)


# =========================
# TEMPI PRODUZIONE (vetri TOTALI per riga sui battenti)
# =========================
def tempo_riga(materiale: str, tipologia: str, quantita_strutture: int, vetri_totali: int) -> int:
    """
    Battente:
      - PVC: 90 min per vetro (vetri_totali)
      - Alluminio: 90 min per vetro (vetri_totali) + 30 min per struttura (quantita_strutture)
    Scorrevole e Struttura speciale:
      - 480 min per struttura (quantita_strutture) indipendente dal materiale
    """
    tipologia = tipologia.strip()

    if tipologia == "Battente":
        tempo = int(vetri_totali) * 90
        if materiale == "Alluminio":
            tempo += int(quantita_strutture) * 30
        return tempo

    # Scorrevole o Struttura speciale
    return int(quantita_strutture) * MINUTI_8_ORE


def minuti_preview(materiale: str, tipologia: str, quantita_strutture: int, vetri_totali: int) -> tuple[int, int]:
    """
    Per mostrare anteprima in UI: (minuti_totali_riga, minuti_medi_per_struttura)
    """
    tot = tempo_riga(materiale, tipologia, quantita_strutture, vetri_totali)
    if quantita_strutture > 0:
        return tot, int(round(tot / quantita_strutture))
    return tot, tot


# =========================
# PIANIFICAZIONE (spezzata su più giorni)
# =========================
def calcola_piano(dati):
    ordini = dati.get("ordini", [])
    if not ordini:
        return [], []

    oggi = date.today()

    # separo le righe per materiale e le ordino per data richiesta (poi gruppo, poi id)
    def safe_date(s):
        try:
            return date.fromisoformat(str(s))
        except Exception:
            return oggi

    pvc = [o for o in ordini if o.get("materiale", "PVC") == "PVC"]
    allu = [o for o in ordini if o.get("materiale", "PVC") == "Alluminio"]

    pvc.sort(key=lambda o: (safe_date(o.get("data_richiesta")), str(o.get("ordine_gruppo")), int(o.get("id", 0))))
    allu.sort(key=lambda o: (safe_date(o.get("data_richiesta")), str(o.get("ordine_gruppo")), int(o.get("id", 0))))

    # stato per materiale
    stato = {
        "PVC": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["PVC"]},
        "Alluminio": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["Alluminio"]},
    }

    piano = []
    consegna_per_gruppo = {}  # gruppo -> info consegna unica
    # mantengo i minuti rimanenti per ogni riga (per spezzare su più giorni)
    remaining_map = {}

    def init_remaining(o):
        oid = int(o.get("id", 0))
        if oid not in remaining_map:
            t = int(o.get("tempo_minuti", 0))
            remaining_map[oid] = max(0, t)
        return remaining_map[oid]

    def update_gruppo(o, giorno_fine):
        gruppo = o.get("ordine_gruppo", "")
        if gruppo == "":
            gruppo = "0"

        if gruppo not in consegna_per_gruppo:
            consegna_per_gruppo[gruppo] = {
                "Gruppo": gruppo,
                "Cliente": o.get("cliente", ""),
                "Prodotto": o.get("prodotto", ""),
                "Richiesta": o.get("data_richiesta", ""),
                "Tempo_totale_minuti": 0,
                "Stimata": str(giorno_fine),
            }

        # sommo minuti totali dell'ordine (gruppo)
        try:
            consegna_per_gruppo[gruppo]["Tempo_totale_minuti"] += int(o.get("tempo_minuti", 0))
        except Exception:
            pass

        # consegna unica = massimo tra le righe
        try:
            old = date.fromisoformat(consegna_per_gruppo[gruppo]["Stimata"])
            new = date.fromisoformat(str(giorno_fine))
            if new > old:
                consegna_per_gruppo[gruppo]["Stimata"] = str(new)
        except Exception:
            consegna_per_gruppo[gruppo]["Stimata"] = str(giorno_fine)

        # richiesta: teniamo la più “vicina” (minima)
        try:
            rold = date.fromisoformat(consegna_per_gruppo[gruppo]["Richiesta"])
            rnew = safe_date(o.get("data_richiesta"))
            if rnew < rold:
                consegna_per_gruppo[gruppo]["Richiesta"] = str(rnew)
        except Exception:
            pass

    def pianifica_materiale(lista, materiale):
        cap = stato[materiale]["cap"]
        giorno = stato[materiale]["giorno"]
        usati = stato[materiale]["usati"]

        i = 0
        while i < len(lista):
            o = lista[i]
            oid = int(o.get("id", 0))
            rem = init_remaining(o)

            if rem <= 0:
                i += 1
                continue

            disponibili = cap - usati
            if disponibili <= 0:
                # giorno pieno: passo al giorno dopo
                giorno = giorno + timedelta(days=1)
                usati = 0
                continue

            prodotti_oggi = min(disponibili, rem)
            usati += prodotti_oggi
            rem -= prodotti_oggi
            remaining_map[oid] = rem

            # stima strutture prodotte oggi (proporzionale ai minuti)
            qta_strutture = int(o.get("quantita_strutture", 0) or 0)
            tempo_totale = int(o.get("tempo_minuti", 0) or 0)
            strutture_oggi = 0.0
            if tempo_totale > 0 and qta_strutture > 0:
                strutture_oggi = (prodotti_oggi / tempo_totale) * qta_strutture

            piano.append({
                "Data": str(giorno),
                "Ordine": o.get("id", ""),
                "Gruppo": o.get("ordine_gruppo", ""),
                "Cliente": o.get("cliente", ""),
                "Prodotto": o.get("prodotto", ""),
                "Materiale": materiale,
                "Tipologia": o.get("tipologia", ""),
                "Qta_strutture": qta_strutture,
                "Vetri_totali": o.get("vetri_totali", ""),
                "Minuti_prodotti": int(prodotti_oggi),
                "Minuti_residui_materiale": int(cap - usati),
                "Strutture_prodotte": round(strutture_oggi, 2),
            })

            # se ho finito questa riga, aggiorno consegna riga + consegna ordine
            if rem <= 0:
                o["consegna_stimata"] = str(giorno)
                update_gruppo(o, giorno)
                i += 1  # passo alla prossima riga
            else:
                # non finita: se ho riempito il giorno, domani continuo la stessa riga
                if usati >= cap:
                    giorno = giorno + timedelta(days=1)
                    usati = 0

        # salvo stato finale
        stato[materiale]["giorno"] = giorno
        stato[materiale]["usati"] = usati

    # Pianifico PVC e Alluminio separatamente (ottimizza residui dentro ogni materiale)
    pianifica_materiale(pvc, "PVC")
    pianifica_materiale(allu, "Alluminio")

    salva_dati(dati)

    consegne_ordini = list(consegna_per_gruppo.values())
    # ordino consegne per gruppo (numero se possibile)
    def grp_key(x):
        try:
            return int(x.get("Gruppo", 0))
        except Exception:
            return 0
    consegne_ordini.sort(key=grp_key)

    return consegne_ordini, piano




# =========================
# APP
# =========================
st.set_page_config(page_title="Planner Produzione", layout="wide")

if not check_login():
    st.stop()

st.title("📦 Planner Produzione (Online)")

dati = carica_dati()

if "righe_correnti" not in st.session_state:
    st.session_state["righe_correnti"] = []

col1, col2 = st.columns(2)

with col1:
    st.subheader("⚙️ Capacità giornaliera (minuti/giorno)")
    st.info(
        f"• PVC: {CAPACITA_MINUTI_GIORNALIERA['PVC']} minuti\n"
        f"• Alluminio: {CAPACITA_MINUTI_GIORNALIERA['Alluminio']} minuti\n\n"
        "Regole tempo:\n"
        "• Battente: 90 min/vetro (Alluminio +30 min/struttura)\n"
        "• Scorrevole: 480 min/struttura\n"
        "• Speciale: 480 min/struttura\n\n"
        "⚠️ Per Battente i vetri sono TOTALI della riga (somma su tutte le strutture)."
    )

with col2:
    st.subheader("➕ Nuovo ordine (con righe)")

    cliente = st.text_input("Cliente")
    prodotto = st.text_input("Prodotto/commessa")
    data_richiesta = st.date_input("Data richiesta consegna", value=date.today())

    st.markdown("### Aggiungi riga ordine")

    materiale = st.selectbox("Materiale riga", ["PVC", "Alluminio"])
    tipologia = st.selectbox("Tipologia riga", ["Battente", "Scorrevole", "Struttura speciale"])
    quantita_strutture = st.number_input("Quantità strutture (riga)", min_value=1, value=1, step=1)

    if tipologia == "Battente":
        vetri_totali = st.number_input(
            "Numero vetri TOTALI per questa riga (somma su tutte le strutture)",
            min_value=1,
            value=1,
            step=1
        )
    else:
        vetri_totali = 0

    minuti_riga, minuti_medi = minuti_preview(materiale, tipologia, int(quantita_strutture), int(vetri_totali))
    st.info(f"⏱️ Questa riga: totale {minuti_riga} minuti (≈ {minuti_medi} min/struttura)")

    cadd, cclear = st.columns(2)

    with cadd:
        if st.button("➕ Aggiungi riga"):
            st.session_state["righe_correnti"].append({
                "materiale": materiale,
                "tipologia": tipologia,
                "quantita_strutture": int(quantita_strutture),
                "vetri_totali": int(vetri_totali) if tipologia == "Battente" else "",
                "tempo_minuti": int(minuti_riga)
            })
            st.success("Riga aggiunta")

    with cclear:
        if st.button("🧹 Svuota righe"):
            st.session_state["righe_correnti"] = []
            st.warning("Righe azzerate")

    st.markdown("### Righe attuali")
    righe = st.session_state["righe_correnti"]
    if righe:
        st.dataframe(righe, use_container_width=True)
        totale = sum(int(r.get("tempo_minuti", 0)) for r in righe)
        st.success(f"Totale ordine (somma righe): {totale} minuti")
    else:
        st.info("Nessuna riga aggiunta.")

    if st.button("💾 Salva ordine"):
        if (not cliente) or (not prodotto):
            st.error("Compila cliente e prodotto.")
        elif not st.session_state["righe_correnti"]:
            st.error("Aggiungi almeno una riga ordine.")
        else:
            ordini_esistenti = dati.get("ordini", [])
            max_gruppo = 0
            for oo in ordini_esistenti:
                try:
                    max_gruppo = max(max_gruppo, int(oo.get("ordine_gruppo", 0)))
                except Exception:
                    pass
            ordine_gruppo = max_gruppo + 1

            for r in st.session_state["righe_correnti"]:
                nuovo = {
                    "id": len(dati["ordini"]) + 1,
                    "ordine_gruppo": ordine_gruppo,
                    "cliente": cliente,
                    "prodotto": prodotto,
                    "materiale": r["materiale"],
                    "tipologia": r["tipologia"],
                    "quantita_strutture": r["quantita_strutture"],
                    "vetri_totali": r["vetri_totali"],
                    "tempo_minuti": int(r["tempo_minuti"]),
                    "data_richiesta": str(data_richiesta),
                    "inserito_il": str(date.today())
                }
                dati["ordini"].append(nuovo)

            salva_dati(dati)
            st.session_state["righe_correnti"] = []
            st.success(f"Ordine salvato (gruppo {ordine_gruppo})")

st.divider()

st.subheader("📋 Ordini (righe)")
if dati.get("ordini"):
    st.dataframe(dati["ordini"], use_container_width=True)
    
else:
    st.info("Nessun ordine inserito.")
st.markdown("## 🧹 Cancellazione")

col_del1, col_del2 = st.columns(2)

# =========================
# Cancella singola riga (per ID)
# =========================
with col_del1:
    st.markdown("### 🗑️ Cancella singola riga")

    if dati.get("ordini"):
        # elenco righe
        righe_map = {}
        opzioni = []
        for o in dati["ordini"]:
            rid = int(o.get("id", 0))
            gruppo = o.get("ordine_gruppo", "")
            materiale = o.get("materiale", "")
            tipologia = o.get("tipologia", "")
            minuti = o.get("tempo_minuti", "")
            label = f"ID {rid} | Gruppo {gruppo} | {materiale} | {tipologia} | {minuti} min"
            opzioni.append(label)
            righe_map[label] = rid

        scelta_riga = st.selectbox("Seleziona riga", opzioni, key="sel_riga_delete")

        if st.button("❌ Elimina riga selezionata", key="btn_delete_riga"):
            id_da_cancellare = righe_map[scelta_riga]

            # elimina riga
            dati["ordini"] = [o for o in dati["ordini"] if int(o.get("id", -1)) != id_da_cancellare]

            # rinumera ID per evitare buchi (puoi togliere questo blocco se non vuoi rinumerare)
            for i, o in enumerate(dati["ordini"], start=1):
                o["id"] = i

            salva_dati(dati)
            st.session_state.pop("consegne", None)
            st.session_state.pop("piano", None)
            st.success(f"Eliminata riga ID {id_da_cancellare}")
            st.rerun()
    else:
        st.info("Nessuna riga presente.")


# =========================
# Cancella ordine completo (per Gruppo ordine_gruppo)
# =========================
with col_del2:
    st.markdown("### 🧨 Cancella ordine completo (Gruppo)")

    if dati.get("ordini"):
        gruppi = sorted({str(o.get("ordine_gruppo")) for o in dati["ordini"] if o.get("ordine_gruppo") is not None})

        if gruppi:
            gruppo_sel = st.selectbox("Seleziona Gruppo ordine", gruppi, key="sel_gruppo_delete")

            righe_gruppo = [o for o in dati["ordini"] if str(o.get("ordine_gruppo")) == str(gruppo_sel)]
            st.caption(f"Righe che verranno eliminate: {len(righe_gruppo)}")

            if st.button("🔥 Elimina TUTTO l’ordine", key="btn_delete_gruppo"):
                dati["ordini"] = [o for o in dati["ordini"] if str(o.get("ordine_gruppo")) != str(gruppo_sel)]

                # rinumera ID
                for i, o in enumerate(dati["ordini"], start=1):
                    o["id"] = i

                salva_dati(dati)
                st.session_state.pop("consegne", None)
                st.session_state.pop("piano", None)
                st.success(f"Eliminato ordine (Gruppo {gruppo_sel})")
                st.rerun()
        else:
            st.info("Nessun gruppo disponibile.")
    else:
        st.info("Nessun ordine presente.")

c1, c2, c3 = st.columns([1, 1, 2])

with c1:
    if st.button("📅 Calcola piano"):
        consegne, piano = calcola_piano(dati)
        st.session_state["consegne"] = consegne
        st.session_state["piano"] = piano

with c2:
    if st.button("🗑️ Cancella tutto"):
        dati = {"capacita_giornaliera": dati.get("capacita_giornaliera", 0), "ordini": []}
        salva_dati(dati)
        st.session_state.pop("consegne", None)
        st.session_state.pop("piano", None)
        st.session_state["righe_correnti"] = []
        st.warning("Ordini cancellati")

with c3:
    if st.button("🚪 Logout"):
        st.session_state.logged_in = False
        st.rerun()

if "consegne" in st.session_state:
    st.subheader("✅ Consegne stimate (per riga)")
    st.dataframe(st.session_state["consegne"], use_container_width=True)

if "piano" in st.session_state:
    st.subheader("🧾 Piano produzione giorno per giorno (spezzato a minuti)")
    st.dataframe(st.session_state["piano"], use_container_width=True)

