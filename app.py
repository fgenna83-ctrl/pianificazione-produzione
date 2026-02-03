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

# Capacità giornaliera fissa per materiale (minuti/giorno)
CAPACITA_MINUTI_GIORNALIERA = {
    "PVC": 4500,
    "Alluminio": 3000
}

MINUTI_8_ORE = 8 * 60  # 480

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
    tipologia = tipologia.strip()

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
# PIANIFICAZIONE (NO WEEKEND)
# =========================
def calcola_piano(dati):
    ordini = dati.get("ordini", [])
    if not ordini:
        return [], []

    oggi = prossimo_giorno_lavorativo(date.today())

    def safe_date(s):
        try:
            return date.fromisoformat(str(s))
        except Exception:
            return oggi

    # --- Preparo righe (task) per materiale ---
    # Ogni riga è un "lavoro" con tot minuti da consumare su quel materiale
    tasks = []
    for o in ordini:
        g = str(o.get("ordine_gruppo", "0") or "0")
        start_g = safe_date(o.get("data_inizio_gruppo", o.get("data_richiesta", str(oggi))))
        materiale = o.get("materiale", "PVC")
        if materiale not in ("PVC", "Alluminio"):
            materiale = "PVC"

        tempo = int(o.get("tempo_minuti", 0) or 0)
        if tempo < 0:
            tempo = 0

        qta_strutture = int(o.get("quantita_strutture", 0) or 0)

        tasks.append({
            "ref": o,  # riferimento all'ordine originale (così aggiorno consegna_stimata)
            "ordine_id": o.get("id", ""),
            "gruppo": g,
            "cliente": o.get("cliente", ""),
            "prodotto": o.get("prodotto", ""),
            "materiale": materiale,
            "tipologia": o.get("tipologia", ""),
            "vetri_totali": o.get("vetri_totali", ""),
            "qta_strutture": qta_strutture,
            "tempo_totale": tempo,
            "remaining": tempo,
            "start_group": prossimo_giorno_lavorativo(start_g),
        })

    # Ordinamento “naturale”: prima chi ha start_group più vecchia, poi gruppo, poi id
    def key_task(t):
        try:
            gnum = int(t["gruppo"])
        except Exception:
            gnum = 0
        try:
            oid = int(t["ordine_id"])
        except Exception:
            oid = 0
        return (t["start_group"], gnum, oid)

    tasks.sort(key=key_task)

    # Stato per materiale
    stato = {
        "PVC": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["PVC"]},
        "Alluminio": {"giorno": oggi, "usati": 0, "cap": CAPACITA_MINUTI_GIORNALIERA["Alluminio"]},
    }

    piano = []
    fine_per_gruppo = {}  # gruppo -> data fine (massima)
    baseinfo_per_gruppo = {}  # gruppo -> {cliente, prodotto}
    tempotot_per_gruppo = {}  # gruppo -> minuti tot

    # Per velocizzare: separo tasks per materiale
    tasks_by_mat = {"PVC": [], "Alluminio": []}
    for t in tasks:
        tasks_by_mat[t["materiale"]].append(t)

    # Pianifica un materiale in modo “work-conserving”
    def schedule_material(mat: str):
        cap = stato[mat]["cap"]
        giorno = stato[mat]["giorno"]
        usati = stato[mat]["usati"]

        pending = tasks_by_mat[mat]

        # finché ci sono task su questo materiale
        while True:
            # task eleggibili oggi (start_group <= oggi e remaining > 0)
            eligible = [t for t in pending if t["remaining"] > 0 and t["start_group"] <= giorno]

            if not eligible:
                # se non c'è nulla oggi, salto al prossimo start_group disponibile
                future = [t for t in pending if t["remaining"] > 0]
                if not future:
                    break  # finito tutto
                next_start = min(t["start_group"] for t in future)
                if giorno < next_start:
                    giorno = prossimo_giorno_lavorativo(next_start)
                    usati = 0
                    continue
                # fallback
                giorno = aggiungi_giorno_lavorativo(giorno)
                usati = 0
                continue

            # prendo il primo eleggibile (priorità naturale)
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

            # strutture prodotte oggi (stima proporzionale)
            strutture_oggi = 0.0
            if t["tempo_totale"] > 0 and t["qta_strutture"] > 0:
                strutture_oggi = (lavoro / t["tempo_totale"]) * t["qta_strutture"]

            piano.append({
                "Data": str(giorno),
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

            # aggiorno info gruppo
            baseinfo_per_gruppo.setdefault(t["gruppo"], {"Cliente": t["cliente"], "Prodotto": t["prodotto"]})
            tempotot_per_gruppo[t["gruppo"]] = tempotot_per_gruppo.get(t["gruppo"], 0) + 0  # lo setto dopo

            # se ho finito questo task, consegna stimata riga = oggi (fine su quel materiale)
            if t["remaining"] <= 0:
                t["ref"]["consegna_stimata"] = str(giorno)
                # fine gruppo = max delle righe
                if t["gruppo"] not in fine_per_gruppo:
                    fine_per_gruppo[t["gruppo"]] = giorno
                else:
                    fine_per_gruppo[t["gruppo"]] = max(fine_per_gruppo[t["gruppo"]], giorno)

            # se ho esaurito cap, domani
            if usati >= cap:
                giorno = aggiungi_giorno_lavorativo(giorno)
                usati = 0

        # salvo stato
        stato[mat]["giorno"] = giorno
        stato[mat]["usati"] = usati

    # pianifico PVC e Alluminio indipendenti (non si bloccano a vicenda)
    schedule_material("PVC")
    schedule_material("Alluminio")

    # tempo totale per gruppo (somma righe originali, come prima)
    for o in ordini:
        g = str(o.get("ordine_gruppo", "0") or "0")
        tempotot_per_gruppo[g] = tempotot_per_gruppo.get(g, 0) + int(o.get("tempo_minuti", 0) or 0)

    # costruisco consegne per gruppo
    consegne = []
    for g, fine in fine_per_gruppo.items():
        info = baseinfo_per_gruppo.get(g, {"Cliente": "", "Prodotto": ""})
        consegne.append({
            "Gruppo": g,
            "Cliente": info.get("Cliente", ""),
            "Prodotto": info.get("Prodotto", ""),
            "Tempo_totale_minuti": int(tempotot_per_gruppo.get(g, 0)),
            "Stimata": str(prossimo_giorno_lavorativo(fine)),
        })

    # ordino consegne per gruppo numerico
    def grp_key(x):
        try:
            return int(x.get("Gruppo", 0))
        except Exception:
            return 0

    consegne.sort(key=grp_key)

    salva_dati(dati)
    return consegne, piano

def calcola_saturazione(piano):
    if not piano:
        return pd.DataFrame()

    df = pd.DataFrame(piano)
    df["Data"] = pd.to_datetime(df["Data"])
    df["Giorno"] = df["Data"].dt.strftime("%d/%m")

    agg = (
        df.groupby(["Giorno", "Materiale"], as_index=False)
          .agg(minuti_usati=("Minuti_prodotti", "sum"))
    )

    agg["Capacità"] = agg["Materiale"].map(CAPACITA_MINUTI_GIORNALIERA)
    agg["Saturazione_%"] = (agg["minuti_usati"] / agg["Capacità"]).round(3)
    agg["Saturazione_testo"] = (agg["Saturazione_%"] * 100).round(1).astype(str) + " %"


    return agg.sort_values(["Giorno", "Materiale"])

    consegne_ordini = list(consegna_per_gruppo.values())

    def grp_key(x):
        try:
            return int(x.get("Gruppo", 0))
        except Exception:
            return 0

    consegne_ordini.sort(key=grp_key)
    return consegne_ordini, piano


# =========================
# ✅ NUOVO: INSERIMENTO NON DISTRUTTIVO (non sposta gli ordini già inseriti)
# =========================
def _build_load_from_piano(piano: list[dict]) -> dict:
    load = {"PVC": {}, "Alluminio": {}}
    for r in piano:
        mat = r.get("Materiale", "PVC")
        d = str(r.get("Data"))
        m = int(r.get("Minuti_prodotti", 0) or 0)
        if mat not in load:
            load[mat] = {}
        load[mat][d] = load[mat].get(d, 0) + m
    return load

def _sum_minutes_per_material(righe_correnti: list[dict]) -> dict:
    mins = {"PVC": 0, "Alluminio": 0}
    for r in righe_correnti:
        mat = r.get("materiale", "PVC")
        t = int(r.get("tempo_minuti", 0) or 0)
        if mat not in mins:
            mins[mat] = 0
        mins[mat] += t
    return mins

def _find_first_start_date_without_moving_existing(dati_esistenti: dict, righe_correnti: list[dict], from_date: date | None = None) -> date:
    if from_date is None:
        from_date = date.today()

    # Piano attuale (solo ordini esistenti)
    _, piano_old = calcola_piano(dati_esistenti)
    load = _build_load_from_piano(piano_old)

    need = _sum_minutes_per_material(righe_correnti)
    mats = ["PVC", "Alluminio"]

    d = prossimo_giorno_lavorativo(from_date)

    # Cerca fino a 365 giorni lavorativi avanti
    for _ in range(365):
        tmp = {m: dict(load.get(m, {})) for m in mats}

        for m in mats:
            remaining = int(need.get(m, 0) or 0)
            if remaining <= 0:
                continue

            cur = d
            while remaining > 0:
                cur = prossimo_giorno_lavorativo(cur)

                used = int(tmp[m].get(str(cur), 0) or 0)
                cap = int(CAPACITA_MINUTI_GIORNALIERA.get(m, 0) or 0)
                free = max(0, cap - used)

                if free <= 0:
                    cur = aggiungi_giorno_lavorativo(cur)
                    continue

                take = min(free, remaining)
                tmp[m][str(cur)] = used + take
                remaining -= take

        # Se arrivo qui, significa che da d in poi lo spazio c'è (prima o poi)
        return d

    # Fallback: in coda
    if piano_old:
        last_day = max(pd.to_datetime([r["Data"] for r in piano_old])).date()
        return aggiungi_giorno_lavorativo(last_day)

    return prossimo_giorno_lavorativo(date.today())


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

            # ✅ NUOVO: trova data inizio senza spostare gli ordini già inseriti
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
                    # ✅ qui NON metto più data_richiesta: metto la prima data disponibile senza spostare gli altri
                    "data_inizio_gruppo": str(start_suggerito),
                    "inserito_il": str(date.today())
                }
                dati["ordini"].append(nuovo)

            salva_dati(dati)
            st.session_state["righe_correnti"] = []
            st.success(f"Ordine salvato (gruppo {ordine_gruppo}) - inizio: {start_suggerito}")

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

# -----------------------------
# CONSEGNE + PIANO
# -----------------------------
if "consegne" in st.session_state:
    st.subheader("✅ Consegne stimate (per gruppo)")
    st.dataframe(st.session_state["consegne"], use_container_width=True)

if "piano" in st.session_state:
    st.subheader("🧾 Piano produzione giorno per giorno (spezzato a minuti)")
    st.dataframe(st.session_state["piano"], use_container_width=True)

    st.subheader("📦 Sposta inizio produzione commessa")
    st.subheader("📈 Saturazione produzione (PVC / Alluminio)")

    sat = calcola_saturazione(st.session_state["piano"])

    if sat.empty:
        st.info("Nessun dato di saturazione.")
    else:
        st.dataframe(
            sat,
            use_container_width=True,
            column_config={
                "Saturazione_%": st.column_config.ProgressColumn(
                    "Saturazione %",
                    min_value=0,
                    max_value=100,
                )
            }
        )

if "consegne" in st.session_state:
    gruppi = sorted({str(o["Gruppo"]) for o in st.session_state["consegne"]})

    g_sel = st.selectbox("Seleziona gruppo", gruppi)
    nuova_data = st.date_input("Nuova data inizio produzione")

    if st.button("📌 Applica spostamento"):
        for o in dati["ordini"]:
            if str(o.get("ordine_gruppo")) == g_sel:
                o["data_inizio_gruppo"] = str(nuova_data)

        salva_dati(dati)

        consegne, piano = calcola_piano(dati)
        st.session_state["consegne"] = consegne
        st.session_state["piano"] = piano

        st.success(f"Gruppo {g_sel} spostato al {nuova_data}")
        st.rerun()

    # =========================
    # GANTT CLASSICO (NO SAB/DOM ANCHE ASSE) + GIORNI CONTINUI
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



































