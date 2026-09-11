"""
exporters.py
============
Export des resultats de dimensionnement en JSON et PDF.

- JSON : serialisation complete et fidele du SizingResult (machine-readable).
- PDF  : rapport mis en forme (reportlab/Platypus) avec tableaux et notes.

Aucune dependance Streamlit : reutilisable en CLI / API.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict

from sizing_engine import SizingResult, SizingInput, Factor


# ===========================================================================
# JSON
# ===========================================================================
def _factor_to_dict(f: Factor) -> Dict[str, Any]:
    return {"total": f.total, "origin": f.origin, "repr": str(f)}


def result_to_dict(res: SizingResult) -> Dict[str, Any]:
    """Convertit un SizingResult en dict serialisable (Factor gere a part)."""
    inp = res.inp
    d = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input": {
            **{k: v for k, v in asdict(inp).items() if k not in ("rf", "sf")},
            "rf": _factor_to_dict(inp.rf),
            "sf": _factor_to_dict(inp.sf),
        },
        "storage": asdict(res.storage),
        "tiers": [asdict(t) for t in res.tiers],
        "hardware": [asdict(h) for h in res.hardware],
        "warnings": [asdict(w) for w in res.warnings],
        "architecture_notes": res.architecture_notes,
        "assumptions": res.assumptions,
    }
    return d


def export_json(res: SizingResult, path: str) -> str:
    d = result_to_dict(res)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    return path


def export_json_str(res: SizingResult) -> str:
    return json.dumps(result_to_dict(res), indent=2, ensure_ascii=False)


# ===========================================================================
# PDF (reportlab / Platypus)
# ===========================================================================
def export_pdf(res: SizingResult, path: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    )

    def gb_tb(gb: float) -> str:
        """Affiche en To si >= 1024 Go, sinon en Go."""
        if gb >= 1024:
            return f"{gb / 1024:.2f} To"
        return f"{gb:.1f} Go"

    def esc(s: str) -> str:
        """Echappe les caracteres speciaux XML pour reportlab Paragraph."""
        return (str(s).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;"))

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "H1c", parent=styles["Heading1"], textColor=colors.HexColor("#0b3d59"),
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        "H2c", parent=styles["Heading2"], textColor=colors.HexColor("#11688f"),
        spaceBefore=10, spaceAfter=4,
    ))
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8,
                           textColor=colors.HexColor("#555555"))
    note_style = ParagraphStyle("note", parent=styles["Normal"], fontSize=9,
                                leading=12)

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        title="Dimensionnement Splunk",
    )
    story = []
    inp = res.inp
    st = res.storage

    # --- Titre ---
    story.append(Paragraph("Dimensionnement infrastructure Splunk distribuee", styles["H1c"]))
    story.append(Paragraph(
        f"Genere le {datetime.now().strftime('%d/%m/%Y a %H:%M')}", small))
    story.append(Spacer(1, 8))

    # --- Parametres d'entree ---
    story.append(Paragraph("Parametres d'entree", styles["H2c"]))
    site_mode = "Mono-site" if inp.num_sites == 1 else f"Multisite ({inp.num_sites} sites)"
    in_rows = [
        ["Volume quotidien", gb_tb(inp.daily_volume_gb) + "/jour"],
        ["Retention totale", f"{inp.retention_days} jours"],
        ["Retention hot/warm", f"{inp.hotwarm_retention_days} jours"],
        ["Topologie", site_mode],
        ["Replication Factor (RF)", str(inp.rf)],
        ["Search Factor (SF)", str(inp.sf)],
        ["Indexers", str(inp.num_indexers)],
        ["Search heads", str(inp.num_search_heads)],
        ["Compression rawdata / tsidx",
         f"{inp.compression_rawdata:.0%} / {inp.compression_tsidx:.0%}"],
        ["Utilisation disque cible", f"{inp.target_disk_utilisation:.0%}"],
        ["Data models acceleres", str(inp.num_accelerated_dm)],
        ["Recherches concurrentes", str(inp.concurrent_searches)],
        ["Premium app ES", "Oui" if inp.use_es else "Non"],
        ["Cold sur SmartStore", "Oui" if inp.smartstore_cold else "Non"],
    ]
    t = Table(in_rows, colWidths=[6 * cm, 11 * cm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#0b3d59")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1),
         [colors.white, colors.HexColor("#f2f7fa")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)

    # --- Stockage ---
    story.append(Paragraph("Stockage", styles["H2c"]))
    stor_rows = [
        ["Poste", "Cluster (sur retention)"],
        ["Rawdata (x RF.total)", gb_tb(st.rawdata_cluster_gb)],
        ["tsidx (x SF.total)", gb_tb(st.tsidx_cluster_gb)],
        ["Data model accel (HPAS)", gb_tb(st.dm_accel_cluster_gb)],
        ["Donnees totales (hors marge)", gb_tb(st.data_total_cluster_gb)],
        ["Capacite BRUTE cluster (avec marge)", gb_tb(st.raw_capacity_cluster_gb)],
        ["Capacite BRUTE / indexer", gb_tb(st.raw_capacity_per_indexer_gb)],
    ]
    t2 = Table(stor_rows, colWidths=[10 * cm, 7 * cm])
    t2.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#11688f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 5), (-1, 6), "Helvetica-Bold"),
        ("BACKGROUND", (0, 5), (-1, 6), colors.HexColor("#eaf3f8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, 4),
         [colors.white, colors.HexColor("#f2f7fa")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t2)

    # --- Tiering ---
    story.append(Paragraph("Repartition par tier", styles["H2c"]))
    tier_rows = [["Tier", "Retention", "Cluster", "/Indexer", "Media", "IOPS min/reco"]]
    for tr in res.tiers:
        tier_rows.append([
            tr.name, f"{tr.retention_days} j",
            gb_tb(tr.cluster_gb), gb_tb(tr.per_indexer_gb),
            tr.media, f"{tr.iops_min:.0f} / {tr.iops_reco:.0f}",
        ])
    t3 = Table(tier_rows, colWidths=[3.2*cm, 2*cm, 3*cm, 3*cm, 3.3*cm, 2.5*cm])
    t3.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#11688f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f7fa")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t3)

    # --- Hardware ---
    story.append(Paragraph("Dimensionnement CPU / RAM par role", styles["H2c"]))
    hw_rows = [["Role", "Nb", "vCPU", "RAM", "Source"]]
    for h in res.hardware:
        hw_rows.append([h.role, str(h.count), str(h.vcpu_each),
                        f"{h.ram_gb_each} Go", h.source])
    t4 = Table(hw_rows, colWidths=[6*cm, 1.5*cm, 2*cm, 2.5*cm, 5*cm])
    t4.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#11688f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f7fa")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t4)
    story.append(Paragraph(
        "Source OFFICIEL = doc Splunk (Reference Hardware / ES Performance). "
        "Source TERRAIN = regle empirique communement admise, a calibrer.",
        small))

    story.append(PageBreak())

    # --- Warnings ---
    if res.warnings:
        story.append(Paragraph("Alertes & verifications", styles["H2c"]))
        color_map = {"ERREUR": "#b00020", "ALERTE": "#c77700", "INFO": "#11688f"}
        for w in res.warnings:
            c = color_map.get(w.level, "#333333")
            story.append(Paragraph(
                f'<font color="{c}"><b>[{w.level}]</b></font> {esc(w.message)}',
                note_style))
            story.append(Spacer(1, 3))

    # --- Notes archi ---
    story.append(Paragraph("Recommandations d'architecture", styles["H2c"]))
    for n in res.architecture_notes:
        story.append(Paragraph(f"&bull; {esc(n)}", note_style))
        story.append(Spacer(1, 3))

    # --- Hypotheses / formules ---
    story.append(Paragraph("Hypotheses & formules de calcul", styles["H2c"]))
    for k, v in res.assumptions.items():
        story.append(Paragraph(f"<b>{esc(k)}</b> : {esc(v)}", small))
        story.append(Spacer(1, 2))

    doc.build(story)
    return path
