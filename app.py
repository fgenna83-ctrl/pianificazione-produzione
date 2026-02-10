import streamlit as st
from datetime import date, timedelta
import json
import os
import hashlib
import pandas as pd
import altair as alt
from pathlib import Path
import streamlit.components.v1 as components

# =========================
# FILE DATI
# =========================
FILE_DATI = "dati_produzione.json"

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
# NORMALIZZAZIONI
# =========================
def norm_materiale(x: str) -> str:
    x = (x or "").strip()
    xl = x.lower()
    if "allu" in xl:
        return "Alluminio"
    if xl == "pvc" or x == "":
        return "PVC"
    # fallback
    return "PVC"

def norm_tipologia(x: str) -> str:
    x = (x or "").strip()
    xl = x.lower()
    if xl in ("battente",):
        return "Battente"
    if xl in ("scorrevole",):
        return "Scorrevole"
    if xl in ("struttura speciale", "strutturaspeciale", "speciale", "struttura_speciale"):
        return "Struttura speciale"
    return x

def tipologia_cluster(tip: str) -> str:
    """Raggruppo scorrevole + speciale perché hanno stesse capacità."""
    tip = norm_tipologia(tip)
    if tip in ("Scorrevole", "Struttura speciale"):
        return "Scorrevole/Speciale"
    return "Battente"

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
# CAPACITÀ PER FASE (unità/giorno)
# unità:
#  - Battente: "vetri"
#  - Scorrevole/Speciale: "strutture"
# =========================

# Risorse separate:
# - Taglio PVC, Taglio Alluminio
# - Saldatura solo PVC
# - Assemblaggio PVC, Assemblaggio Alluminio
# Risorse condivise:
# - Vetrazione (totale PVC+Alluminio)
# - Imballaggio (totale PVC+Alluminio)

CAP = {
    # -------- Battente (vetri/giorno)
    ("Taglio", "PVC", "Battente"): 60,
    ("Taglio", "Alluminio", "Battente"): 40,

    ("Saldatura", "PVC", "Battente"): 40,  # solo PVC

    ("Assemblaggio", "PVC", "Battente"): 50,
    ("Assemblaggio", "Alluminio", "Battente"): 30,

    # Vetrazione condivisa -> useremo materiale="ALL"
    ("Vetrazione", "ALL", "Battente"): 40,

    # Imballaggio condiviso -> useremo materiale="ALL"
    ("Imballaggio", "ALL", "Battente"): 60,

    # -------- Scorrevole/Speciale (strutture/giorno)
    ("Taglio", "PVC", "Scorrevole/Speciale"): 10,
    ("Taglio", "Alluminio", "Scorrevole/Speciale"): 10,

    ("Saldatura", "PVC", "Scorrevole/Speciale"): 10,  # solo PVC

    ("Assemblaggio", "PVC", "Scorrevole/Speciale"): 10,
    ("Assemblaggio", "Alluminio", "Scorrevole/Speciale"): 10,

    # Vetrazione condivisa
    ("Vetrazione", "ALL", "Scorrevole/Speciale"): 15,

    # Imballaggio condiviso (ASSUNZIONE: stesso limite 60 anche per strutture)
    ("Imballaggio", "ALL", "Scorrevole/Speciale"): 60,
}

PHASES_PVC = ["Taglio", "Saldatura", "Assemblaggio", "Vetrazione", "Imballaggio"]
PHASES_ALLU = ["Taglio", "Assemblaggio", "Vetrazione"]

# =========================
# INPUT: calcolo carico per riga
# =========================
def carico_riga_unita(o: dict) -> tuple[str, int]:
    """
    Ritorna (cluster, qta)
      - Battente -> qta = vetri_totali
      - Scorrevole/Speciale -> qta = quantita_strutture
    """
    tip = tipologia_cluster(o.get("tipologia", "Battente"))
    if tip == "Battente":
        q = int(o.get("vetri_totali", 0) or 0)
        return tip, max(0, q)
    else:
        q = int(o.get("quantita_strutture", 0) or 0)
        return tip, max(0, q)

# =========================
# BUILD NEEDS per fase
# =========================
def build_group_meta(dati: dict):
    meta = {}
    for o in dati.get("ordini", []):
        g = str(o.get("ordine_gruppo"))
        if g not in meta:
            meta[g] = {
                "Cliente": o.get("cliente", ""),
                "Prodotto": o.get("prodotto", ""),
                "Inserito": safe_date(o.get("inserito_il") or date.today()),
                "StartTaglio": safe_date(o.get("data_inizio_taglio_gruppo") or o.get("inserito_il") or date.today()),
            }
        else:
            # tengo il più vecchio come data inserimento, e start taglio più vecchio
            meta[g]["Inserito"] = min(meta[g]["Inserito"], safe_date(o.get("inserito_il") or date.today()))
            meta[g]["StartTaglio"] = min(meta[g]["StartTaglio"], safe_date(o.get("data_inizio_taglio_gruppo") or o.get("inserito_il") or date.today()))
    return meta

def build_needs_by_phase(dati: dict):
    """
    needs[phase][resource_key][group] = qty
    resource_key:
      - per fasi separate: (materiale, cluster) es: ("PVC","Battente")
      - per fasi condivise: ("ALL", cluster)
    """
    needs = {p: {} for p in ["Taglio", "Saldatura", "Assemblaggio", "Vetrazione", "Imballaggio"]}

    for o in dati.get("ordini", []):
        g = str(o.get("ordine_gruppo"))
        mat = norm_materiale(o.get("materiale", "PVC"))
        cluster, qta = carico_riga_unita(o)
        if qta <= 0:
            continue

        # TAGLIO: sempre (PVC e Allu)
        needs["Taglio"].setdefault((mat, cluster), {})
        needs["Taglio"][(mat, cluster)][g] = needs["Taglio"][(mat, cluster)].get(g, 0) + qta

        # SALDATURA: solo PVC
        if mat == "PVC":
            needs["Saldatura"].setdefault((mat, cluster), {})
            needs["Saldatura"][(mat, cluster)][g] = needs["Saldatura"][(mat, cluster)].get(g, 0) + qta

        # ASSEMBLAGGIO: PVC e Allu
        needs["Assemblaggio"].setdefault((mat, cluster), {})
        needs["Assemblaggio"][(mat, cluster)][g] = needs["Assemblaggio"][(mat, cluster)].get(g, 0) + qta

        # VETRAZIONE: condivisa ALL
        needs["Vetrazione"].setdefault(("ALL", cluster), {})
        needs["Vetrazione"][("ALL", cluster)][g] = needs["Vetrazione"][("ALL", cluster)].get(g, 0) + qta

        # IMBALLAGGIO: solo PVC secondo tuo processo? (tu hai scritto PVC sì, Alluminio no)
        # Se vuoi anche Alluminio in imballaggio, togli questo if.
        if mat == "PVC":
            needs["Imballaggio"].setdefault(("ALL", cluster), {})
            needs["Imballaggio"][("ALL", cluster)][g] = needs["Imballaggio"][("ALL", cluster)].get(g, 0) + qta

    return needs

# =========================
# SCHEDULER GENERICO a capacità/giorno
# =========================
def schedule_resource(
    phase: str,
    material_key: str,   # "PVC" | "Alluminio" | "ALL"
    cluster: str,        # "Battente" | "Scorrevole/Speciale"
    group_qty: dict,     # {group: qty}
    group_meta: dict,    # meta per gruppo
    group_start_day: dict,  # {group: start_day}
    load_used: dict,     # load_used[(phase,material_key,cluster)][day_str] = used
):
    """
    Pianifica questo resource (una "linea") riempiendo i giorni fino a saturazione.
    Ritorna:
      - plan_rows: list[dict]
      - end_day_by_group: dict[group]=last_day_used
    """
    key = (phase, material_key, cluster)
    cap = int(CAP.get(key, 0))
    if cap <= 0:
        return [], {}

    load_used.setdefault(key, {})

    # ordine gruppi: per data start (inserimento/taglio) poi per numero gruppo
    def grp_sort(g):
        sd = group_start_day.get(g, group_meta.get(g, {}).get("StartTaglio", date.today()))
        try:
            gi = int(g)
        except Exception:
            gi = 10**9
        return (sd, gi)

    groups = sorted([g for g, q in group_qty.items() if int(q) > 0], key=grp_sort)

    plan_rows = []
    end_day_by_group = {}

    for g in groups:
        remaining = int(group_qty.get(g, 0) or 0)
        if remaining <= 0:
            continue

        day = prossimo_giorno_lavorativo(group_start_day.get(g, date.today()))
        # ✅ non posso andare prima della data start del gruppo per questa fase
        # ma posso entrare nello stesso giorno se c'è capienza residua.

        while remaining > 0:
            day = prossimo_giorno_lavorativo(day)
            ds = str(day)

            used = int(load_used[key].get(ds, 0) or 0)
            free = max(0, cap - used)

            if free <= 0:
                day = aggiungi_giorno_lavorativo(day)
                continue

            take = min(free, remaining)
            load_used[key][ds] = used + take
            remaining -= take

            meta = group_meta.get(g, {"Cliente": "", "Prodotto": ""})
            plan_rows.append({
                "Fase": phase,
                "Data": ds,
                "Gruppo": str(g),
                "Cliente": meta.get("Cliente", ""),
                "Prodotto": meta.get("Prodotto", ""),
                "Materiale": material_key,
                "Tipo": cluster,
                "Quantita_lavorata": int(take),
                "Residuo_capacita_giorno": int(cap - (used + take)),
            })

            end_day_by_group[g] = max(end_day_by_group.get(g, day), day)

            if remaining > 0:
                # continuo dal giorno stesso (se free era grande, potrebbe restare spazio,
                # ma take è min(free,remaining) quindi free finito o remaining finito.
                # se remaining > 0 e free era finito, passo al giorno dopo.
                if load_used[key][ds] >= cap:
                    day = aggiungi_giorno_lavorativo(day)

    return plan_rows, end_day_by_group

# =========================
# CALCOLO PIANI DI TUTTE LE FASI + CONSEGNE
# =========================
def calcola_piani_fasi(dati: dict):
    ordini = dati.get("ordini", [])
    if not ordini:
        return {}, []

    group_meta = build_group_meta(dati)
    needs = build_needs_by_phase(dati)

    groups = sorted(group_meta.keys(), key=lambda x: int(x) if str(x).isdigit() else 10**9)

    # start day per fase (sequenza)
    start_day_by_group_phase = {}  # (group, phase) -> date
    end_day_by_group_phase = {}    # (group, phase) -> date

    # fase 1: Taglio parte da data_inizio_taglio_gruppo (o inserito)
    for g in groups:
        start_day_by_group_phase[(g, "Taglio")] = prossimo_giorno_lavorativo(group_meta[g]["StartTaglio"])

    load_used = {}
    plans = {p: [] for p in ["Taglio", "Saldatura", "Assemblaggio", "Vetrazione", "Imballaggio"]}

    def phase_start(g: str, phase: str) -> date:
        return start_day_by_group_phase.get((g, phase), prossimo_giorno_lavorativo(group_meta[g]["StartTaglio"]))

    def set_next_phase_start(g: str, cur_phase: str, next_phase: str):
        end_cur = end_day_by_group_phase.get((g, cur_phase))
        if end_cur is None:
            # se fase non esiste per questo gruppo, non obbligo
            start_day_by_group_phase[(g, next_phase)] = phase_start(g, next_phase)
            return
        start_day_by_group_phase[(g, next_phase)] = aggiungi_giorno_lavorativo(prossimo_giorno_lavorativo(end_cur))

    # Pianifico in ordine fasi
    phase_order = ["Taglio", "Saldatura", "Assemblaggio", "Vetrazione", "Imballaggio"]

    for phase in phase_order:
        # per ogni resource della fase
        for (mat, cluster), group_qty in needs.get(phase, {}).items():
            # start per ogni gruppo = max(start fase, ...)
            group_start = {}
            for g in group_qty.keys():
                group_start[g] = phase_start(g, phase)

            rows, end_by_g = schedule_resource(
                phase=phase,
                material_key=mat,
                cluster=cluster,
                group_qty=group_qty,
                group_meta=group_meta,
                group_start_day=group_start,
                load_used=load_used,
            )

            plans[phase].extend(rows)

            for g, endd in end_by_g.items():
                prev = end_day_by_group_phase.get((g, phase))
                end_day_by_group_phase[(g, phase)] = max(prev, endd) if prev else endd

        # imposto start fase successiva (sequenza) usando la fine massima della fase per quel gruppo
        if phase != phase_order[-1]:
            next_phase = phase_order[phase_order.index(phase) + 1]
            for g in groups:
                # fine max di quella fase (anche se su più risorse)
                end_max = end_day_by_group_phase.get((g, phase))
                if end_max is not None:
                    start_day_by_group_phase[(g, next_phase)] = aggiungi_giorno_lavorativo(prossimo_giorno_lavorativo(end_max))
                else:
                    # se non ha lavori in questa fase, lascio invariato
                    start_day_by_group_phase[(g, next_phase)] = start_day_by_group_phase.get((g, next_phase), phase_start(g, next_phase))

    # CONSEGNE: fine dell'ultima fase presente (PVC -> Imballaggio, Allu -> Vetrazione)
    # prendo la data max tra (Imballaggio) e (Vetrazione)
    consegne = []
    for g in groups:
        end_imp = end_day_by_group_phase.get((g, "Imballaggio"))
        end_vet = end_day_by_group_phase.get((g, "Vetrazione"))
        fine = None
        if end_imp and end_vet:
            fine = max(end_imp, end_vet)
        else:
            fine = end_imp or end_vet

        if fine is None:
            fine = prossimo_giorno_lavorativo(date.today())

        # +3 gg lavorativi (come facevi prima)
        d = prossimo_giorno_lavorativo(fine)
        for _ in range(3):
            d = aggiungi_giorno_lavorativo(d)

        consegne.append({
            "Gruppo": str(g),
            "Cliente": group_meta[g]["Cliente"],
            "Prodotto": group_meta[g]["Prodotto"],
            "Stimata": str(d),
        })

    # ordino righe piani
    for p in plans:
        plans[p].sort(key=lambda r: (r["Data"], r["Gruppo"], r["Materiale"], r["Tipo"]))

    consegne.sort(key=lambda x: int(x["Gruppo"]) if x["Gruppo"].isdigit() else 10**9)
    return plans, consegne

# =========================
# GANTT (giorno per giorno)
# =========================
def render_gantt(df_phase: pd.DataFrame, title: str):
    st.subheader(title)

    if df_phase.empty:
        st.info("Nessun dato.")
        return

    df_phase = df_phase.copy()
    df_phase["Data"] = pd.to_datetime(df_phase["Data"])
    df_phase = df_phase[df_phase["Data"].dt.weekday < 5].copy()
    df_phase["Giorno"] = df_phase["Data"].dt.strftime("%d/%m")

    df_phase["Commessa"] = (
        "G" + df_phase["Gruppo"].astype(str)
        + " | " + df_phase["Cliente"].astype(str)
        + " | " + df_phase["Prodotto"].astype(str)
    )

    agg = (
        df_phase.groupby(["Giorno", "Commessa", "Gruppo", "Cliente", "Prodotto"], as_index=False)
        .agg(qta=("Quantita_lavorata", "sum"))
    )

    min_d = df_phase["Data"].min().normalize()
    max_d = df_phase["Data"].max().normalize()

    all_days = pd.date_range(start=min_d, end=max_d, freq="B")
    giorni_ordinati = [d.strftime("%d/%m") for d in all_days]
    df_days = pd.DataFrame({"Giorno": giorni_ordinati})

    agg["label"] = agg["Commessa"] + "\n" + agg["qta"].astype(int).astype(str)

    sort_y = alt.SortField(field="Gruppo", order="ascending")

    base = alt.Chart(agg).encode(
        y=alt.Y(
            "Commessa:N",
            sort=sort_y,
            title="Commesse",
            axis=alt.Axis(labelFontSize=12, labelLimit=550, titleFontSize=13),
            scale=alt.Scale(paddingInner=0.35, paddingOuter=0.15),
        ),
        x=alt.X(
            "Giorno:N",
            sort=giorni_ordinati,
            scale=alt.Scale(domain=giorni_ordinati),
            title="Giorni (solo lavorativi)",
            axis=alt.Axis(labelAngle=0, labelFontSize=12, titleFontSize=13),
        ),
        tooltip=[
            alt.Tooltip("Giorno:N", title="Giorno"),
            alt.Tooltip("Commessa:N", title="Commessa"),
            alt.Tooltip("qta:Q", title="Quantità lavorata"),
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
        height=max(380, 70 * len(agg["Commessa"].unique()))
    )

    st.altair_chart(chart, use_container_width=True)

# =========================
# UI APP
# =========================
st.set_page_config(page_title="Planner Produzione - Fasi", layout="wide")

if not check_login():
    st.stop()

st.title("📦 Planner Produzione (Fasi + Gantt multipli)")

dati = carica_dati()

if "righe_correnti" not in st.session_state:
    st.session_state["righe_correnti"] = []

col1, col2 = st.columns(2)

with col1:
    st.subheader("⚙️ Capacità (giornaliere)")

    st.info(
        "**BATTENTE (vetri/giorno)**\n"
        "• Taglio PVC: 60 | Taglio Alluminio: 40\n"
        "• Saldatura PVC: 40\n"
        "• Assemblaggio PVC: 50 | Assemblaggio Alluminio: 30\n"
        "• Vetrazione (totale PVC+Alluminio): 40\n"
        "• Imballaggio (totale PVC+Alluminio): 60\n\n"
        "**SCORREVOLE / SPECIALE (strutture/giorno)**\n"
        "• Taglio PVC: 10 | Taglio Alluminio: 10\n"
        "• Saldatura PVC: 10\n"
        "• Assemblaggio PVC: 10 | Assemblaggio Alluminio: 10\n"
        "• Vetrazione (totale PVC+Alluminio): 15\n"
        "• Imballaggio (totale PVC+Alluminio): 60  (assunzione)\n"
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
            "Numero vetri TOTALI per questa riga",
            min_value=1, value=1, step=1
        )
    else:
        vetri_totali = 0

    # info carico
    tcluster = tipologia_cluster(tipologia)
    if tcluster == "Battente":
        st.info(f"Carico riga: {int(vetri_totali)} vetri")
    else:
        st.info(f"Carico riga: {int(quantita_strutture)} strutture")

    cadd, cclear = st.columns(2)

    with cadd:
        if st.button("➕ Aggiungi riga"):
            st.session_state["righe_correnti"].append({
                "materiale": norm_materiale(materiale),
                "tipologia": norm_tipologia(tipologia),
                "quantita_strutture": int(quantita_strutture),
                "vetri_totali": int(vetri_totali) if tipologia == "Battente" else 0,
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
                    "materiale": norm_materiale(r["materiale"]),
                    "tipologia": norm_tipologia(r["tipologia"]),
                    "quantita_strutture": int(r["quantita_strutture"]),
                    "vetri_totali": int(r["vetri_totali"]),
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

st.subheader("📋 Ordini (righe)")
if dati.get("ordini"):
    st.dataframe(dati["ordini"], use_container_width=True)
else:
    st.info("Nessun ordine inserito.")

c1, c2, c3 = st.columns([1, 1, 2])

with c1:
    if st.button("📅 Calcola piani + Gantt"):
        plans, consegne = calcola_piani_fasi(dati)
        st.session_state["plans"] = plans
        st.session_state["consegne"] = consegne

with c2:
    if st.button("🗑️ Cancella tutto"):
        dati = {"ordini": []}
        salva_dati(dati)
        st.session_state.pop("plans", None)
        st.session_state.pop("consegne", None)
        st.session_state["righe_correnti"] = []
        st.warning("Ordini cancellati")
        st.rerun()

with c3:
    if st.button("🚪 Logout"):
        st.session_state.logged_in = False
        st.rerun()

if "consegne" in st.session_state:
    st.subheader("✅ Consegne stimate (fine ultima fase + 3 gg lavorativi)")
    st.dataframe(st.session_state["consegne"], use_container_width=True)

# =========================
# GANTT MULTIPLI
# =========================
if "plans" in st.session_state:
    plans = st.session_state["plans"]

    # Taglio: separo PVC e Alluminio
    df_taglio = pd.DataFrame(plans.get("Taglio", []))
    if not df_taglio.empty:
        render_gantt(df_taglio[df_taglio["Materiale"] == "PVC"], "✂️ Gantt TAGLIO - PVC")
        render_gantt(df_taglio[df_taglio["Materiale"] == "Alluminio"], "✂️ Gantt TAGLIO - Alluminio")
    else:
        st.subheader("✂️ Gantt TAGLIO")
        st.info("Nessun dato.")

    # Saldatura: solo PVC
    df_sald = pd.DataFrame(plans.get("Saldatura", []))
    render_gantt(df_sald, "🔥 Gantt SALDATURA - PVC")

    # Assemblaggio: separo PVC e Alluminio
    df_ass = pd.DataFrame(plans.get("Assemblaggio", []))
    if not df_ass.empty:
        render_gantt(df_ass[df_ass["Materiale"] == "PVC"], "🧩 Gantt ASSEMBLAGGIO - PVC")
        render_gantt(df_ass[df_ass["Materiale"] == "Alluminio"], "🧩 Gantt ASSEMBLAGGIO - Alluminio")
    else:
        st.subheader("🧩 Gantt ASSEMBLAGGIO")
        st.info("Nessun dato.")

    # Vetrazione: unica (Materiale = ALL)
    df_vet = pd.DataFrame(plans.get("Vetrazione", []))
    render_gantt(df_vet, "🪟 Gantt VETRAZIONE (PVC + Alluminio)")

    # Imballaggio: unico
    df_imb = pd.DataFrame(plans.get("Imballaggio", []))
    render_gantt(df_imb, "📦 Gantt IMBALLAGGIO (PVC + Alluminio)")






















