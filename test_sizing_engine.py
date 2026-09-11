"""
test_sizing_engine.py
=====================
Tests du moteur de dimensionnement. Lancer avec : pytest -v
(ou python3 -m pytest). Couvre formules, multisite origin/total,
tiering, warnings et cas limites.
"""

import math
import pytest

from sizing_engine import (
    SizingInput, Factor, compute_sizing,
)


# --------------------------------------------------------------------------
# Formules de base
# --------------------------------------------------------------------------
def test_rawdata_tsidx_formula_monosite():
    inp = SizingInput(
        daily_volume_gb=1000, retention_days=100,
        rf=Factor(total=3), sf=Factor(total=2),
        num_indexers=5, num_sites=1, num_accelerated_dm=0,
        compression_rawdata=0.15, compression_tsidx=0.35,
        hotwarm_retention_days=30,
    )
    res = compute_sizing(inp)
    st = res.storage
    # rawdata cluster = 1000 * 0.15 * 3 * 100 = 45 000 Go
    assert math.isclose(st.rawdata_cluster_gb, 1000 * 0.15 * 3 * 100)
    # tsidx cluster = 1000 * 0.35 * 2 * 100 = 70 000 Go
    assert math.isclose(st.tsidx_cluster_gb, 1000 * 0.35 * 2 * 100)


def test_per_day_one_copy():
    inp = SizingInput(daily_volume_gb=200, retention_days=10,
                      rf=Factor(total=2), sf=Factor(total=2),
                      num_indexers=2)
    pd = compute_sizing(inp).storage.per_day
    assert math.isclose(pd.rawdata_one_copy_gb, 200 * 0.15)
    assert math.isclose(pd.tsidx_one_copy_gb, 200 * 0.35)


def test_margin_applied():
    inp = SizingInput(daily_volume_gb=1000, retention_days=100,
                      rf=Factor(total=2), sf=Factor(total=2),
                      num_indexers=4, target_disk_utilisation=0.80,
                      num_accelerated_dm=0)
    st = compute_sizing(inp).storage
    expected_brut = st.data_total_cluster_gb / 0.80
    assert math.isclose(st.raw_capacity_cluster_gb, expected_brut)
    assert math.isclose(st.raw_capacity_per_indexer_gb, expected_brut / 4)


# --------------------------------------------------------------------------
# Data model acceleration
# --------------------------------------------------------------------------
def test_dm_acceleration_adds_storage():
    base = SizingInput(daily_volume_gb=1000, retention_days=100,
                       rf=Factor(total=2), sf=Factor(total=2),
                       num_indexers=4, num_accelerated_dm=0)
    with_dm = SizingInput(daily_volume_gb=1000, retention_days=100,
                          rf=Factor(total=2), sf=Factor(total=2),
                          num_indexers=4, num_accelerated_dm=5,
                          dm_accel_retention_days=7)
    s0 = compute_sizing(base).storage
    s1 = compute_sizing(with_dm).storage
    assert s1.dm_accel_cluster_gb > 0
    assert s0.dm_accel_cluster_gb == 0
    assert s1.data_total_cluster_gb > s0.data_total_cluster_gb


# --------------------------------------------------------------------------
# Tiering
# --------------------------------------------------------------------------
def test_tiering_split():
    inp = SizingInput(daily_volume_gb=1000, retention_days=100,
                      hotwarm_retention_days=30,
                      rf=Factor(total=2), sf=Factor(total=2),
                      num_indexers=4, num_accelerated_dm=0)
    st = compute_sizing(inp).storage
    # hot/warm represente 30/100 des donnees bucket
    total_bucket = st.rawdata_cluster_gb + st.tsidx_cluster_gb
    assert math.isclose(st.hotwarm_cluster_gb, total_bucket * 0.30, rel_tol=1e-9)
    assert math.isclose(st.cold_cluster_gb, total_bucket * 0.70, rel_tol=1e-9)


def test_no_cold_when_hotwarm_covers_all():
    inp = SizingInput(daily_volume_gb=1000, retention_days=30,
                      hotwarm_retention_days=30,
                      rf=Factor(total=2), sf=Factor(total=2),
                      num_indexers=4, num_accelerated_dm=0)
    st = compute_sizing(inp).storage
    assert math.isclose(st.cold_cluster_gb, 0.0, abs_tol=1e-9)


# --------------------------------------------------------------------------
# Warnings / cas limites
# --------------------------------------------------------------------------
def test_sf_gt_rf_is_error():
    inp = SizingInput(daily_volume_gb=1000, retention_days=100,
                      rf=Factor(total=2), sf=Factor(total=3),
                      num_indexers=4)
    res = compute_sizing(inp)
    assert any(w.level == "ERREUR" and "SF.total" in w.message
               for w in res.warnings)


def test_shc_under_3_members_warns():
    inp = SizingInput(daily_volume_gb=500, retention_days=90,
                      rf=Factor(total=2), sf=Factor(total=2),
                      num_indexers=4, num_search_heads=2)
    res = compute_sizing(inp)
    assert any("3 membres" in w.message or "minimum" in w.message
               for w in res.warnings)


def test_origin_gt_total_error():
    inp = SizingInput(daily_volume_gb=500, retention_days=90,
                      num_sites=2,
                      rf=Factor(total=2, origin=3), sf=Factor(total=2, origin=1),
                      num_indexers=4)
    res = compute_sizing(inp)
    assert any(w.level == "ERREUR" and "origin" in w.message
               for w in res.warnings)


def test_insufficient_peers_per_site():
    # origin:2 mais 2 indexers / 2 sites = 1 par site < 2
    inp = SizingInput(daily_volume_gb=500, retention_days=90,
                      num_sites=2,
                      rf=Factor(total=3, origin=2), sf=Factor(total=2, origin=1),
                      num_indexers=2)
    res = compute_sizing(inp)
    assert any("peers par site" in w.message or "indexer(s)/site" in w.message
               for w in res.warnings)


def test_zero_volume_blocks():
    inp = SizingInput(daily_volume_gb=0, retention_days=100,
                      num_indexers=4)
    res = compute_sizing(inp)
    assert any(w.level == "ERREUR" for w in res.warnings)
    assert res.storage.raw_capacity_cluster_gb == 0


def test_even_shc_members_info():
    inp = SizingInput(daily_volume_gb=500, retention_days=90,
                      rf=Factor(total=2), sf=Factor(total=2),
                      num_indexers=4, num_search_heads=4)
    res = compute_sizing(inp)
    assert any("IMPAIR" in w.message or "impair" in w.message
               for w in res.warnings)


# --------------------------------------------------------------------------
# Profil hardware
# --------------------------------------------------------------------------
def test_high_volume_picks_high_profile():
    # 1000 Go/jour sur 2 indexers = 500/idx > 200 -> high-perf
    inp = SizingInput(daily_volume_gb=1000, retention_days=90,
                      rf=Factor(total=2), sf=Factor(total=2),
                      num_indexers=2, use_es=True)
    res = compute_sizing(inp)
    idx = next(h for h in res.hardware if "Indexer" in h.role)
    assert idx.vcpu_each == 96  # high-perf


def test_multisite_storage_uses_total():
    # Le stockage doit dependre de RF.total/SF.total, pas de origin.
    a = SizingInput(daily_volume_gb=1000, retention_days=100, num_sites=2,
                    rf=Factor(total=3, origin=2), sf=Factor(total=2, origin=1),
                    num_indexers=6, num_accelerated_dm=0)
    b = SizingInput(daily_volume_gb=1000, retention_days=100, num_sites=2,
                    rf=Factor(total=3, origin=1), sf=Factor(total=2, origin=1),
                    num_indexers=6, num_accelerated_dm=0)
    # origin different, total identique => meme stockage
    sa = compute_sizing(a).storage
    sb = compute_sizing(b).storage
    assert math.isclose(sa.rawdata_cluster_gb, sb.rawdata_cluster_gb)
    assert math.isclose(sa.tsidx_cluster_gb, sb.tsidx_cluster_gb)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
