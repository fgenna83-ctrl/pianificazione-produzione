import streamlit as st
from datetime import date, datetime, timedelta
import json
import os
import hashlib

FILE_DATI = "dati_produzione.json"


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
# TEMPI PRODUZIONE
# =========================
def calcola_tempo_produzione(materiale: str, num_vetri: int) -> int:
    # 90 minuti a vetro (PVC)
    tempo = num_vetri * 90
    # Alluminio: +30 minuti fissi a struttura
    if materiale == "Alluminio":
        tempo += 30
    return tempo


# =========================
# PIANIFICAZIONE (come prima)
# =========================
def calcola_piano(dati):
    cap = dati["capacita_giornaliera"]
    ordini = dati["ordini"]
    if cap <= 0 or not ordini:
        return [], []

    oggi = date.today()
    giorno = oggi
    piano = []
    consegne = []

    for o in ordini:
        # Manteniamo la logica "come prima": usiamo una quantità da consumare
        # Se esiste tempo_minuti, useremo quello come "quantità" per il piano,
        # altrimenti usiamo quantita (retro-compatibilità).
        da_produrre = int(o.get("tempo_minuti", o.get("quantita", 0)))

        rimanenti = da_produrre
        while rimanenti > 0:
            prodotti_oggi = min(cap, rimanenti)
            piano.append({
                "Data": str(giorno),
                "Ordine": o["id"],
                "Cliente": o.get("cliente", ""),
                "Prodotto": o.get("prodotto", ""),
                "Materiale": o.get("materiale", ""),
                "Tipologia": o.get("tipologia", ""),
                "Vetri": o.get("num_vetri", ""),
                "Unità_piano": prodotti_oggi
            })
            rimanenti -= prodotti_oggi
            if rimanenti > 0:
                giorno = giorno + timedelta(days=1)

        consegna_stimata = giorno
        o["consegna_stimata"] = str(consegna_stimata)
        consegne.append({
            "Ordine": o["id"],
            "Cliente": o.get("cliente", ""),
            "Prodotto": o.get("prodotto", ""),
            "Materiale": o.get("materiale", ""),
            "Tipologia": o.get("tipologia", ""),
            "Vetri": o.get("num_vetri", ""),
            "Tempo_minuti": o.get("tempo_minuti", ""),
            "Richiesta": o.get("data_richiesta", ""),
            "Stimata": str(consegna_stimata)
        })

        giorno = giorno + timedelta(days=1)

    salva_dati(dati)
    return consegne, piano


# =========================
# APP
# =========================
st.set_page_config(page_title="Planner Produzione", layout="wide")

# blocco login
if not check_login():
    st.stop()

st.title("📦 Planner Produzione (Online)")

dati = carica_dati()

col1, col2 = st.columns(2)

with col1:
    st.subheader("⚙️ Impostazioni")
    cap = st.number_input(
        "Capacità (unità/giorno) - per ora usata come prima",
        min_value=0,
        value=int(dati["capacita_giornaliera"]),
        step=30
    )
    if st.button("Salva capacità"):
        dati["capacita_giornaliera"] = int(cap)
        salva_dati(dati)
        st.success("Capacità salvata")

with col2:
    st.subheader("➕ Nuovo ordine")

    cliente = st.text_input("Cliente")
    prodotto = st.text_input("Prodotto/commessa")

    materiale = st.selectbox("Materiale", ["PVC", "Alluminio"])
    tipologia = st.selectbox("Tipologia", ["Battente", "Scorrevole", "Struttura speciale"])
    num_vetri = st.number_input("Numero vetri", min_value=1, value=1, step=1)

    data_richiesta = st.date_input("Data richiesta consegna", value=date.today())

    # preview tempo calcolato
    tempo_preview = calcola_tempo_produzione(materiale, int(num_vetri))
    st.info(f"⏱️ Tempo stimato: {tempo_preview} minuti")

    if st.button("Aggiungi ordine"):
        if dati["capacita_giornaliera"] <= 0:
            st.error("Imposta prima la capacità giornaliera.")
        elif (not cliente) or (not prodotto):
            st.error("Compila cliente e prodotto.")
        else:
            tempo = calcola_tempo_produzione(materiale, int(num_vetri))
            nuovo = {
                "id": len(dati["ordini"]) + 1,
                "cliente": cliente,
                "prodotto": prodotto,
                "materiale": materiale,
                "tipologia": tipologia,
                "num_vetri": int(num_vetri),
                "tempo_minuti": int(tempo),
                "data_richiesta": str(data_richiesta),
                "inserito_il": str(date.today())
            }
            dati["ordini"].append(nuovo)
            salva_dati(dati)
            st.success(f"Ordine aggiunto (tempo: {tempo} minuti)")

st.divider()

st.subheader("📋 Ordini")
if dati["ordini"]:
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
        dati = {"capacita_giornaliera": int(dati["capacita_giornaliera"]), "ordini": []}
        salva_dati(dati)
        st.session_state.pop("consegne", None)
        st.session_state.pop("piano", None)
        st.warning("Ordini cancellati")

with c3:
    if st.button("🚪 Logout"):
        st.session_state.logged_in = False
        st.rerun()

if "consegne" in st.session_state:
    st.subheader("✅ Consegne stimate")
    st.dataframe(st.session_state["consegne"], use_container_width=True)

if "piano" in st.session_state:
    st.subheader("🧾 Piano produzione giorno per giorno")
    st.dataframe(st.session_state["piano"], use_container_width=True)
    st.caption("Nota: per ora 'Unità_piano' usa la capacità come prima. Nel prossimo step lo trasformiamo in minuti/giorno.")


