import streamlit as st
from datetime import date, timedelta
import json
import os
import hashlib
import pandas as pd
import altair as alt
import streamlit.components.v1 as components
from pathlib import Path
import math

# =========================
# FILE DATI
# =========================
FILE_DATI = "dati_produzione.json"

# Capacità giornaliera fissa per materiale (minuti/giorno)
CAPACITA_MINUTI_GIORNALIERA = {
    "PVC": 4500,
    "Alluminio": 3000
}

MINUTI_8_ORE = 8 * 60  # 480

# =========================
# TAGLIO (due macchine: PVC e Alluminio)
# Vincoli giornalieri (pezzi/giorno) in base alla tipologia
# =========================
TAGLIO_MAX_PEZZI_GIORNO = {
    "Battente": 15,
    "Scorrevole": 10,
    "Struttura speciale": 5
}

# NON posso tagliare 2 commesse lo stesso giorno sulla stessa macchina
# (anche se finisce presto, il giorno resta “bloccato” per quella commessa).

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
    while d.weekday() >= 5:  # 5=sabato, 6=domenica
        d = d + timedelta(days=1)
    return d

def aggiungi_giorno_lavorativo(d: date) -> date:
    return prossimo_giorno_lavorativo(d + timedelta(days=1))

def aggiungi_n_giorni_lavorativi(d: date, n: int) -> date:
    d = prossimo_giorno_lavorativo(d)
    for _ in range(n):
        d = aggiungi_giorno_lavorativo(d)
    return d

def safe_date(s) -> date:
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
    return {"capacita_giornaliera": 0, "ordini": []}

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
        return max(0, int(t))

    return max(0, int(quantita_strutture) * MINUTI_8_ORE)

def minuti_preview(materiale: str, tipologia: str, quantita_strutture: int, vetri_totali: int):
    tot = tempo_riga(materiale, tipologia, quantita_strutture, vetri_totali)
    if quantita_strutture > 0:
        return tot, int(round(tot / quantita_strutture))
    return tot, tot

# =========================
# UTILS: aggregazioni commesse
# =========================
def _group_orders(ordini):
    gruppi_map = {}
    for o in ordini:
        g = o.get("ordine_gruppo", "0")
        if g is None or str(g).strip() == "":
            g = "0"
        g = str(g)
        gruppi_map.setdefault(g, []).append(o)
    return gruppi_map

def _gruppo_sort_key(gruppi_map, g: str):
    righe = gruppi_map[g]
    d0 = righe[0].get("data_inizio_gruppo", righe[0].get("data_richiesta", str(date.today())))
    start = safe_date(d0)
    try:
        gnum = int(g)
    except Exception:
        gnum = 0
    return (start, gnum, g)

# =========================
# TAGLIO: calcolo calendario taglio per materiale (2 macchine)
# =========================
def _calcola_taglio(dati):
    """
    Ritorna:
      taglio_calendar: list[dict] (righe giorno per giorno)
      taglio_fine: dict[mat][gruppo] -> last_day_cut (date)
      taglio_giorni_occupati: dict[mat] -> set(str(date)) con giorni occupati (per check spostamento)
    """
    ordini = dati.get("ordini", [])
    if not ordini:
        return [], {"PVC": {}, "Alluminio": {}}, {"PVC": set(), "Alluminio": set()}

    gruppi_map = _group_orders(ordini)
    gruppi_ordinati = sorted(gruppi_map.keys(), key=lambda g: _gruppo_sort_key(gruppi_map, g))

    oggi = prossimo_giorno_lavorativo(date.today())

    # stato per macchina: giorno corrente
    stato = {
        "PVC": {"giorno": oggi},
        "Alluminio": {"giorno": oggi},
    }

    taglio_calendar = []
    taglio_fine = {"PVC": {}, "Alluminio": {}}
    giorni_occupati = {"PVC": set(), "Alluminio": set()}

    def cut_capacity(tipologia: str) -> int:
        tipologia = (tipologia or "").strip()
        return int(TAGLIO_MAX_PEZZI_GIORNO.get(tipologia, 10))

    for g in gruppi_ordinati:
        righe = gruppi_map[g]

        # base info (per tabella)
        base = righe[0] if righe else {}

        # per ogni materiale, taglio indipendente su sua macchina
        for mat in ("PVC", "Alluminio"):
            righe_mat = [r for r in righe if (r.get("materiale", "PVC") == mat)]
            if not righe_mat:
                continue

            # start taglio = max(stato macchina, data_inizio_gruppo)
            d0 = base.get("data_inizio_gruppo", base.get("data_richiesta", str(oggi)))
            start_group = prossimo_giorno_lavorativo(safe_date(d0))

            giorno = stato[mat]["giorno"]
            if giorno < start_group:
                giorno = start_group

            # Non posso fare 2 commesse nello stesso giorno su quella macchina:
            # quindi quando inizio una commessa, i giorni che userà restano "solo sua".
            # Se finisce in mezzo al giorno, quel giorno non viene riusato da altre commesse.

            # Pianifico riga per riga, consumando capacità giornaliera in pezzi,
            # ma sempre dentro la stessa commessa (quindi posso usare residuo per altre righe della stessa commessa).
            remaining_per_riga = []
            for r in righe_mat:
                qta = int(r.get("quantita_strutture", 0) or 0)
                tip = (r.get("tipologia", "") or "").strip()
                remaining_per_riga.append({"tipologia": tip, "remaining": max(0, qta)})

            # se non c'è niente da tagliare, metto fine taglio = giorno-1 (ma per sicurezza giorno)
            if sum(x["remaining"] for x in remaining_per_riga) <= 0:
                taglio_fine[mat][g] = giorno
                continue

            # scorro giorni finché finisco tutti i pezzi
            while True:
                giorno = prossimo_giorno_lavorativo(giorno)

                cap_used_detail = []
                # capacità totale del giorno NON è unica: dipende dalla tipologia.
                # Quindi faccio greedy: taglio prima le righe nell'ordine inserimento.
                # Ogni tipologia ha il suo "massimo al giorno", ma essendo 1 sola commessa in macchina,
                # usiamo la regola più semplice: il giorno è dedicato alla commessa, ma se ha tipologie miste,
                # il massimo effettivo lo applichiamo PER tipologia dentro il giorno.
                # (così non “bara” tagliando 15 battenti + 10 scorrevoli nello stesso giorno: resti comunque dentro i massimali per tipo)
                caps = {"Battente": 0, "Scorrevole": 0, "Struttura speciale": 0}
                caps_max = {k: cut_capacity(k) for k in caps.keys()}

                # taglio sulle righe
                any_remaining = False
                for rr in remaining_per_riga:
                    if rr["remaining"] <= 0:
                        continue
                    any_remaining = True

                    tip = rr["tipologia"]
                    if tip not in caps:
                        # tip sconosciuta: tratto come scorrevole (10)
                        tip = "Scorrevole"

                    free = caps_max[tip] - caps[tip]
                    if free <= 0:
                        continue

                    take = min(free, rr["remaining"])
                    rr["remaining"] -= take
                    caps[tip] += take
                    if take > 0:
                        cap_used_detail.append(f"{tip}:{take}")

                # scrivo una riga di calendario taglio solo se ho tagliato qualcosa
                if sum(caps.values()) > 0:
                    giorni_occupati[mat].add(str(giorno))
                    taglio_calendar.append({
                        "Data": str(giorno),
                        "Fase": "Taglio",
                        "Gruppo": g,
                        "Cliente": base.get("cliente", ""),
                        "Prodotto": base.get("prodotto", ""),
                        "Materiale": mat,
                        "Battenti_tagliati": int(caps["Battente"]),
                        "Scorrevoli_tagliati": int(caps["Scorrevole"]),
                        "Speciali_tagliati": int(caps["Struttura speciale"]),
                    })

                # finito tutto?
                if all(rr["remaining"] <= 0 for rr in remaining_per_riga):
                    taglio_fine[mat][g] = giorno
                    # prossima commessa sulla macchina: giorno lavorativo successivo
                    stato[mat]["giorno"] = aggiungi_giorno_lavorativo(giorno)
                    break

                # se non ho tagliato nulla (giorno “inutile”), vado al prossimo giorno
                if not any_remaining:
                    taglio_fine[mat][g] = giorno
                    stato[mat]["giorno"] = aggiungi_giorno_lavorativo(giorno)
                    break

                giorno = aggiungi_giorno_lavorativo(giorno)

    return taglio_calendar, taglio_fine, giorni_occupati

# =========================
# PRODUZIONE: pianificazione work-conserving per materiale
# (ma con vincolo: non prima di fine taglio del materiale per quel gruppo)
# =========================
def calcola_piano(dati):
    ordini = dati.get("ordini", [])
    if not ordini:
        return [], [], []

    oggi = prossimo_giorno_lavorativo(date.today())

    # 1) TAGLIO
    taglio_calendar, taglio_fine, _giorni_occupati = _calcola_taglio(dati)

    # 2) preparo tasks produzione (per riga ordine)
    tasks = []
    for o in ordini:
        g = str(o.get("ordine_gruppo", "0") or "0")

        materiale = o.get("materiale", "PVC")
        if materiale not in ("PVC", "Alluminio"):
            materiale = "PVC"

        tempo = int(o.get("tempo_minuti", 0) or 0)
        tempo = max(0, tempo)

        qta_strutture = int(o.get("quantita_strutture", 0) or 0)
        tipologia = (o.get("tipologia", "") or "").strip()

        # start produzione = max(data_inizio_gruppo, (fine taglio materiale gruppo + 1 workday))
        base_start = prossimo_giorno_lavorativo(safe_date(o.get("data_inizio_gruppo", str(oggi))))
        cut_end = taglio_fine.get(materiale, {}).get(g, base_start)
        prod_after_cut = aggiungi_giorno_lavorativo(prossimo_giorno_lavorativo(cut_end))
        start_prod = max(base_start, prod_after_cut)

        tasks.append({
            "ref": o,
            "ordine_id": o.get("id", ""),
            "gruppo": g,
            "cliente": o.get("cliente", ""),
            "prodotto": o.get("prodotto", ""),
            "materiale": materiale,
            "tipologia": tipologia,
            "vetri_totali": o.get("vetri_totali", ""),
            "qta_strutture": qta_strutture,
            "tempo_totale": tempo,
            "remaining": tempo,
            "start_prod": start_prod,
        })

    # ordinamento: start_prod, gruppo, id
    def key_task(t):
        try:
            gnum = int(t["gruppo"])
        except Exception:
            gnum = 0
        try:
            oid = int(t["ordine_id"])
        except Exception:
            oid = 0
        return (t["start_prod"], gnum, oid)

    tasks.sort(key=key_task)

    # stato per materiale (minuti)
    stato = {
        "PVC": {"giorno": oggi, "usati": 0, "cap": int(CAPACITA_MINUTI_GIORNALIERA["PVC"])},
        "Alluminio": {"giorno": oggi, "usati": 0, "cap": int(CAPACITA_MINUTI_GIORNALIERA["Alluminio"])},
    }

    piano = []
    fine_per_gruppo = {}
    baseinfo_per_gruppo = {}
    tempotot_per_gruppo = {}

    tasks_by_mat = {"PVC": [], "Alluminio": []}
    for t in tasks:
        tasks_by_mat[t["materiale"]].append(t)

    def schedule_material(mat: str):
        cap = stato[mat]["cap"]
        giorno = stato[mat]["giorno"]
        usati = stato[mat]["usati"]
        pending = tasks_by_mat[mat]

        while True:
            eligible = [t for t in pending if t["remaining"] > 0 and t["start_prod"] <= giorno]
            if not eligible:
                future = [t for t in pending if t["remaining"] > 0]
                if not future:
                    break
                next_start = min(t["start_prod"] for t in future)
                if giorno < next_start:
                    giorno = prossimo_giorno_lavorativo(next_start)
                    usati = 0
                    continue
                giorno = aggiungi_giorno_lavorativo(giorno)
                usati = 0
                continue

            eligible.sort(key=key_task)
            t = eligible[0]

            disponibili = cap - usati
            if disponibili <= 0:
                giorno = aggiungi_giorno_lavorativo(giorno)
                usati = 0
                continue

            lavoro = min(disponibili, t["remaining"])
            t["remaining"] -= lavoro
            usati += lavoro

            strutture_oggi = 0.0
            if t["tempo_totale"] > 0 and t["qta_strutture"] > 0:
                strutture_oggi = (lavoro / t["tempo_totale"]) * t["qta_strutture"]

            piano.append({
                "Data": str(giorno),
                "Fase": "Produzione",
                "Ordine": t["ordine_id"],
                "Gruppo": t["gruppo"],
                "Cliente": t["cliente"],
                "Prodotto": t["prodotto"],
                "Materiale": mat,
                "Tipologia": t["tipologia"],
                "Qta_strutture": t["qta_strutture"],
                "Vetri_totali": t["vetri_totali"],
                "Minuti_prodotti": int(lavoro),
                "Minuti_residui_materiale": int(cap - usati),
                "Strutture_prodotte": round(strutture_oggi, 2),
            })

            baseinfo_per_gruppo.setdefault(t["gruppo"], {"Cliente": t["cliente"], "Prodotto": t["prodotto"]})

            if t["remaining"] <= 0:
                t["ref"]["consegna_stimata"] = str(giorno)
                if t["gruppo"] not in fine_per_gruppo:
                    fine_per_gruppo[t["gruppo"]] = giorno
                else:
                    fine_per_gruppo[t["gruppo"]] = max(fine_per_gruppo[t["gruppo"]], giorno)

            if usati >= cap:
                giorno = aggiungi_giorno_lavorativo(giorno)
                usati = 0

        stato[mat]["giorno"] = giorno
        stato[mat]["usati"] = usati

    schedule_material("PVC")
    schedule_material("Alluminio")

    # tempo totale per gruppo
    for o in ordini:
        g = str(o.get("ordine_gruppo", "0") or "0")
        tempotot_per_gruppo[g] = tempotot_per_gruppo.get(g, 0) + int(o.get("tempo_minuti", 0) or 0)

    # consegne per gruppo: fine produzione + 3 giorni lavorativi
    consegne = []
    for g, fine in fine_per_gruppo.items():
        info = baseinfo_per_gruppo.get(g, {"Cliente": "", "Prodotto": ""})
        stimata = aggiungi_n_giorni_lavorativi(prossimo_giorno_lavorativo(fine), 3)
        consegne.append({
            "Gruppo": g,
            "Cliente": info.get("Cliente", ""),
            "Prodotto": info.get("Prodotto", ""),
            "Tempo_totale_minuti": int(tempotot_per_gruppo.get(g, 0)),
            "Stimata": str(stimata),
        })

    def grp_key(x):
        try:
            return int(x.get("Gruppo", 0))
        except Exception:
            return 0

    consegne.sort(key=grp_key)

    salva_dati(dati)
    return consegne, piano, taglio_calendar

# =========================
# INSERIMENTO NON DISTRUTTIVO
# (trova primo start (taglio) disponibile senza spostare i già inseriti)
# =========================
def _build_cut_occupied_from_calendar(taglio_calendar):
    occ = {"PVC": set(), "Alluminio": set()}
    for r in taglio_calendar or []:
        if r.get("Fase") != "Taglio":
            continue
        mat = r.get("Materiale", "PVC")
        d = str(r.get("Data"))
        if mat in occ:
            occ[mat].add(d)
    return occ

def _sum_pieces_per_material_by_tip(righe_correnti):
    """
    Ritorna: need[mat][tip] = pezzi (usiamo quantita_strutture come pezzi da tagliare)
    """
    need = {"PVC": {"Battente": 0, "Scorrevole": 0, "Struttura speciale": 0},
            "Alluminio": {"Battente": 0, "Scorrevole": 0, "Struttura speciale": 0}}
    for r in righe_correnti:
        mat = r.get("materiale", "PVC")
        tip = (r.get("tipologia", "") or "").strip()
        qta = int(r.get("quantita_strutture", 0) or 0)
        if mat not in need:
            mat = "PVC"
        if tip not in need[mat]:
            # tip sconosciuta -> la tratto come Scorrevole
            tip = "Scorrevole"
        need[mat][tip] += max(0, qta)
    return need

def _compute_cut_days_for_material(need_mat):
    """
    need_mat: dict tip->qta
    Ritorna numero giorni (>=0) necessari per il taglio su quella macchina.
    Rispetta i massimali per tipologia per giorno.
    """
    if sum(need_mat.values()) <= 0:
        return 0

    # giorni = massimo tra ceil(qta_tip / cap_tip) per ciascuna tip
    # perché nello stesso giorno puoi fare tagli di più tipologie (ma ciascuna col suo massimo)
    # e la giornata resta dedicata alla commessa comunque.
    days = 0
    for tip, qta in need_mat.items():
        cap = int(TAGLIO_MAX_PEZZI_GIORNO.get(tip, 10))
        if qta > 0:
            days = max(days, int(math.ceil(qta / cap)))
    return days

def _find_first_start_date_without_moving_existing(dati_esistenti, righe_correnti, from_date=None):
    if from_date is None:
        from_date = date.today()

    # calcolo taglio attuale per sapere i giorni occupati (PVC/ALL)
    _, _, taglio_calendar = calcola_piano(dati_esistenti)
    occupied = _build_cut_occupied_from_calendar(taglio_calendar)

    need = _sum_pieces_per_material_by_tip(righe_correnti)
    need_days = {
        "PVC": _compute_cut_days_for_material(need["PVC"]),
        "Alluminio": _compute_cut_days_for_material(need["Alluminio"]),
    }

    d = prossimo_giorno_lavorativo(from_date)

    for _ in range(365):
        ok = True
        for mat in ("PVC", "Alluminio"):
            days_needed = need_days[mat]
            if days_needed <= 0:
                continue

            # verifico se da d per days_needed giorni lavorativi, quei giorni sono liberi per la macchina
            cur = d
            for _k in range(days_needed):
                cur = prossimo_giorno_lavorativo(cur)
                if str(cur) in occupied.get(mat, set()):
                    ok = False
                    break
                cur = aggiungi_giorno_lavorativo(cur)
            if not ok:
                break

        if ok:
            return d

        d = aggiungi_giorno_lavorativo(d)

    # fallback: metto in coda (giorno dopo ultimo taglio)
    last = None
    for mat in ("PVC", "Alluminio"):
        if occupied.get(mat):
            mx = max(safe_date(x) for x in occupied[mat])
            last = mx if last is None else max(last, mx)
    if last:
        return aggiungi_giorno_lavorativo(last)

    return prossimo_giorno_lavorativo(date.today())

# =========================
# CHECK SPOSTAMENTO: se il giorno taglio è già occupato per tutte le macchine necessarie -> ALERT
# =========================
def check_spazio_primo_giorno_taglio(dati, gruppo_sel: str, nuova_data: date):
    if nuova_data is None:
        return False, "Seleziona una data valida."

    nuova_data = prossimo_giorno_lavorativo(nuova_data)
    day_str = str(nuova_data)

    righe_gruppo = [o for o in dati.get("ordini", []) if str(o.get("ordine_gruppo")) == str(gruppo_sel)]
    if not righe_gruppo:
        return False, f"Gruppo {gruppo_sel} non trovato."

    materiali_necessari = sorted({(r.get("materiale", "PVC") if r.get("materiale", "PVC") in ("PVC","Alluminio") else "PVC")
                                  for r in righe_gruppo})

    # ricostruisco calendario taglio attuale
    _, _, taglio_calendar = calcola_piano(dati)
    occupied = _build_cut_occupied_from_calendar(taglio_calendar)

    # escludo il gruppo stesso: se già era su quel giorno, lo considero “libero” per lui
    for mat in ("PVC", "Alluminio"):
        occ2 = set()
        for r in taglio_calendar:
            if r.get("Fase") != "Taglio":
                continue
            if r.get("Materiale") != mat:
                continue
            if str(r.get("Gruppo")) == str(gruppo_sel):
                continue
            occ2.add(str(r.get("Data")))
        occupied[mat] = occ2

    # se tutte le macchine necessarie sono occupate quel giorno -> no spazio
    if all(day_str in occupied.get(mat, set()) for mat in materiali_necessari):
        det = ", ".join([f"{mat}: occupata" for mat in materiali_necessari])
        return False, f"❌ Non c'è spazio al TAGLIO il {day_str} ({det})."

    return True, ""

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
    st.subheader("⚙️ Capacità e regole")

    st.info(
        f"**Produzione (minuti/giorno):**\n"
        f"• PVC: {CAPACITA_MINUTI_GIORNALIERA['PVC']} min\n"
        f"• Alluminio: {CAPACITA_MINUTI_GIORNALIERA['Alluminio']} min\n\n"
        f"**Taglio (pezzi/giorno, 2 macchine separate):**\n"
        f"• Battente: 15\n"
        f"• Scorrevole: 10\n"
        f"• Struttura speciale: 5\n"
        f"⚠️ Non si tagliano **2 commesse nello stesso giorno** sulla stessa macchina.\n\n"
        "Regole tempo produzione:\n"
        "• Battente: 90 min/vetro (Alluminio +30 min/struttura)\n"
        "• Scorrevole: 480 min/struttura\n"
        "• Speciale: 480 min/struttura\n\n"
        "📦 Consegna stimata = **fine produzione + 3 giorni lavorativi**."
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
            min_value=1, value=1, step=1
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

            # trova data inizio TAGLIO senza spostare le altre commesse
            start_suggerito = _find_first_start_date_without_moving_existing(
                dati_esistenti=dati,
                righe_correnti=st.session_state["righe_correnti"],
                from_date=date.today()
            )

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
                    # questa guida TAGLIO (poi produzione partirà dopo taglio)
                    "data_inizio_gruppo": str(start_suggerito),
                    "inserito_il": str(date.today())
                }
                dati["ordini"].append(nuovo)

            salva_dati(dati)
            st.session_state["righe_correnti"] = []
            st.success(f"Ordine salvato (gruppo {ordine_gruppo}) - taglio da: {start_suggerito}")

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
        consegne, piano, taglio_calendar = calcola_piano(dati)
        st.session_state["consegne"] = consegne
        st.session_state["piano"] = piano
        st.session_state["taglio"] = taglio_calendar

with c2:
    if st.button("🗑️ Cancella tutto"):
        dati = {"capacita_giornaliera": dati.get("capacita_giornaliera", 0), "ordini": []}
        salva_dati(dati)
        st.session_state.pop("consegne", None)
        st.session_state.pop("piano", None)
        st.session_state.pop("taglio", None)
        st.session_state["righe_correnti"] = []
        st.warning("Ordini cancellati")

with c3:
    if st.button("🚪 Logout"):
        st.session_state.logged_in = False
        st.rerun()

# -----------------------------
# CONSEGNE + PIANO
# -----------------------------
if "consegne" in st.session_state:
    st.subheader("✅ Consegne stimate (per gruppo)")
    st.dataframe(st.session_state["consegne"], use_container_width=True)

if "piano" in st.session_state:
    st.subheader("🧾 Piano produzione giorno per giorno (spezzato a minuti)")

    df_piano = pd.DataFrame(st.session_state.get("piano", []))
    if df_piano.empty:
        st.info("Nessun dato per il piano.")
    else:
        # Saturazione (0-100) per giorno+materiale
        df_piano["Capacità"] = df_piano["Materiale"].map(CAPACITA_MINUTI_GIORNALIERA).astype(float)

        used = (
            df_piano.groupby(["Data", "Materiale"], as_index=False)["Minuti_prodotti"]
            .sum()
            .rename(columns={"Minuti_prodotti": "minuti_usati"})
        )
        df_piano = df_piano.merge(used, on=["Data", "Materiale"], how="left")
        df_piano["Saturazione_%"] = (df_piano["minuti_usati"] / df_piano["Capacità"]) * 100
        df_piano["Saturazione_%"] = df_piano["Saturazione_%"].fillna(0).clip(0, 100)

        # simbolo saturazione
        def sat_icon(x):
            try:
                x = float(x)
            except Exception:
                x = 0.0
            if x >= 95:
                return "🔴"
            if x >= 70:
                return "🟠"
            if x >= 40:
                return "🟡"
            return "🟢"

        df_piano["Sat"] = df_piano["Saturazione_%"].apply(sat_icon)

        # rimuovo colonne tecniche se vuoi (lasciamo minuti_usati/capacità fuori vista)
        df_show = df_piano.drop(columns=["minuti_usati", "Capacità"], errors="ignore")

        st.dataframe(
            df_show,
            use_container_width=True,
            column_config={
                "Sat": st.column_config.TextColumn("Sat", help="Indicatore saturazione (verde->rosso)"),
                "Saturazione_%": st.column_config.ProgressColumn(
                    "Saturazione %",
                    min_value=0,
                    max_value=100,
                    format="%.0f%%",
                    help="Saturazione giornaliera della capacità produttiva (per giorno+materiale)",
                )
            },
        )

    st.subheader("📦 Sposta inizio TAGLIO commessa")

if "consegne" in st.session_state:
    gruppi = sorted({str(o["Gruppo"]) for o in st.session_state["consegne"]})
    g_sel = st.selectbox("Seleziona gruppo", gruppi)
    nuova_data = st.date_input("Nuova data inizio TAGLIO")

    if st.button("📌 Applica spostamento"):
        ok_spazio, msg = check_spazio_primo_giorno_taglio(dati, g_sel, nuova_data)
        if not ok_spazio:
            st.error(msg)
            st.stop()

        nuova_data_ok = prossimo_giorno_lavorativo(nuova_data)

        for o in dati["ordini"]:
            if str(o.get("ordine_gruppo")) == str(g_sel):
                o["data_inizio_gruppo"] = str(nuova_data_ok)

        salva_dati(dati)

        consegne, piano, taglio_calendar = calcola_piano(dati)
        st.session_state["consegne"] = consegne
        st.session_state["piano"] = piano
        st.session_state["taglio"] = taglio_calendar

        st.success(f"✅ Gruppo {g_sel} spostato al {nuova_data_ok} (taglio). Produzione partirà dopo il taglio.")
        st.rerun()

    # =========================
    # GANTT PRODUZIONE (lun-ven + giorni continui)
    # =========================
    st.subheader("📊 Gantt Produzione (giorno per giorno)")

    df = pd.DataFrame(st.session_state.get("piano", []))
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
              .agg(
                  strutture=("Strutture_prodotte", "sum"),
                  minuti=("Minuti_prodotti", "sum")
              )
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
            agg["label"] = (
                agg["label_base"]
                + "\n"
                + agg["strutture"].round(1).astype(str)
                + " strutt. | "
                + agg["minuti"].astype(int).astype(str)
                + " min"
            )
        else:
            agg["label"] = (
                agg["label_base"]
                + "\n"
                + agg["strutture"].round(1).astype(str)
                + " strutt."
            )

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
                alt.Tooltip("strutture:Q", title="Strutture prodotte"),
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
            fontSize=13,
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





































