"""
app.py
======
Interface Streamlit du calculateur de dimensionnement Splunk distribue.

Couche de PRESENTATION uniquement : toute la logique metier est dans
sizing_engine.py. Lancer avec :

    streamlit run app.py

L'UI permet de saisir les parametres, affiche les resultats (tableaux +
graphiques), les warnings, les recommandations et les hypotheses, et
propose l'export JSON / PDF.
"""

from __future__ import annotations

import io
import tempfile

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from sizing_engine import SizingInput, Factor, compute_sizing
from exporters import export_json_str, result_to_dict, export_pdf
import references as R


# ---------------------------------------------------------------------------
st.set_page_config(page_title="Splunk Sizing Calculator", layout="wide",
                   page_icon="📊")


def gb_disp(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.2f} To"
    return f"{gb:.1f} Go"


# ===========================================================================
# SIDEBAR — parametres d'entree
# ===========================================================================
st.sidebar.title("Parametres d'entree")

with st.sidebar:
    st.subheader("Volume & retention")
    vol_unit = st.radio("Unite de volume", ["Go/jour", "To/jour"],
                        horizontal=True)
    vol_raw = st.number_input(
        f"Volume d'ingestion ({vol_unit})",
        min_value=0.0, value=1.0 if vol_unit == "To/jour" else 1024.0,
        step=0.1 if vol_unit == "To/jour" else 50.0)
    daily_volume_gb = vol_raw * 1024 if vol_unit == "To/jour" else vol_raw

    retention_days = st.number_input("Retention totale (jours)",
                                     min_value=1, value=365, step=30)
    hotwarm_retention_days = st.number_input(
        "Retention hot/warm (jours)", min_value=1, value=30, step=5,
        help="Le reste de la retention bascule en cold.")

    st.subheader("Topologie")
    num_sites = st.number_input("Nombre de sites", min_value=1, value=2, step=1)
    num_indexers = st.number_input("Nombre d'indexers (total)",
                                   min_value=1, value=4, step=1)
    num_search_heads = st.number_input("Nombre de search heads",
                                       min_value=1, value=3, step=1)

    st.subheader("Replication / Search Factor")
    multisite_factors = num_sites >= 2
    if multisite_factors:
        st.caption("Mode multisite (origin / total)")
        c1, c2 = st.columns(2)
        rf_origin = c1.number_input("RF origin", min_value=1, value=2, step=1)
        rf_total = c2.number_input("RF total", min_value=1, value=3, step=1)
        c3, c4 = st.columns(2)
        sf_origin = c3.number_input("SF origin", min_value=1, value=1, step=1)
        sf_total = c4.number_input("SF total", min_value=1, value=2, step=1)
        rf = Factor(total=int(rf_total), origin=int(rf_origin))
        sf = Factor(total=int(sf_total), origin=int(sf_origin))
    else:
        st.caption("Mode mono-site")
        rf_total = st.number_input("Replication Factor (RF)",
                                   min_value=1, value=3, step=1)
        sf_total = st.number_input("Search Factor (SF)",
                                   min_value=1, value=2, step=1)
        rf = Factor(total=int(rf_total))
        sf = Factor(total=int(sf_total))

    st.subheader("Compression & marge")
    compression_rawdata = st.slider("Compression rawdata", 0.05, 0.50, 0.15,
                                    0.01, help="Defaut Splunk : 15%")
    compression_tsidx = st.slider("Compression tsidx", 0.10, 0.60, 0.35,
                                  0.01, help="Defaut Splunk : 35%")
    target_disk_utilisation = st.slider("Utilisation disque cible", 0.50, 0.95,
                                        0.80, 0.05)

    st.subheader("Data model acceleration")
    num_accelerated_dm = st.number_input("Nb de DM acceleres",
                                         min_value=0, value=5, step=1)
    dm_accel_retention_days = st.number_input(
        "Retention d'acceleration (jours)", min_value=1, value=7, step=1)
    dm_accel_factor = st.slider(
        "Facteur surcout tsidx DM (estime)", 0.005, 0.10,
        R.DM_ACCEL_FACTOR.value, 0.005,
        help="ESTIMATION (TERRAIN) — aucun ratio officiel universel. "
             "Fraction du volume/jour par DM et par jour d'acceleration.")

    st.subheader("Charge & options")
    concurrent_searches = st.number_input("Recherches concurrentes estimees",
                                          min_value=1, value=12, step=1)
    use_es = st.checkbox("Premium app ES (Enterprise Security)", value=True)
    smartstore_cold = st.checkbox("Cold sur SmartStore (S3)", value=False)


# ===========================================================================
# CALCUL
# ===========================================================================
inp = SizingInput(
    daily_volume_gb=daily_volume_gb,
    retention_days=int(retention_days),
    hotwarm_retention_days=int(hotwarm_retention_days),
    rf=rf, sf=sf,
    num_sites=int(num_sites),
    num_indexers=int(num_indexers),
    num_search_heads=int(num_search_heads),
    compression_rawdata=compression_rawdata,
    compression_tsidx=compression_tsidx,
    target_disk_utilisation=target_disk_utilisation,
    num_accelerated_dm=int(num_accelerated_dm),
    dm_accel_retention_days=int(dm_accel_retention_days),
    dm_accel_factor=dm_accel_factor,
    concurrent_searches=int(concurrent_searches),
    use_es=use_es,
    smartstore_cold=smartstore_cold,
)
res = compute_sizing(inp)
st_res = res.storage


# ===========================================================================
# HEADER & WARNINGS
# ===========================================================================
st.title("📊 Calculateur de dimensionnement Splunk distribue")
st.caption("Multisite • RF/SF origin·total • tiering hot/warm/cold • "
           "data model acceleration • CPU/RAM par role")

errors = [w for w in res.warnings if w.level == "ERREUR"]
alerts = [w for w in res.warnings if w.level == "ALERTE"]
infos = [w for w in res.warnings if w.level == "INFO"]

if errors:
    for w in errors:
        st.error(f"**[ERREUR]** {w.message}")
if alerts:
    for w in alerts:
        st.warning(f"**[ALERTE]** {w.message}")
if infos:
    with st.expander(f"ℹ️ {len(infos)} info(s) / recommandation(s)"):
        for w in infos:
            st.info(w.message)


# ===========================================================================
# KPIs
# ===========================================================================
k1, k2, k3, k4 = st.columns(4)
k1.metric("Capacite brute cluster", gb_disp(st_res.raw_capacity_cluster_gb))
k2.metric("Brute / indexer", gb_disp(st_res.raw_capacity_per_indexer_gb))
k3.metric("Hot/warm / indexer", gb_disp(st_res.hotwarm_per_indexer_gb))
k4.metric("Cold / indexer", gb_disp(st_res.cold_per_indexer_gb))


# ===========================================================================
# ONGLETS
# ===========================================================================
tab_stock, tab_tier, tab_hw, tab_archi, tab_form, tab_export = st.tabs(
    ["Stockage", "Tiers", "CPU / RAM", "Architecture", "Formules", "Export"])


# --- STOCKAGE ---
with tab_stock:
    cL, cR = st.columns([1.1, 1])
    with cL:
        st.subheader("Detail du stockage (cluster, sur retention)")
        df_stock = pd.DataFrame([
            ["Rawdata (x RF.total)", gb_disp(st_res.rawdata_cluster_gb)],
            ["tsidx (x SF.total)", gb_disp(st_res.tsidx_cluster_gb)],
            ["Data model accel (HPAS)", gb_disp(st_res.dm_accel_cluster_gb)],
            ["Donnees totales (hors marge)", gb_disp(st_res.data_total_cluster_gb)],
            ["Capacite BRUTE (avec marge)", gb_disp(st_res.raw_capacity_cluster_gb)],
            ["Capacite BRUTE / indexer", gb_disp(st_res.raw_capacity_per_indexer_gb)],
        ], columns=["Poste", "Valeur"])
        st.table(df_stock)

        st.subheader("Par jour (cluster)")
        pd_ = st_res.per_day
        df_day = pd.DataFrame([
            ["Rawdata 1 copie", gb_disp(pd_.rawdata_one_copy_gb)],
            ["tsidx 1 copie", gb_disp(pd_.tsidx_one_copy_gb)],
            ["Rawdata cluster/jour", gb_disp(pd_.rawdata_cluster_gb)],
            ["tsidx cluster/jour", gb_disp(pd_.tsidx_cluster_gb)],
            ["Total cluster/jour", gb_disp(pd_.total_cluster_gb)],
        ], columns=["Poste", "Valeur"])
        st.table(df_day)

    with cR:
        st.subheader("Repartition du stockage")
        labels = ["Rawdata", "tsidx", "DM accel"]
        values = [st_res.rawdata_cluster_gb, st_res.tsidx_cluster_gb,
                  st_res.dm_accel_cluster_gb]
        fig = go.Figure(data=[go.Pie(
            labels=labels, values=values, hole=0.45,
            marker=dict(colors=["#11688f", "#5ab0d6", "#f0a202"]))])
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300)
        st.plotly_chart(fig, use_container_width=True)

        # Projection cumulative sur la retention
        st.subheader("Projection capacite vs retention")
        days = list(range(0, int(retention_days) + 1,
                          max(1, int(retention_days) // 60)))
        per_day_brut = (st_res.data_total_cluster_gb / max(retention_days, 1)
                        / max(target_disk_utilisation, 0.01))
        cum = [d * per_day_brut / 1024 for d in days]  # en To
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=days, y=cum, fill="tozeroy",
                                  line=dict(color="#11688f"),
                                  name="Capacite brute (To)"))
        fig2.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=260,
                           xaxis_title="Jours de retention",
                           yaxis_title="Capacite brute cumulee (To)")
        st.plotly_chart(fig2, use_container_width=True)


# --- TIERS ---
with tab_tier:
    st.subheader("Recommandations par tier")
    if res.tiers:
        df_tiers = pd.DataFrame([{
            "Tier": t.name,
            "Retention (j)": t.retention_days,
            "Cluster": gb_disp(t.cluster_gb),
            "/ Indexer": gb_disp(t.per_indexer_gb),
            "Media": t.media,
            "IOPS min": int(t.iops_min),
            "IOPS reco": int(t.iops_reco),
        } for t in res.tiers])
        st.dataframe(df_tiers, use_container_width=True, hide_index=True)
        for t in res.tiers:
            st.markdown(f"**{t.name}** — {t.note}")

        # Barres hot/warm vs cold par indexer
        fig = go.Figure(data=[go.Bar(
            x=[t.name for t in res.tiers],
            y=[t.per_indexer_gb / 1024 for t in res.tiers],
            marker_color=["#11688f", "#8fc3df"][:len(res.tiers)],
            text=[gb_disp(t.per_indexer_gb) for t in res.tiers],
            textposition="auto")])
        fig.update_layout(margin=dict(t=20, b=10), height=300,
                          yaxis_title="To / indexer (avec marge)",
                          title="Capacite par tier et par indexer")
        st.plotly_chart(fig, use_container_width=True)


# --- HARDWARE ---
with tab_hw:
    st.subheader("Dimensionnement CPU / RAM par role")
    df_hw = pd.DataFrame([{
        "Role": h.role, "Nb": h.count, "vCPU (chacun)": h.vcpu_each,
        "RAM (chacun)": f"{h.ram_gb_each} Go", "Source": h.source,
    } for h in res.hardware])
    st.dataframe(df_hw, use_container_width=True, hide_index=True)
    st.caption("OFFICIEL = doc Splunk (Reference Hardware / ES Performance). "
               "TERRAIN = regle empirique a calibrer.")
    for h in res.hardware:
        with st.expander(f"{h.role} — detail"):
            st.write(h.note)
            if h.disk_note:
                st.write(f"**Disque :** {h.disk_note}")


# --- ARCHITECTURE ---
with tab_archi:
    st.subheader("Recommandations d'architecture")
    for n in res.architecture_notes:
        st.markdown(f"- {n}")


# --- FORMULES / HYPOTHESES ---
with tab_form:
    st.subheader("Formules & hypotheses de calcul")
    for k, v in res.assumptions.items():
        st.markdown(f"**{k}** — {v}")
    st.divider()
    st.subheader("Valeurs de reference utilisees")
    refs = R.all_references()
    df_ref = pd.DataFrame([{
        "Parametre": name, "Valeur": ref.value,
        "Source": ref.source, "Note": ref.note,
    } for name, ref in refs.items()])
    st.dataframe(df_ref, use_container_width=True, hide_index=True)


# --- EXPORT ---
with tab_export:
    st.subheader("Export des resultats")
    json_str = export_json_str(res)
    st.download_button("⬇️ Telecharger JSON", data=json_str,
                       file_name="splunk_sizing.json", mime="application/json")

    if st.button("Generer le PDF"):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            export_pdf(res, tmp.name)
            tmp.flush()
            with open(tmp.name, "rb") as f:
                pdf_bytes = f.read()
        st.download_button("⬇️ Telecharger PDF", data=pdf_bytes,
                           file_name="splunk_sizing.pdf",
                           mime="application/pdf")
        st.success("PDF genere.")

    with st.expander("Apercu JSON"):
        st.code(json_str, language="json")
