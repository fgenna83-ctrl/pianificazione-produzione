import streamlit as st
from datetime import date, datetime, timedelta
import json
import os

FILE_DATI = "dati_produzione.json"

def carica_dati():
    if os.path.exists(FILE_DATI):
        with open(FILE_DATI, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"capacita_giornaliera": 0, "ordini": []}

def salva_dati(dati):
    with open(FILE_DATI, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=2)

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
        rimanenti = o["quantita"]
        while rimanenti > 0:
            prodotti_oggi = min(cap, rimanenti)
            piano.append({
                "Data": str(giorno),
                "Ordine": o["id"],
                "Cliente": o["cliente"],
                "Prodotto": o["prodotto"],
                "Pezzi": prodotti_oggi
            })
            rimanenti -= prodotti_oggi
            if rimanenti > 0:
                giorno = giorno + timedelta(days=1)

        consegna_stimata = giorno
        o["consegna_stimata"] = str(consegna_stimata)
        consegne.append({
            "Ordine": o["id"],
            "Cliente": o["cliente"],
            "Prodotto": o["prodotto"],
            "Quantità": o["quantita"],
            "Richiesta": o["data_richiesta"],
            "Stimata": str(consegna_stimata)
        })

        giorno = giorno + timedelta(days=1)

    salva_dati(dati)
    return consegne, piano


st.set_page_config(page_title="Planner Produzione", layout="wide")
st.title("📦 Planner Produzione (Online)")

dati = carica_dati()

col1, col2 = st.columns(2)

with col1:
    st.subheader("⚙️ Impostazioni")
    cap = st.number_input("Capacità (pezzi/giorno)", min_value=0, value=int(dati["capacita_giornaliera"]), step=10)
    if st.button("Salva capacità"):
        dati["capacita_giornaliera"] = int(cap)
        salva_dati(dati)
        st.success("Capacità salvata")

with col2:
    st.subheader("➕ Nuovo ordine")
    cliente = st.text_input("Cliente")
    prodotto = st.text_input("Prodotto/commessa")
    qta = st.number_input("Quantità (pezzi)", min_value=0, value=0, step=10)
    data_richiesta = st.date_input("Data richiesta consegna", value=date.today())

    if st.button("Aggiungi ordine"):
        if dati["capacita_giornaliera"] <= 0:
            st.error("Imposta prima la capacità giornaliera.")
        elif not cliente or not prodotto or qta <= 0:
            st.error("Compila cliente, prodotto e quantità (>0).")
        else:
            nuovo = {
                "id": len(dati["ordini"]) + 1,
                "cliente": cliente,
                "prodotto": prodotto,
                "quantita": int(qta),
                "data_richiesta": str(data_richiesta),
                "inserito_il": str(date.today())
            }
            dati["ordini"].append(nuovo)
            salva_dati(dati)
            st.success("Ordine aggiunto")

st.divider()

st.subheader("📋 Ordini")
if dati["ordini"]:
    st.dataframe(dati["ordini"], use_container_width=True)
else:
    st.info("Nessun ordine inserito.")

c1, c2, c3 = st.columns([1,1,2])
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

if "consegne" in st.session_state:
    st.subheader("✅ Consegne stimate")
    st.dataframe(st.session_state["consegne"], use_container_width=True)

if "piano" in st.session_state:
    st.subheader("🧾 Piano produzione giorno per giorno")
    st.dataframe(st.session_state["piano"], use_container_width=True)
