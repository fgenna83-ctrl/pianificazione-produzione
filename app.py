import streamlit as st
from datetime import date, timedelta
import json
import os
import hashlib
import pandas as pd
import altair as alt

FILE_DATI = "dati_produzione.json"

# Capacità giornaliera fissa per materiale (minuti/giorno)
CAPACITA_MINUTI_GIORNALIERA = {
    "PVC": 4500,
    "Alluminio": 3000
}

MINUTI_8_ORE = 8 * 60  # 480


# =========================
# GIORNI LAVORATIVI (LUN-VEN)
# =========================
def prossimo_giorno_lavorativo(d):
    while d.weekday() >= 5:  # 5=sabato, 6=domenica
        d = d + timedelta(days=1)
    return d


def aggiungi_giorno_lavorativo(d):
    d = d + timedelta(days=1)
    return prossimo_giorno_lavorativo(d)


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
# PIANIFICAZIONE (spezzata su più giorni) - NO WEEKEND
# =========================
def calcola_piano(dati):
    ordini = dati.get("ordini", [])
    if not ordini:
        return [], []

    # ✅ FIX: prima assegno oggi, poi lo rendo lavorativo
    oggi = date.today()
    oggi = prossimo_giorno_lavorativo(oggi)

    def safe_date(s):
        try:
            return date.fromisoformat(str(s))
        except Exception:
            return oggi

    # Stato per materiale (minuti/giorno)
    stato = {
        "PVC": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["PVC"]},
        "Alluminio": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["Alluminio"]},
    }

    piano = []
    consegna_per_gruppo = {}  # consegna unica per ordine (gruppo)

    # --- Raggruppo righe per commessa (ordine_gruppo) ---
    gruppi_map = {}
    for o in ordini:
        g = o.get("ordine_gruppo", "0")
        if g is None or str(g).strip() == "":
            g = "0"
        g = str(g)
        gruppi_map.setdefault(g, []).append(o)

    # Ordino le commesse per data richiesta (min) e poi per gruppo
    def gruppo_sort_key(g):
        righe = gruppi_map[g]
        min_req = min(safe_date(r.get("data_richiesta")) for r in righe)
        try:
            gnum = int(g)
        except Exception:
            gnum = 0
        return (min_req, gnum, g)

    gruppi_ordinati = sorted(gruppi_map.keys(), key=gruppo_sort_key)

    # Pianifica una singola riga sulla sua linea materiale, spezzandola su più giorni
    def pianifica_riga(o):
        materiale = o.get("materiale", "PVC")
        if materiale not in stato:
            materiale = "PVC"

        cap = stato[materiale]["cap"]
        giorno = stato[materiale]["giorno"]
        usati = stato[materiale]["usati"]

        tempo_totale = int(o.get("tempo_minuti", 0) or 0)
        if tempo_totale < 0:
            tempo_totale = 0

        remaining = tempo_totale
        qta_strutture = int(o.get("quantita_strutture", 0) or 0)

        while remaining > 0:
            disponibili = cap - usati
            if disponibili <= 0:
                giorno = aggiungi_giorno_lavorativo(giorno)
                usati = 0
                continue

            prodotti_oggi = min(disponibili, remaining)
            usati += prodotti_oggi
            remaining -= prodotti_oggi

            # strutture prodotte oggi (stima proporzionale ai minuti)
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

            # se non finito e ho esaurito la capacità -> domani (lavorativo)
            if remaining > 0 and usati >= cap:
                giorno = aggiungi_giorno_lavorativo(giorno)
                usati = 0

        # fine riga
        o["consegna_stimata"] = str(giorno)

        # aggiorno stato materiale
        stato[materiale]["giorno"] = giorno
        stato[materiale]["usati"] = usati

        return giorno  # giorno di fine riga

    # --- Pianificazione COMMESSA per COMMESSA (sequenza globale) ---
    for g in gruppi_ordinati:
        righe = gruppi_map[g]

        # Ordino le righe dentro la commessa
        righe.sort(key=lambda o: (o.get("materiale", "PVC"), int(o.get("id", 0) or 0)))

        # Pianifico tutte le righe della commessa
        fine_righe = []
        for r in righe:
            fine_righe.append(pianifica_riga(r))

        # Giorno fine commessa
        fine_commessa = max(fine_righe) if fine_righe else stato["PVC"]["giorno"]
        fine_commessa = prossimo_giorno_lavorativo(fine_commessa)  # sicurezza

        # Salvo consegna unica per commessa (gruppo)
        base = righe[0] if righe else {}
        consegna_per_gruppo[g] = {
            "Gruppo": g,
            "Cliente": base.get("cliente", ""),
            "Prodotto": base.get("prodotto", ""),
            "Richiesta": str(min(safe_date(r.get("data_richiesta")) for r in righe)) if righe else str(oggi),
            "Tempo_totale_minuti": int(sum(int(r.get("tempo_minuti", 0) or 0) for r in righe)),
            "Stimata": str(fine_commessa),
        }

        # porto tutte le linee almeno a fine_commessa
        for mat in stato:
            if stato[mat]["giorno"] < fine_commessa:
                stato[mat]["giorno"] = fine_commessa
                stato[mat]["usati"] = 0

    salva_dati(dati)

    consegne_ordini = list(consegna_per_gruppo.values())

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
            dati["ordini"] = [o for o in dati["ordini"] if int(o.get("id", -1)) != id_da_cancellare]

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

    # -----------------------------
    # GANTT GIORNO PER GIORNO (lun-ven)
    # -----------------------------
    st.subheader("📊 Gantt Produzione (giorno per giorno)")

    df = pd.DataFrame(st.session_state["piano"])
    if df.empty:
        st.info("Nessun dato per il Gantt.")
    else:
        df["Data"] = pd.to_datetime(df["Data"])
        df = df[df["Data"].dt.weekday < 5]  # lun-ven

        df["Commessa"] = (
            "Gruppo " + df["Gruppo"].astype(str)
            + " | " + df["Cliente"].astype(str)
            + " | " + df["Prodotto"].astype(str)
        )

        agg = (
            df.groupby(["Data", "Commessa", "Gruppo", "Cliente", "Prodotto"], as_index=False)
              .agg(
                  strutture=("Strutture_prodotte", "sum"),
                  minuti=("Minuti_prodotti", "sum")
              )
        )

        agg["inizio"] = agg["Data"]
        agg["fine"] = agg["Data"] + pd.Timedelta(days=1)

        agg["label"] = agg["Commessa"] + "\n" + agg["strutture"].round(1).astype(str) + " strutt."

        colA, colB, colC = st.columns([1, 1, 2])
        with colA:
            show_minutes = st.checkbox("Mostra minuti nel box", value=False, key="gantt_show_minutes")
        with colB:
            ordina = st.selectbox("Ordina righe", ["Per Gruppo", "Per Cliente"], index=0, key="gantt_ordina")
        with colC:
            st.caption("Ogni rettangolo = 1 giorno lavorativo di produzione per una commessa.")

        if show_minutes:
            agg["label"] = (
                agg["Commessa"]
                + "\n"
                + agg["strutture"].round(1).astype(str)
                + " strutt. | "
                + agg["minuti"].astype(int).astype(str)
                + " min"
            )

        if ordina == "Per Gruppo":
            sort_y = alt.SortField(field="Gruppo", order="ascending")
        else:
            sort_y = alt.SortField(field="Cliente", order="ascending")

        base = alt.Chart(agg).encode(
            y=alt.Y("Commessa:N", sort=sort_y, title="Commesse"),
            x=alt.X(
                "yearmonthdate(inizio):T",
                title="Giorni",
                axis=alt.Axis(format="%d/%m", labelAngle=0)
            ),
            x2="fine:T",
            tooltip=[
                alt.Tooltip("Data:T", title="Giorno"),
                alt.Tooltip("Commessa:N", title="Commessa"),
                alt.Tooltip("strutture:Q", title="Strutture prodotte"),
                alt.Tooltip("minuti:Q", title="Minuti prodotti"),
            ],
        )

        bars = base.mark_bar(cornerRadius=6).encode(
            color=alt.Color("Cliente:N", legend=alt.Legend(title="Cliente"))
        )

        text = alt.Chart(agg).mark_text(
            align="left",
            baseline="top",
            dx=6,
            dy=6,
            fontSize=12,
            lineBreak="\n"
        ).encode(
            y=alt.Y("Commessa:N", sort=sort_y),
            x=alt.X("inizio:T"),
            text="label:N"
        )

        chart = (bars + text).properties(
            height=max(300, 45 * len(agg["Commessa"].unique())),
        ).interactive()

        st.altair_chart(chart, use_container_width=True)






