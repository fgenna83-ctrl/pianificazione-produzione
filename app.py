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
# TEMPI PRODUZIONE (NUOVI)
# =========================
def minuti_per_struttura(materiale: str, tipologia: str, num_vetri: int) -> int:
    """
    Battente:
      - PVC: 90 min a vetro
      - Alluminio: 90 min a vetro + 30 min fissi a struttura
    Scorrevole e Speciale:
      - 8 ore/struttura (480 min) per entrambi i materiali
    """
    if tipologia == "Battente":
        tempo = num_vetri * 90
        if materiale == "Alluminio":
            tempo += 30
        return tempo

    # Scorrevole o Struttura speciale
    return MINUTI_8_ORE


def tempo_riga(materiale: str, tipologia: str, quantita_strutture: int, num_vetri: int) -> int:
    return minuti_per_struttura(materiale, tipologia, num_vetri) * quantita_strutture


# =========================
# PIANIFICAZIONE (come già funziona: spezza su più giorni)
# =========================
def calcola_piano(dati):
    ordini = dati.get("ordini", [])
    if not ordini:
        return [], []

    oggi = date.today()

    # Stato separato per ogni materiale
    stato = {
        "PVC": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["PVC"]},
        "Alluminio": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["Alluminio"]},
    }

    piano = []
    consegne = []

    for o in ordini:
        materiale = o.get("materiale", "PVC")
        if materiale not in stato:
            materiale = "PVC"

        tempo_totale = int(o.get("tempo_minuti", 0))
        if tempo_totale < 0:
            tempo_totale = 0

        remaining = tempo_totale

        giorno = stato[materiale]["giorno"]
        usati = stato[materiale]["usati"]
        cap = stato[materiale]["cap"]

        while remaining > 0:
            disponibili = cap - usati
            if disponibili <= 0:
                giorno = giorno + timedelta(days=1)
                usati = 0
                continue

            prodotti_oggi = min(disponibili, remaining)
            usati += prodotti_oggi
            remaining -= prodotti_oggi

            piano.append({
                "Data": str(giorno),
                "Ordine": o.get("id", ""),
                "Gruppo": o.get("ordine_gruppo", ""),
                "Cliente": o.get("cliente", ""),
                "Prodotto": o.get("prodotto", ""),
                "Materiale": materiale,
                "Tipologia": o.get("tipologia", ""),
                "Qta_strutture": o.get("quantita_strutture", ""),
                "Vetri": o.get("num_vetri", ""),
                "Minuti_prodotti": prodotti_oggi,
                "Minuti_residui_materiale": cap - usati
            })

            if remaining > 0 and usati >= cap:
                giorno = giorno + timedelta(days=1)
                usati = 0

        o["consegna_stimata"] = str(giorno)

        consegne.append({
            "Ordine": o.get("id", ""),
            "Gruppo": o.get("ordine_gruppo", ""),
            "Cliente": o.get("cliente", ""),
            "Prodotto": o.get("prodotto", ""),
            "Materiale": materiale,
            "Tipologia": o.get("tipologia", ""),
            "Qta_strutture": o.get("quantita_strutture", ""),
            "Vetri": o.get("num_vetri", ""),
            "Tempo_minuti": tempo_totale,
            "Richiesta": o.get("data_richiesta", ""),
            "Stimata": str(giorno)
        })

        stato[materiale]["giorno"] = giorno
        stato[materiale]["usati"] = usati

    salva_dati(dati)
    return consegne, piano


# =========================
# APP
# =========================
st.set_page_config(page_title="Planner Produzione", layout="wide")

if not check_login():
    st.stop()

st.title("📦 Planner Produzione (Online)")

dati = carica_dati()

# Stato righe ordine in sessione (per costruire ordini con più tipologie)
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
        "• Speciale: 480 min/struttura"
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
        num_vetri = st.number_input("Numero vetri per struttura (solo battente)", min_value=1, value=1, step=1)
    else:
        num_vetri = 0  # non serve

    minuti_struttura = minuti_per_struttura(materiale, tipologia, int(num_vetri))
    minuti_riga = minuti_struttura * int(quantita_strutture)

    st.info(f"⏱️ Questa riga: {minuti_struttura} min/struttura → totale riga {minuti_riga} minuti")

    cadd, cclear = st.columns(2)

    with cadd:
        if st.button("➕ Aggiungi riga"):
            st.session_state["righe_correnti"].append({
                "materiale": materiale,
                "tipologia": tipologia,
                "quantita_strutture": int(quantita_strutture),
                "num_vetri": int(num_vetri) if tipologia == "Battente" else "",
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
            # Generiamo un id "gruppo ordine" per collegare le righe dello stesso ordine
            ordini_esistenti = dati.get("ordini", [])
            max_gruppo = 0
            for oo in ordini_esistenti:
                try:
                    max_gruppo = max(max_gruppo, int(oo.get("ordine_gruppo", 0)))
                except Exception:
                    pass
            ordine_gruppo = max_gruppo + 1

            # ogni riga diventa un "record" pianificabile (materiale unico per riga)
            for r in st.session_state["righe_correnti"]:
                nuovo = {
                    "id": len(dati["ordini"]) + 1,
                    "ordine_gruppo": ordine_gruppo,
                    "cliente": cliente,
                    "prodotto": prodotto,
                    "materiale": r["materiale"],
                    "tipologia": r["tipologia"],
                    "quantita_strutture": r["quantita_strutture"],
                    "num_vetri": r["num_vetri"],
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
