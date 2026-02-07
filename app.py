import streamlit as st
from datetime import date, timedelta
import json
import os
import hashlib
import pandas as pd
import altair as alt
import streamlit.components.v1 as components
from pathlib import Path

# =========================
# FILE DATI
# =========================
FILE_DATI = "dati_produzione.json"

# Capacità giornaliera produzione (minuti/giorno) per materiale
CAPACITA_MINUTI_GIORNALIERA = {
    "PVC": 4500,
    "Alluminio": 3000,
}

MINUTI_8_ORE = 8 * 60  # 480

# =========================
# TAGLIO (pezzi/strutture al giorno) - 2 macchine: una PVC e una Alluminio
# =========================
CAP_TAGLIO_STRUTTURE_GIORNO = {
    "PVC": {
        "Battente": 15,
        "Scorrevole": 10,
        "Struttura speciale": 5,
    },
    "Alluminio": {
        "Battente": 15,
        "Scorrevole": 10,
        "Struttura speciale": 5,
    },
}

# =========================
# COMPONENTE (GANTT DRAG&DROP)
# Cartella: gantt_dnd/index.html
# =========================
_COMPONENT_DIR = Path(__file__).resolve().parent / "gantt_dnd"
if _COMPONENT_DIR.exists() and (_COMPONENT_DIR / "index.html").exists():
    gantt_dnd = components.declare_component("gantt_dnd", path=str(_COMPONENT_DIR))
else:
    gantt_dnd = None

# =========================
# GIORNI LAVORATIVI (LUN-VEN)
# =========================
def prossimo_giorno_lavorativo(d: date) -> date:
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d

def aggiungi_giorno_lavorativo(d: date) -> date:
    return prossimo_giorno_lavorativo(d + timedelta(days=1))

def safe_date(s):
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s))
    except Exception:
        return prossimo_giorno_lavorativo(date.today())

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
    return {"ordini": []}

def salva_dati(dati):
    with open(FILE_DATI, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=2)

# =========================
# TEMPI PRODUZIONE
# =========================
def tempo_riga(materiale: str, tipologia: str, quantita_strutture: int, vetri_totali: int) -> int:
    """
    Battente:
      - PVC: 90 min per vetro (vetri_totali)
      - Alluminio: 90 min per vetro (vetri_totali) + 30 min per struttura (quantita_strutture)
    Scorrevole e Struttura speciale:
      - 480 min per struttura (quantita_strutture) indipendente dal materiale
    """
    tipologia = (tipologia or "").strip()

    if tipologia == "Battente":
        t = int(vetri_totali) * 90
        if materiale == "Alluminio":
            t += int(quantita_strutture) * 30
        return t

    return int(quantita_strutture) * MINUTI_8_ORE

def minuti_preview(materiale: str, tipologia: str, quantita_strutture: int, vetri_totali: int):
    tot = tempo_riga(materiale, tipologia, quantita_strutture, vetri_totali)
    if quantita_strutture > 0:
        return tot, int(round(tot / quantita_strutture))
    return tot, tot

# =========================
# HELPERS: LOAD & CHECK (TAGLIO)
# =========================
def _build_load_taglio_excluding_group(piano_taglio: list[dict], gruppo_escluso: str) -> dict:
    # load[materiale][tipologia][data] = pezzi tagliati
    load = {}
    for r in (piano_taglio or []):
        if str(r.get("Gruppo")) == str(gruppo_escluso):
            continue
        mat = r.get("Materiale", "PVC")
        tip = r.get("Tipologia", "Battente")
        ds = str(r.get("Data"))
        pezzi = int(r.get("Strutture_tagliate", 0) or 0)
        load.setdefault(mat, {}).setdefault(tip, {})
        load[mat][tip][ds] = load[mat][tip].get(ds, 0) + pezzi
    return load

def _need_taglio_for_group(dati: dict, gruppo: str) -> dict:
    # need[materiale][tipologia] = strutture da tagliare
    need = {}
    for o in dati.get("ordini", []):
        if str(o.get("ordine_gruppo")) != str(gruppo):
            continue
        mat = o.get("materiale", "PVC")
        tip = (o.get("tipologia", "") or "").strip()
        qta = int(o.get("quantita_strutture", 0) or 0)
        need.setdefault(mat, {})
        need[mat][tip] = need[mat].get(tip, 0) + qta
    return need

def _first_day_has_any_cut_capacity(load_taglio: dict, need_taglio: dict, start_day: date) -> bool:
    start_day = prossimo_giorno_lavorativo(start_day)
    ds = str(start_day)

    # ok se esiste almeno un (mat, tip) del gruppo con free > 0
    for mat, tips in need_taglio.items():
        for tip, qta in tips.items():
            if qta <= 0:
                continue
            cap = int(CAP_TAGLIO_STRUTTURE_GIORNO.get(mat, {}).get(tip, 0))
            used = int(load_taglio.get(mat, {}).get(tip, {}).get(ds, 0) or 0)
            free = max(0, cap - used)
            if free > 0:
                return True
    return False

def _insert_group_taglio_into_free_capacity(dati: dict, piano_taglio_base: list[dict], gruppo: str, start_day: date) -> tuple[list[dict], date]:
    """
    Inserisce TAGLIO del gruppo nei buchi a partire da start_day.
    Ritorna: (piano_taglio_add, last_cut_day)
    - Se nel primo giorno c'è anche solo spazio per 1 struttura su una qualunque riga del gruppo -> inserisce e il resto slitta
    - Blocca solo se primo giorno è 0 assoluto per tutto il gruppo
    """
    start_day = prossimo_giorno_lavorativo(start_day)

    load = _build_load_taglio_excluding_group(piano_taglio_base, gruppo)
    need = _need_taglio_for_group(dati, gruppo)

    if not _first_day_has_any_cut_capacity(load, need, start_day):
        raise ValueError(f"❌ Non c'è spazio al TAGLIO nemmeno per 1 struttura il {start_day} (tutto pieno).")

    righe = [o for o in dati.get("ordini", []) if str(o.get("ordine_gruppo")) == str(gruppo)]
    cliente = (righe[0].get("cliente") if righe else "")
    prodotto = (righe[0].get("prodotto") if righe else "")

    piano_add = []
    last_day = start_day

    # Pianifico per macchina/materiale, ma capacità per tipologia è separata
    for mat, tips in need.items():
        for tip, remaining in tips.items():
            remaining = int(remaining or 0)
            if remaining <= 0:
                continue

            cap = int(CAP_TAGLIO_STRUTTURE_GIORNO.get(mat, {}).get(tip, 0))
            if cap <= 0:
                # se manca cap, non riesco a pianificare
                raise ValueError(f"❌ Capacità TAGLIO non configurata per {mat} / {tip}.")

            day = start_day
            while remaining > 0:
                day = prossimo_giorno_lavorativo(day)
                ds = str(day)

                used = int(load.get(mat, {}).get(tip, {}).get(ds, 0) or 0)
                free = max(0, cap - used)

                if free <= 0:
                    day = aggiungi_giorno_lavorativo(day)
                    continue

                take = min(free, remaining)
                load.setdefault(mat, {}).setdefault(tip, {})
                load[mat][tip][ds] = used + take
                remaining -= take

                piano_add.append({
                    "Data": ds,
                    "Gruppo": str(gruppo),
                    "Cliente": str(cliente),
                    "Prodotto": str(prodotto),
                    "Materiale": mat,
                    "Tipologia": tip,
                    "Strutture_tagliate": int(take),
                    "Residuo_taglio_tipologia": int(cap - (used + take)),
                })

                last_day = max(last_day, day)

                if remaining > 0:
                    day = aggiungi_giorno_lavorativo(day)

    return piano_add, last_day

def _merge_piani_keep_existing(piano_base: list[dict], gruppo: str, piano_add: list[dict]) -> list[dict]:
    out = [r for r in (piano_base or []) if str(r.get("Gruppo")) != str(gruppo)]
    out.extend(piano_add)
    out.sort(key=lambda r: (str(r.get("Data")), str(r.get("Materiale")), str(r.get("Tipologia", "")), str(r.get("Gruppo"))))
    return out

# =========================
# HELPERS: LOAD & INSERT (PRODUZIONE)
# =========================
def _build_load_prod_excluding_group(piano_prod: list[dict], gruppo_escluso: str) -> dict:
    # load[materiale][data] = minuti prodotti
    load = {"PVC": {}, "Alluminio": {}}
    for r in (piano_prod or []):
        if str(r.get("Gruppo")) == str(gruppo_escluso):
            continue
        mat = r.get("Materiale", "PVC")
        ds = str(r.get("Data"))
        m = int(r.get("Minuti_prodotti", 0) or 0)
        load.setdefault(mat, {})
        load[mat][ds] = load[mat].get(ds, 0) + m
    return load

def _need_prod_minutes_for_group(dati: dict, gruppo: str) -> dict:
    need = {"PVC": 0, "Alluminio": 0}
    for o in dati.get("ordini", []):
        if str(o.get("ordine_gruppo")) != str(gruppo):
            continue
        mat = o.get("materiale", "PVC")
        need[mat] = need.get(mat, 0) + int(o.get("tempo_minuti", 0) or 0)
    return need

def _first_day_has_any_free_minutes(load: dict, need: dict, day: date) -> bool:
    day = prossimo_giorno_lavorativo(day)
    ds = str(day)
    mats = [m for m, mins in need.items() if mins > 0]
    for m in mats:
        cap = int(CAPACITA_MINUTI_GIORNALIERA.get(m, 0))
        used = int(load.get(m, {}).get(ds, 0) or 0)
        free = max(0, cap - used)
        if free > 0:
            return True
    return False

def _insert_group_prod_into_free_capacity(dati: dict, piano_prod_base: list[dict], gruppo: str, start_day: date) -> tuple[list[dict], date]:
    """
    Inserisce PRODUZIONE del gruppo nei buchi a partire da start_day.
    Ritorna: (piano_prod_add, last_prod_day)
    """
    start_day = prossimo_giorno_lavorativo(start_day)
    load = _build_load_prod_excluding_group(piano_prod_base, gruppo)
    need = _need_prod_minutes_for_group(dati, gruppo)

    if not _first_day_has_any_free_minutes(load, need, start_day):
        raise ValueError(f"❌ Non c'è spazio in PRODUZIONE nemmeno per 1 minuto il {start_day} (tutto pieno).")

    righe = [o for o in dati.get("ordini", []) if str(o.get("ordine_gruppo")) == str(gruppo)]
    cliente = (righe[0].get("cliente") if righe else "")
    prodotto = (righe[0].get("prodotto") if righe else "")

    piano_add = []
    last_day = start_day

    for mat in ["PVC", "Alluminio"]:
        remaining = int(need.get(mat, 0) or 0)
        if remaining <= 0:
            continue

        cap = int(CAPACITA_MINUTI_GIORNALIERA.get(mat, 0))
        day = start_day

        while remaining > 0:
            day = prossimo_giorno_lavorativo(day)
            ds = str(day)

            used = int(load.get(mat, {}).get(ds, 0) or 0)
            free = max(0, cap - used)

            if free <= 0:
                day = aggiungi_giorno_lavorativo(day)
                continue

            take = min(free, remaining)
            load.setdefault(mat, {})
            load[mat][ds] = used + take
            remaining -= take

            piano_add.append({
                "Data": ds,
                "Gruppo": str(gruppo),
                "Cliente": str(cliente),
                "Prodotto": str(prodotto),
                "Materiale": mat,
                "Minuti_prodotti": int(take),
                "Minuti_residui_materiale": int(cap - (used + take)),
            })

            last_day = max(last_day, day)

            if remaining > 0:
                day = aggiungi_giorno_lavorativo(day)

    return piano_add, last_day

# =========================
# CALCOLO PIANO (TAGLIO + PRODUZIONE)
# =========================
def calcola_piano(dati: dict):
    ordini = dati.get("ordini", [])
    if not ordini:
        return [], [], []

    # gruppi esistenti
    gruppi = sorted({str(o.get("ordine_gruppo")) for o in ordini}, key=lambda x: int(x) if str(x).isdigit() else 10**9)

    # ---- TAGLIO: pianifico gruppo per gruppo in ordine, usando start taglio del gruppo ----
    piano_taglio = []
    fine_taglio_gruppo = {}  # gruppo -> date (ultimo giorno di taglio)

    for g in gruppi:
        righe_g = [o for o in ordini if str(o.get("ordine_gruppo")) == str(g)]
        if not righe_g:
            continue

        # start taglio: preferisco data_inizio_taglio_gruppo, fallback data_inizio_gruppo, fallback data_richiesta, fallback oggi
        d0 = (
            righe_g[0].get("data_inizio_taglio_gruppo")
            or righe_g[0].get("data_inizio_gruppo")
            or righe_g[0].get("data_richiesta")
            or str(date.today())
        )
        start_taglio = prossimo_giorno_lavorativo(safe_date(d0))

        add, last_day = _insert_group_taglio_into_free_capacity(
            dati=dati,
            piano_taglio_base=piano_taglio,
            gruppo=g,
            start_day=start_taglio,
        )
        piano_taglio = _merge_piani_keep_existing(piano_taglio, g, add)
        fine_taglio_gruppo[g] = last_day

    # ---- PRODUZIONE: per ogni gruppo, start = giorno lavorativo successivo a fine taglio ----
    piano_prod = []
    fine_prod_gruppo = {}

    for g in gruppi:
        last_cut = fine_taglio_gruppo.get(g)
        if not last_cut:
            # se per qualche motivo non c'è taglio, parto da oggi
            start_prod = prossimo_giorno_lavorativo(date.today())
        else:
            start_prod = aggiungi_giorno_lavorativo(prossimo_giorno_lavorativo(last_cut))

        add_prod, last_prod = _insert_group_prod_into_free_capacity(
            dati=dati,
            piano_prod_base=piano_prod,
            gruppo=g,
            start_day=start_prod,
        )
        piano_prod = _merge_piani_keep_existing(piano_prod, g, add_prod)
        fine_prod_gruppo[g] = last_prod

    # ---- CONSEGNE: fine produzione + 3 giorni lavorativi ----
    consegne = []
    for g in gruppi:
        righe_g = [o for o in ordini if str(o.get("ordine_gruppo")) == str(g)]
        cliente = righe_g[0].get("cliente", "") if righe_g else ""
        prodotto = righe_g[0].get("prodotto", "") if righe_g else ""
        tempo_tot = sum(int(o.get("tempo_minuti", 0) or 0) for o in righe_g)

        fine_prod = fine_prod_gruppo.get(g)
        if fine_prod:
            d = prossimo_giorno_lavorativo(fine_prod)
            for _ in range(3):
                d = aggiungi_giorno_lavorativo(d)
            stimata = d
        else:
            stimata = prossimo_giorno_lavorativo(date.today())

        consegne.append({
            "Gruppo": str(g),
            "Cliente": str(cliente),
            "Prodotto": str(prodotto),
            "Tempo_totale_minuti": int(tempo_tot),
            "Stimata": str(stimata),
        })

    return consegne, piano_prod, piano_taglio

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
    st.subheader("⚙️ Capacità")
    st.info(
        f"**PRODUZIONE (minuti/giorno)**\n"
        f"• PVC: {CAPACITA_MINUTI_GIORNALIERA['PVC']} minuti\n"
        f"• Alluminio: {CAPACITA_MINUTI_GIORNALIERA['Alluminio']} minuti\n\n"
        f"**TAGLIO (strutture/giorno) - 2 macchine**\n"
        f"• PVC: Battente {CAP_TAGLIO_STRUTTURE_GIORNO['PVC']['Battente']}, "
        f"Scorrevole {CAP_TAGLIO_STRUTTURE_GIORNO['PVC']['Scorrevole']}, "
        f"Speciale {CAP_TAGLIO_STRUTTURE_GIORNO['PVC']['Struttura speciale']}\n"
        f"• Alluminio: Battente {CAP_TAGLIO_STRUTTURE_GIORNO['Alluminio']['Battente']}, "
        f"Scorrevole {CAP_TAGLIO_STRUTTURE_GIORNO['Alluminio']['Scorrevole']}, "
        f"Speciale {CAP_TAGLIO_STRUTTURE_GIORNO['Alluminio']['Struttura speciale']}\n\n"
        "Regole tempo PRODUZIONE:\n"
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
    data_inizio_taglio = st.date_input("Data inizio TAGLIO (gruppo)", value=prossimo_giorno_lavorativo(date.today()))

    st.markdown("### Aggiungi riga ordine")
    materiale = st.selectbox("Materiale riga", ["PVC", "Alluminio"])
    tipologia = st.selectbox("Tipologia riga", ["Battente", "Scorrevole", "Struttura speciale"])
    quantita_strutture = st.number_input("Quantità strutture (riga)", min_value=1, value=1, step=1)

    if tipologia == "Battente":
        vetri_totali = st.number_input(
            "Numero vetri TOTALI per questa riga (somma su tutte le strutture)",
            min_value=1, value=1, step=1
        )
    else:
        vetri_totali = 0

    minuti_riga, minuti_medi = minuti_preview(materiale, tipologia, int(quantita_strutture), int(vetri_totali))
    st.info(f"⏱️ Produzione riga: totale {minuti_riga} minuti (≈ {minuti_medi} min/struttura)")

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
        st.success(f"Totale PRODUZIONE (somma righe): {totale} minuti")
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
                    "data_inizio_taglio_gruppo": str(prossimo_giorno_lavorativo(data_inizio_taglio)),
                    "inserito_il": str(date.today()),
                }
                dati["ordini"].append(nuovo)

            salva_dati(dati)
            st.session_state["righe_correnti"] = []
            st.success(f"Ordine salvato (gruppo {ordine_gruppo}) - inizio TAGLIO: {prossimo_giorno_lavorativo(data_inizio_taglio)}")
            st.rerun()

st.divider()

# -----------------------------
# LISTA ORDINI
# -----------------------------
st.subheader("📋 Ordini (righe)")
if dati.get("ordini"):
    st.dataframe(dati["ordini"], use_container_width=True)
else:
    st.info("Nessun ordine inserito.")

# -----------------------------
# BOTTONI PRINCIPALI
# -----------------------------
c1, c2, c3 = st.columns([1, 1, 2])

with c1:
    if st.button("📅 Calcola piano"):
        consegne, piano_prod, piano_taglio = calcola_piano(dati)
        st.session_state["consegne"] = consegne
        st.session_state["piano_prod"] = piano_prod
        st.session_state["piano_taglio"] = piano_taglio

with c2:
    if st.button("🗑️ Cancella tutto"):
        dati = {"ordini": []}
        salva_dati(dati)
        st.session_state.pop("consegne", None)
        st.session_state.pop("piano_prod", None)
        st.session_state.pop("piano_taglio", None)
        st.session_state["righe_correnti"] = []
        st.warning("Ordini cancellati")
        st.rerun()

with c3:
    if st.button("🚪 Logout"):
        st.session_state.logged_in = False
        st.rerun()

# -----------------------------
# CONSEGNE
# -----------------------------
if "consegne" in st.session_state:
    st.subheader("✅ Consegne stimate (per gruppo) — fine produzione + 3 gg lavorativi")
    st.dataframe(st.session_state["consegne"], use_container_width=True)

# -----------------------------
# PIANO TAGLIO
# -----------------------------
if "piano_taglio" in st.session_state:
    st.subheader("✂️ Piano TAGLIO (strutture/giorno)")
    df_taglio = pd.DataFrame(st.session_state.get("piano_taglio", []))
    if df_taglio.empty:
        st.info("Nessun dato per il taglio.")
    else:
        st.dataframe(df_taglio, use_container_width=True)

# -----------------------------
# PIANO PRODUZIONE + SATURAZIONE
# -----------------------------
if "piano_prod" in st.session_state:
    st.subheader("🧾 Piano PRODUZIONE (spezzato a minuti) + Saturazione")

    df_p = pd.DataFrame(st.session_state.get("piano_prod", []))
    if df_p.empty:
        st.info("Nessun dato per la produzione.")
    else:
        df_p["Capacità"] = df_p["Materiale"].map(CAPACITA_MINUTI_GIORNALIERA).astype(float)

        used = (
            df_p.groupby(["Data", "Materiale"], as_index=False)["Minuti_prodotti"]
            .sum()
            .rename(columns={"Minuti_prodotti": "minuti_usati"})
        )

        df_p = df_p.merge(used, on=["Data", "Materiale"], how="left")
        df_p["Saturazione_%"] = (df_p["minuti_usati"] / df_p["Capacità"]) * 100
        df_p["Saturazione_%"] = df_p["Saturazione_%"].fillna(0).clip(0, 100)

        st.dataframe(
            df_p.drop(columns=["Capacità"], errors="ignore"),
            use_container_width=True,
            column_config={
                "Saturazione_%": st.column_config.ProgressColumn(
                    "Saturazione %",
                    min_value=0,
                    max_value=100,
                    format="%.0f%%",
                    help="Saturazione giornaliera della capacità produttiva (per giorno+materiale)",
                )
            },
        )

# -----------------------------
# SPOSTA INIZIO TAGLIO (inserimento nel residuo, il resto slitta)
# -----------------------------
st.divider()
st.subheader("📦 Sposta inizio TAGLIO commessa (usa residuo, poi slitta)")

if "consegne" in st.session_state and st.session_state.get("consegne"):
    gruppi = sorted({str(o["Gruppo"]) for o in st.session_state["consegne"]},
                    key=lambda x: int(x) if str(x).isdigit() else 10**9)

    g_sel = st.selectbox("Seleziona gruppo", gruppi, key="sposta_gruppo_taglio")
    nuova_data_taglio = st.date_input("Nuova data inizio TAGLIO", key="sposta_data_taglio", value=prossimo_giorno_lavorativo(date.today()))

    if st.button("📌 Applica spostamento TAGLIO"):
        piano_taglio_corrente = st.session_state.get("piano_taglio", [])
        piano_prod_corrente = st.session_state.get("piano_prod", [])

        nuova_data_ok = prossimo_giorno_lavorativo(nuova_data_taglio)

        # 1) Reinserisco TAGLIO del gruppo nei buchi
        try:
            add_taglio, last_cut_day = _insert_group_taglio_into_free_capacity(
                dati=dati,
                piano_taglio_base=piano_taglio_corrente,
                gruppo=g_sel,
                start_day=nuova_data_ok,
            )
        except ValueError as e:
            st.error(str(e))
            st.stop()

        nuovo_taglio = _merge_piani_keep_existing(piano_taglio_corrente, g_sel, add_taglio)

        # 2) Start produzione = giorno lavorativo dopo fine taglio del gruppo
        start_prod = aggiungi_giorno_lavorativo(prossimo_giorno_lavorativo(last_cut_day))

        # 3) Reinserisco PRODUZIONE del gruppo nei buchi (non distruttivo)
        try:
            add_prod, _ = _insert_group_prod_into_free_capacity(
                dati=dati,
                piano_prod_base=piano_prod_corrente,
                gruppo=g_sel,
                start_day=start_prod,
            )
        except ValueError as e:
            st.error(str(e))
            st.stop()

        nuovo_prod = _merge_piani_keep_existing(piano_prod_corrente, g_sel, add_prod)

        # 4) Salvo la nuova data nel JSON
        for o in dati["ordini"]:
            if str(o.get("ordine_gruppo")) == str(g_sel):
                o["data_inizio_taglio_gruppo"] = str(nuova_data_ok)
        salva_dati(dati)

        # 5) Aggiorno consegne (fine produzione + 3 gg lav) in modo coerente col nuovo piano produzione
        df_np = pd.DataFrame(nuovo_prod)
        consegne_new = []
        if not df_np.empty:
            df_np["Data_dt"] = pd.to_datetime(df_np["Data"]).dt.date
            for g in gruppi:
                righe_g = [o for o in dati.get("ordini", []) if str(o.get("ordine_gruppo")) == str(g)]
                cliente = righe_g[0].get("cliente", "") if righe_g else ""
                prodotto = righe_g[0].get("prodotto", "") if righe_g else ""
                tempo_tot = sum(int(o.get("tempo_minuti", 0) or 0) for o in righe_g)

                sub = df_np[df_np["Gruppo"].astype(str) == str(g)]
                if sub.empty:
                    stimata = prossimo_giorno_lavorativo(date.today())
                else:
                    fine = max(sub["Data_dt"])
                    d = prossimo_giorno_lavorativo(fine)
                    for _ in range(3):
                        d = aggiungi_giorno_lavorativo(d)
                    stimata = d

                consegne_new.append({
                    "Gruppo": str(g),
                    "Cliente": str(cliente),
                    "Prodotto": str(prodotto),
                    "Tempo_totale_minuti": int(tempo_tot),
                    "Stimata": str(stimata),
                })

        st.session_state["piano_taglio"] = nuovo_taglio
        st.session_state["piano_prod"] = nuovo_prod
        st.session_state["consegne"] = consegne_new

        st.success(f"✅ Gruppo {g_sel} spostato al TAGLIO {nuova_data_ok} (se c'è spazio anche parziale, inserisce e slitta il resto).")
        st.rerun()

# -----------------------------
# GANTT PRODUZIONE (giorno per giorno)
# -----------------------------
st.divider()
st.subheader("📊 Gantt Produzione (giorno per giorno)")

df = pd.DataFrame(st.session_state.get("piano_prod", []))
if df.empty:
    st.info("Nessun dato per il Gantt.")
else:
    df["Data"] = pd.to_datetime(df["Data"])
    df = df[df["Data"].dt.weekday < 5].copy()
    df["Giorno"] = df["Data"].dt.strftime("%d/%m")

    df["Commessa"] = (
        "Gruppo " + df["Gruppo"].astype(str)
        + " | " + df["Cliente"].astype(str)
        + " | " + df["Prodotto"].astype(str)
    )

    agg = (
        df.groupby(["Giorno", "Commessa", "Gruppo", "Cliente", "Prodotto"], as_index=False)
          .agg(minuti=("Minuti_prodotti", "sum"))
    )

    min_d = df["Data"].min().normalize()
    max_d = df["Data"].max().normalize()

    all_days = pd.date_range(start=min_d, end=max_d, freq="B")
    giorni_ordinati = [d.strftime("%d/%m") for d in all_days]
    df_days = pd.DataFrame({"Giorno": giorni_ordinati})

    agg["label_base"] = (
        "G" + agg["Gruppo"].astype(str)
        + " | " + agg["Cliente"].astype(str)
        + " | " + agg["Prodotto"].astype(str)
    )

    colA, colB, colC = st.columns([1, 1, 2])
    with colA:
        show_minutes = st.checkbox("Mostra minuti nel box", value=False, key="gantt_show_minutes")
    with colB:
        ordina = st.selectbox("Ordina righe", ["Per Gruppo", "Per Cliente"], index=0, key="gantt_ordina")
    with colC:
        st.caption("Ogni rettangolo = 1 giorno lavorativo di produzione per una commessa.")

    if show_minutes:
        agg["label"] = agg["label_base"] + "\n" + agg["minuti"].astype(int).astype(str) + " min"
    else:
        agg["label"] = agg["label_base"]

    if ordina == "Per Gruppo":
        sort_y = alt.SortField(field="Gruppo", order="ascending")
    else:
        sort_y = alt.SortField(field="Cliente", order="ascending")

    base = alt.Chart(agg).encode(
        y=alt.Y(
            "Commessa:N",
            sort=sort_y,
            title="Commesse",
            axis=alt.Axis(labelFontSize=12, labelLimit=500, titleFontSize=13),
            scale=alt.Scale(paddingInner=0.35, paddingOuter=0.15)
        ),
        x=alt.X(
            "Giorno:N",
            sort=giorni_ordinati,
            scale=alt.Scale(domain=giorni_ordinati),
            title="Giorni (solo lavorativi)",
            axis=alt.Axis(labelAngle=0, labelFontSize=12, titleFontSize=13)
        ),
        tooltip=[
            alt.Tooltip("Giorno:N", title="Giorno"),
            alt.Tooltip("Commessa:N", title="Commessa"),
            alt.Tooltip("minuti:Q", title="Minuti prodotti"),
        ],
    )

    ghost = alt.Chart(df_days).mark_point(opacity=0).encode(
        x=alt.X("Giorno:N", sort=giorni_ordinati, scale=alt.Scale(domain=giorni_ordinati))
    )

    bars = base.mark_bar(cornerRadius=10).encode(
        color=alt.Color("Cliente:N", legend=alt.Legend(title="Cliente"))
    )

    text = alt.Chart(agg).mark_text(
        align="center",
        baseline="middle",
        fontSize=12,
        lineBreak="\n",
    ).encode(
        y=alt.Y("Commessa:N", sort=sort_y),
        x=alt.X("Giorno:N", sort=giorni_ordinati),
        text="label:N",
    )

    chart = (ghost + bars + text).properties(
        height=max(380, 70 * len(agg["Commessa"].unique())),
    )

    st.altair_chart(chart, use_container_width=True)



















