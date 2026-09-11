"""
sizing_engine.py
================
Moteur de dimensionnement d'une infrastructure Splunk distribuee multisite.

Code PUR (aucune dependance UI/Streamlit) : prend un objet `SizingInput`,
retourne un objet `SizingResult`. Testable et reutilisable (FastAPI, CLI...).

------------------------------------------------------------------------------
MODELE DE CALCUL & FORMULES (documentees pour transparence)
------------------------------------------------------------------------------

Notations :
    V_raw   = volume quotidien de donnees pre-indexees (Go/jour)
    c_raw   = ratio compression rawdata (~0.15, OFFICIEL)
    c_tsidx = ratio compression tsidx   (~0.35, OFFICIEL)
    RF      = Replication Factor total (nb de copies du rawdata dans le cluster)
    SF      = Search Factor total       (nb de copies searchable -> tsidx)
    R       = retention (jours)
    U       = utilisation disque cible (~0.80)
    N       = nombre d'indexers (total cluster)

1) Empreinte d'UNE copie, par jour :
       rawdata_1copie  = V_raw * c_raw
       tsidx_1copie    = V_raw * c_tsidx
   (somme ~= 50% de V_raw : estimation officielle de compression)

2) Empreinte CLUSTER par jour, en tenant compte de la replication :
   Le rawdata est replique RF fois, les tsidx sont presents sur les copies
   searchable donc SF fois :
       rawdata_cluster_jour = V_raw * c_raw   * RF
       tsidx_cluster_jour   = V_raw * c_tsidx * SF
       total_cluster_jour   = rawdata_cluster_jour + tsidx_cluster_jour

3) Sur la retention :
       total_cluster = total_cluster_jour * R
   (la retention peut differer par tier : on calcule hot/warm et cold
    separement puis on additionne — cf. tiering ci-dessous.)

4) Data Model Acceleration (HPAS) — surcout tsidx de data model, distinct
   des tsidx de bucket :
       dm_cluster = V_raw * f_dm * n_dm * R_dm * SF
   ou f_dm est un FACTEUR ESTIME (TERRAIN, a calibrer), n_dm le nombre de
   DM acceleres, R_dm la retention d'acceleration. On le multiplie par SF
   car les summaries vivent avec les copies searchable.

5) Capacite BRUTE necessaire (avec marge) :
       brut_cluster = (total_cluster + dm_cluster) / U
       brut_par_indexer = brut_cluster / N

------------------------------------------------------------------------------
MULTISITE : logique origin / total
------------------------------------------------------------------------------
En multisite, RF et SF s'expriment "origin:x, total:y" :
    - origin = nb de copies sur le site d'ORIGINE de la donnee
    - total  = nb de copies au TOTAL dans le cluster (tous sites)
Contraintes Splunk :
    - total >= origin
    - SF.total <= RF.total  et  SF.origin <= RF.origin
    - pour satisfaire origin:k, il faut au moins k peers sur le site source

Pour le STOCKAGE, ce qui compte est le nombre TOTAL de copies dans le
cluster : on utilise donc RF.total et SF.total pour l'empreinte globale.
La notion origin sert surtout aux WARNINGS de placement / tolerance de
panne (assez de peers par site pour reconstruire les copies origin).

La repartition par site suppose une distribution equilibree des indexers
entre sites (hypothese explicite ; un desequilibre reel se gere a la main).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict
import math

from references import (
    COMPRESSION_RAWDATA,
    COMPRESSION_TSIDX,
    TARGET_DISK_UTILISATION,
    VOLUME_PER_INDEXER_MIN,
    VOLUME_PER_INDEXER_MAX,
    VOLUME_PER_INDEXER_ES,
    IOPS_MIN,
    IOPS_RECOMMENDED_HOTWARM,
    CPU_CORES_PER_150GB_INDEXING,
    CPU_CORES_PER_CONCURRENT_SEARCH,
    SHC_MIN_MEMBERS,
    DM_ACCEL_FACTOR,
    INDEXER_BASE,
    INDEXER_ES,
    INDEXER_MID,
    INDEXER_HIGH,
    SEARCH_HEAD,
    CLUSTER_MANAGER,
    DEPLOYER,
    DEPLOYMENT_SERVER,
    MONITORING_CONSOLE,
    RoleSpec,
)


# ===========================================================================
# Facteurs / replication
# ===========================================================================
@dataclass
class Factor:
    """Replication ou Search Factor, mono- ou multi-site.

    En mono-site, seul `total` est utilise (origin laisse a None).
    En multisite, `origin` et `total` sont tous deux renseignes.
    """
    total: int
    origin: Optional[int] = None  # None => mono-site

    def is_multisite(self) -> bool:
        return self.origin is not None

    def __str__(self) -> str:
        if self.is_multisite():
            return f"origin:{self.origin}, total:{self.total}"
        return str(self.total)


# ===========================================================================
# Entrees
# ===========================================================================
@dataclass
class SizingInput:
    """Tous les parametres d'entree du dimensionnement.

    Les unites de volume sont en Go/jour en interne. L'UI peut convertir
    To <-> Go avant d'appeler le moteur.
    """
    # --- Volume & retention ---
    daily_volume_gb: float                 # Go/jour (volume de licence)
    retention_days: int                    # retention globale (jours)
    # Tiering : retention hot/warm (SSD) ; le reste va en cold.
    hotwarm_retention_days: int = 30       # retention hot/warm (defaut 30j)

    # --- Replication / Search factors ---
    rf: Factor = field(default_factory=lambda: Factor(total=3))
    sf: Factor = field(default_factory=lambda: Factor(total=2))

    # --- Topologie ---
    num_sites: int = 1
    num_indexers: int = 4                  # total cluster
    num_search_heads: int = 3

    # --- Compression (surchargables) ---
    compression_rawdata: float = COMPRESSION_RAWDATA.value
    compression_tsidx: float = COMPRESSION_TSIDX.value

    # --- Marge ---
    target_disk_utilisation: float = TARGET_DISK_UTILISATION.value

    # --- Data model acceleration ---
    num_accelerated_dm: int = 0
    dm_accel_retention_days: int = 7       # summariesRange typique
    dm_accel_factor: float = DM_ACCEL_FACTOR.value

    # --- Charge de recherche ---
    concurrent_searches: int = 8           # recherches simultanees estimees

    # --- Options ---
    use_es: bool = True                    # premium app ES -> seuils ES
    smartstore_cold: bool = False          # cold sur SmartStore (S3) ?


# ===========================================================================
# Sorties — sous-structures
# ===========================================================================
@dataclass
class StoragePerDay:
    rawdata_one_copy_gb: float
    tsidx_one_copy_gb: float
    rawdata_cluster_gb: float
    tsidx_cluster_gb: float
    total_cluster_gb: float


@dataclass
class StorageResult:
    per_day: StoragePerDay
    # Empreinte sur retention (donnees, hors marge)
    rawdata_cluster_gb: float
    tsidx_cluster_gb: float
    dm_accel_cluster_gb: float
    data_total_cluster_gb: float           # raw+tsidx+dm, hors marge
    # Avec marge (capacite brute a provisionner)
    raw_capacity_cluster_gb: float
    raw_capacity_per_indexer_gb: float
    # Tiering
    hotwarm_cluster_gb: float
    cold_cluster_gb: float
    hotwarm_per_indexer_gb: float
    cold_per_indexer_gb: float


@dataclass
class TierRecommendation:
    name: str
    retention_days: int
    cluster_gb: float
    per_indexer_gb: float
    media: str
    iops_min: float
    iops_reco: float
    note: str


@dataclass
class CpuRamRecommendation:
    role: str
    count: int
    vcpu_each: int
    ram_gb_each: int
    source: str
    note: str
    disk_note: str = ""


@dataclass
class Warning:
    level: str          # "ERREUR", "ALERTE", "INFO"
    message: str


@dataclass
class SizingResult:
    inp: SizingInput
    storage: StorageResult
    tiers: List[TierRecommendation]
    hardware: List[CpuRamRecommendation]
    warnings: List[Warning]
    architecture_notes: List[str]
    assumptions: Dict[str, str]            # formules / hypotheses affichables


# ===========================================================================
# Helpers
# ===========================================================================
def _gb_to_tb(gb: float) -> float:
    return gb / 1024.0


def _indexers_per_site(num_indexers: int, num_sites: int) -> int:
    """Repartition equilibree (hypothese). Arrondi bas pour le pire cas."""
    if num_sites <= 0:
        return num_indexers
    return num_indexers // num_sites


# ===========================================================================
# Validation des entrees -> warnings (sans lever d'exception sauf valeurs
# absurdes qui empecheraient tout calcul).
# ===========================================================================
def _validate(inp: SizingInput) -> List[Warning]:
    w: List[Warning] = []

    # Valeurs nulles / negatives bloquantes
    if inp.daily_volume_gb <= 0:
        w.append(Warning("ERREUR", "Le volume quotidien doit etre > 0 Go/jour."))
    if inp.retention_days <= 0:
        w.append(Warning("ERREUR", "La retention doit etre > 0 jour."))
    if inp.num_indexers <= 0:
        w.append(Warning("ERREUR", "Le nombre d'indexers doit etre >= 1."))
    if inp.num_sites <= 0:
        w.append(Warning("ERREUR", "Le nombre de sites doit etre >= 1."))

    # SF > RF : impossible (on ne peut pas rendre searchable plus de copies
    # qu'il n'en existe).
    if inp.sf.total > inp.rf.total:
        w.append(Warning(
            "ERREUR",
            f"SF.total ({inp.sf.total}) > RF.total ({inp.rf.total}) : "
            "impossible. Le Search Factor ne peut depasser le Replication "
            "Factor (on ne rend pas searchable plus de copies qu'il n'existe).",
        ))

    # Coherence multisite origin/total
    for name, fac in (("RF", inp.rf), ("SF", inp.sf)):
        if fac.is_multisite():
            if fac.origin is not None and fac.origin > fac.total:
                w.append(Warning(
                    "ERREUR",
                    f"{name} origin ({fac.origin}) > total ({fac.total}) : "
                    "incoherent. origin doit etre <= total.",
                ))
    if inp.rf.is_multisite() and inp.sf.is_multisite():
        if inp.sf.origin is not None and inp.rf.origin is not None:
            if inp.sf.origin > inp.rf.origin:
                w.append(Warning(
                    "ERREUR",
                    f"SF.origin ({inp.sf.origin}) > RF.origin "
                    f"({inp.rf.origin}) : impossible.",
                ))

    # Multisite declare mais facteurs mono-site (ou inverse)
    if inp.num_sites >= 2 and not (inp.rf.is_multisite() or inp.sf.is_multisite()):
        w.append(Warning(
            "ALERTE",
            f"{inp.num_sites} sites declares mais RF/SF mono-site. "
            "En multisite, utilisez la notation origin:x,total:y pour "
            "garantir le placement des copies par site.",
        ))
    if inp.num_sites == 1 and (inp.rf.is_multisite() or inp.sf.is_multisite()):
        w.append(Warning(
            "ALERTE",
            "Facteurs multisite (origin:...) declares avec un seul site. "
            "L'attribut origin n'a de sens qu'avec 2+ sites.",
        ))

    # Peers par site suffisants pour satisfaire origin
    if inp.num_sites >= 2 and inp.rf.is_multisite() and inp.rf.origin:
        per_site = _indexers_per_site(inp.num_indexers, inp.num_sites)
        if per_site < inp.rf.origin:
            w.append(Warning(
                "ALERTE",
                f"~{per_site} indexer(s)/site < RF.origin "
                f"({inp.rf.origin}). Pas assez de peers par site pour "
                "satisfaire les copies origin : ajoutez des indexers par site.",
            ))
        # Tolerance de panne : pour survivre a la perte d'un peer en gardant
        # origin satisfait, il faut origin+1 peers par site.
        if per_site < inp.rf.origin + 1:
            w.append(Warning(
                "INFO",
                f"Pour tolerer la panne d'un peer en gardant origin:"
                f"{inp.rf.origin} satisfait, prevoir au moins "
                f"{inp.rf.origin + 1} indexers par site (N+1).",
            ))

    # SHC quorum
    if inp.num_search_heads < SHC_MIN_MEMBERS.value and inp.num_search_heads > 1:
        w.append(Warning(
            "ALERTE",
            f"{inp.num_search_heads} search heads : un Search Head Cluster "
            f"requiert au minimum {SHC_MIN_MEMBERS.value} membres pour le "
            "quorum / l'election du captain.",
        ))
    if inp.num_search_heads >= SHC_MIN_MEMBERS.value and inp.num_search_heads % 2 == 0:
        w.append(Warning(
            "INFO",
            f"{inp.num_search_heads} membres SHC (pair) : un nombre IMPAIR "
            "est recommande pour eviter les egalites de vote (split-brain).",
        ))

    # Compression hors plage raisonnable
    if not (0 < inp.compression_rawdata < 1):
        w.append(Warning("ALERTE", "Compression rawdata hors ]0,1[ : verifiez."))
    if not (0 < inp.compression_tsidx < 1):
        w.append(Warning("ALERTE", "Compression tsidx hors ]0,1[ : verifiez."))

    # Utilisation disque
    if not (0 < inp.target_disk_utilisation <= 1):
        w.append(Warning("ALERTE", "Utilisation disque cible hors ]0,1]."))
    elif inp.target_disk_utilisation > 0.90:
        w.append(Warning(
            "INFO",
            f"Utilisation cible {inp.target_disk_utilisation:.0%} elevee : "
            "laisser plus de marge (~80%) pour le working space de recherche.",
        ))

    # Retention hot/warm coherente
    if inp.hotwarm_retention_days > inp.retention_days:
        w.append(Warning(
            "ALERTE",
            "Retention hot/warm > retention totale : ajustement a la "
            "retention totale (pas de tier cold dans ce cas).",
        ))

    return w


# ===========================================================================
# Calcul stockage
# ===========================================================================
def _compute_storage(inp: SizingInput) -> StorageResult:
    V = inp.daily_volume_gb
    c_raw = inp.compression_rawdata
    c_tsidx = inp.compression_tsidx
    RF = inp.rf.total
    SF = inp.sf.total
    U = inp.target_disk_utilisation if inp.target_disk_utilisation > 0 else 0.80
    N = max(inp.num_indexers, 1)

    # 1) Une copie / jour
    raw_1 = V * c_raw
    tsidx_1 = V * c_tsidx

    # 2) Cluster / jour (replication)
    raw_cluster_day = raw_1 * RF
    tsidx_cluster_day = tsidx_1 * SF
    total_cluster_day = raw_cluster_day + tsidx_cluster_day

    per_day = StoragePerDay(
        rawdata_one_copy_gb=raw_1,
        tsidx_one_copy_gb=tsidx_1,
        rawdata_cluster_gb=raw_cluster_day,
        tsidx_cluster_gb=tsidx_cluster_day,
        total_cluster_gb=total_cluster_day,
    )

    # 3) Sur retention
    R = inp.retention_days
    rawdata_cluster = raw_cluster_day * R
    tsidx_cluster = tsidx_cluster_day * R

    # 4) Data model acceleration (HPAS)
    dm_cluster = 0.0
    if inp.num_accelerated_dm > 0:
        dm_cluster = (
            V * inp.dm_accel_factor
            * inp.num_accelerated_dm
            * inp.dm_accel_retention_days
            * SF
        )

    data_total = rawdata_cluster + tsidx_cluster + dm_cluster

    # 5) Capacite brute (marge)
    raw_capacity_cluster = data_total / U
    raw_capacity_per_indexer = raw_capacity_cluster / N

    # --- Tiering hot/warm vs cold ---
    hw_ret = min(inp.hotwarm_retention_days, R)
    cold_ret = max(R - hw_ret, 0)
    # Fraction de la retention en hot/warm
    hw_frac = hw_ret / R if R > 0 else 0.0
    cold_frac = cold_ret / R if R > 0 else 0.0
    # Le surcout DM accel vit en hot/warm (acceleration recente).
    hotwarm_cluster = (rawdata_cluster + tsidx_cluster) * hw_frac + dm_cluster
    cold_cluster = (rawdata_cluster + tsidx_cluster) * cold_frac
    # Avec marge, par indexer
    hotwarm_per_indexer = (hotwarm_cluster / U) / N
    cold_per_indexer = (cold_cluster / U) / N

    return StorageResult(
        per_day=per_day,
        rawdata_cluster_gb=rawdata_cluster,
        tsidx_cluster_gb=tsidx_cluster,
        dm_accel_cluster_gb=dm_cluster,
        data_total_cluster_gb=data_total,
        raw_capacity_cluster_gb=raw_capacity_cluster,
        raw_capacity_per_indexer_gb=raw_capacity_per_indexer,
        hotwarm_cluster_gb=hotwarm_cluster,
        cold_cluster_gb=cold_cluster,
        hotwarm_per_indexer_gb=hotwarm_per_indexer,
        cold_per_indexer_gb=cold_per_indexer,
    )


# ===========================================================================
# Tiering recommandations
# ===========================================================================
def _compute_tiers(inp: SizingInput, st: StorageResult) -> List[TierRecommendation]:
    tiers: List[TierRecommendation] = []
    hw_ret = min(inp.hotwarm_retention_days, inp.retention_days)
    cold_ret = max(inp.retention_days - hw_ret, 0)

    tiers.append(TierRecommendation(
        name="Hot / Warm",
        retention_days=hw_ret,
        cluster_gb=st.hotwarm_cluster_gb,
        per_indexer_gb=st.hotwarm_per_indexer_gb,
        media="SSD / NVMe",
        iops_min=IOPS_MIN.value,
        iops_reco=IOPS_RECOMMENDED_HOTWARM.value,
        note="Recherches recentes intensives. Volume index separe de l'OS. "
             "Inclut le surcout tsidx des data models acceleres.",
    ))

    if cold_ret > 0:
        if inp.smartstore_cold:
            tiers.append(TierRecommendation(
                name="Cold (SmartStore)",
                retention_days=cold_ret,
                cluster_gb=st.cold_cluster_gb,
                per_indexer_gb=st.cold_per_indexer_gb,
                media="Object store S3 (+ cache local)",
                iops_min=IOPS_MIN.value,
                iops_reco=IOPS_MIN.value,
                note="SmartStore : donnees deportees sur object store, cache "
                     "local pour les buckets chauds. Dimensionner le cache "
                     "plutot que la capacite cold complete. Decouple stockage "
                     "et compute.",
            ))
        else:
            tiers.append(TierRecommendation(
                name="Cold (local/SAN)",
                retention_days=cold_ret,
                cluster_gb=st.cold_cluster_gb,
                per_indexer_gb=st.cold_per_indexer_gb,
                media="HDD / SAN / NAS",
                iops_min=IOPS_MIN.value,
                iops_reco=IOPS_MIN.value,
                note="Donnees anciennes peu recherchees. HDD acceptable. "
                     "Les recherches sur volumes reseau sont plus lentes.",
            ))

    return tiers


# ===========================================================================
# CPU / RAM recommandations
# ===========================================================================
def _pick_indexer_profile(inp: SizingInput) -> RoleSpec:
    """Choisit le profil indexer selon volume/indexer et usage ES."""
    vol_per_idx = inp.daily_volume_gb / max(inp.num_indexers, 1)
    if inp.use_es:
        base = INDEXER_ES
    else:
        base = INDEXER_BASE
    # Si la charge par indexer est elevee, monter en gamme.
    if vol_per_idx > 200:
        return INDEXER_HIGH
    if vol_per_idx > 100:
        return INDEXER_MID
    return base


def _compute_hardware(inp: SizingInput) -> List[CpuRamRecommendation]:
    recs: List[CpuRamRecommendation] = []

    # --- Indexers ---
    idx = _pick_indexer_profile(inp)
    recs.append(CpuRamRecommendation(
        role=idx.role, count=inp.num_indexers,
        vcpu_each=idx.vcpu, ram_gb_each=idx.ram_gb,
        source=idx.source, note=idx.note, disk_note=idx.disk_note,
    ))

    # --- Search heads ---
    # SH de base 32 vCPU. On verifie la marge vs concurrence de recherche
    # (1 coeur/recherche active). Si concurrence > capacite d'un SH, on le
    # signale via la note (le SHC repartit la charge entre membres).
    sh = SEARCH_HEAD
    per_sh_capacity = sh.vcpu  # ~1 recherche par vCPU
    total_capacity = per_sh_capacity * inp.num_search_heads
    sh_note = sh.note
    if inp.concurrent_searches > total_capacity:
        sh_note += (
            f" | Charge estimee {inp.concurrent_searches} recherches "
            f"concurrentes > capacite SHC (~{total_capacity} coeurs sur "
            f"{inp.num_search_heads} SH) : ajouter des membres ou des coeurs."
        )
    recs.append(CpuRamRecommendation(
        role=sh.role, count=inp.num_search_heads,
        vcpu_each=sh.vcpu, ram_gb_each=sh.ram_gb,
        source=sh.source, note=sh_note, disk_note=sh.disk_note,
    ))

    # --- Composants de management ---
    cm = CLUSTER_MANAGER
    # CM : monter la RAM avec la taille du cluster (nb de buckets ~ volume).
    cm_ram = cm.ram_gb
    if inp.num_indexers > 10 or inp.daily_volume_gb > 1024:
        cm_ram = max(cm_ram, 32)
    recs.append(CpuRamRecommendation(
        role=cm.role, count=1 if inp.num_sites == 1 else 1,
        vcpu_each=cm.vcpu, ram_gb_each=cm_ram,
        source=cm.source,
        note=cm.note + (
            " | Le CM gere l'ensemble du cluster (mono-CM meme en multisite). "
            "Prevoir un standby pour la HA du CM."
        ),
        disk_note=cm.disk_note,
    ))

    if inp.num_search_heads >= SHC_MIN_MEMBERS.value:
        dp = DEPLOYER
        recs.append(CpuRamRecommendation(
            role=dp.role, count=1, vcpu_each=dp.vcpu, ram_gb_each=dp.ram_gb,
            source=dp.source, note=dp.note,
        ))

    mc = MONITORING_CONSOLE
    recs.append(CpuRamRecommendation(
        role=mc.role, count=1, vcpu_each=mc.vcpu, ram_gb_each=mc.ram_gb,
        source=mc.source, note=mc.note,
    ))

    ds = DEPLOYMENT_SERVER
    recs.append(CpuRamRecommendation(
        role=ds.role, count=1, vcpu_each=ds.vcpu, ram_gb_each=ds.ram_gb,
        source=ds.source, note=ds.note,
    ))

    return recs


# ===========================================================================
# Notes d'architecture textuelles
# ===========================================================================
def _architecture_notes(inp: SizingInput, st: StorageResult) -> List[str]:
    notes: List[str] = []
    vol_per_idx = inp.daily_volume_gb / max(inp.num_indexers, 1)

    # Volume par indexer vs rule of thumb
    seuil = VOLUME_PER_INDEXER_ES.value if inp.use_es else VOLUME_PER_INDEXER_MAX.value
    if vol_per_idx > seuil:
        notes.append(
            f"Volume par indexer ~{vol_per_idx:.0f} Go/jour, au-dessus du "
            f"seuil indicatif ({seuil:.0f} Go/jour"
            f"{' avec ES' if inp.use_es else ''}, TERRAIN). "
            "La performance de recherche risque de souffrir : envisager "
            "d'ajouter des indexers."
        )
    else:
        notes.append(
            f"Volume par indexer ~{vol_per_idx:.0f} Go/jour, dans la "
            f"fourchette indicative ({VOLUME_PER_INDEXER_MIN.value:.0f}"
            f"-{seuil:.0f} Go/jour, TERRAIN)."
        )

    # Tolerance de panne multisite
    if inp.num_sites >= 2:
        per_site = _indexers_per_site(inp.num_indexers, inp.num_sites)
        origin = inp.rf.origin if inp.rf.is_multisite() else None
        if origin:
            notes.append(
                f"Multisite {inp.num_sites} sites, ~{per_site} indexer(s)/site. "
                f"Pour tolerer la panne d'un peer en gardant origin:{origin} "
                f"satisfait, prevoir {origin + 1} indexers/site (regle N+1). "
                f"Configuration cible recommandee : {origin + 1} x {inp.num_sites} "
                f"= {(origin + 1) * inp.num_sites} indexers minimum."
            )
        notes.append(
            "En multisite, un seul Cluster Manager pilote le cluster ; "
            "prevoir un CM standby pour la haute disponibilite du role."
        )

    # SmartStore
    if inp.smartstore_cold:
        notes.append(
            "SmartStore active pour le cold : dimensionner le cache local "
            "(hot/warm + buckets recemment recherches) plutot que la "
            "capacite cold totale. Le stockage objet absorbe la retention "
            "longue et decouple capacite et compute."
        )
    elif st.cold_cluster_gb > 0:
        notes.append(
            "Cold sur stockage local/SAN : alternative SmartStore (object "
            "store S3) interessante si la retention longue domine le sizing "
            "et pour decoupler stockage et compute."
        )

    # Replication / searchability
    notes.append(
        f"RF={inp.rf} / SF={inp.sf} : le rawdata est conserve en "
        f"{inp.rf.total} copies (tolerance de panne), {inp.sf.total} d'entre "
        f"elles sont searchable (tsidx presents). SF<RF = degrade gracieux : "
        "des copies restent recuperables sans etre immediatement searchable."
    )

    return notes


# ===========================================================================
# Hypotheses / formules affichables (transparence UI)
# ===========================================================================
def _assumptions(inp: SizingInput) -> Dict[str, str]:
    return {
        "Compression rawdata":
            f"{inp.compression_rawdata:.0%} du volume (OFFICIEL Splunk)",
        "Compression tsidx":
            f"{inp.compression_tsidx:.0%} du volume (OFFICIEL Splunk)",
        "Empreinte rawdata cluster/jour":
            "V x compression_rawdata x RF.total",
        "Empreinte tsidx cluster/jour":
            "V x compression_tsidx x SF.total",
        "Total sur retention":
            "(rawdata_jour + tsidx_jour) x retention",
        "Surcout DM accel":
            "V x facteur_DM x nb_DM x retention_accel x SF.total "
            f"(facteur estime {inp.dm_accel_factor:.1%}/DM/jour, TERRAIN)",
        "Capacite brute":
            "(donnees + DM) / utilisation_cible",
        "Par indexer":
            "capacite_brute_cluster / nb_indexers (repartition equilibree)",
        "Multisite":
            "Stockage = RF.total/SF.total (copies totales). origin sert au "
            "placement par site et a la tolerance de panne (N+1/site).",
        "CPU recherche":
            "1 recherche active ~= 1 coeur sur le SH et sur chaque indexer "
            "(OFFICIEL).",
    }


# ===========================================================================
# Point d'entree principal
# ===========================================================================
def compute_sizing(inp: SizingInput) -> SizingResult:
    """Calcule le dimensionnement complet a partir des entrees.

    Ne leve pas d'exception sur entrees discutables : remonte des Warning.
    Bloque le calcul numerique uniquement si une ERREUR rend les formules
    indefinies (volume/indexers/retention <= 0).
    """
    warnings = _validate(inp)

    blocking = any(w.level == "ERREUR" and (
        inp.daily_volume_gb <= 0 or inp.retention_days <= 0
        or inp.num_indexers <= 0 or inp.num_sites <= 0
    ) for w in warnings)

    # On calcule quand meme si SF>RF (erreur logique) pour montrer l'impact,
    # mais on bloque sur les valeurs qui rendent les formules indefinies.
    if blocking:
        empty_day = StoragePerDay(0, 0, 0, 0, 0)
        empty_storage = StorageResult(
            per_day=empty_day, rawdata_cluster_gb=0, tsidx_cluster_gb=0,
            dm_accel_cluster_gb=0, data_total_cluster_gb=0,
            raw_capacity_cluster_gb=0, raw_capacity_per_indexer_gb=0,
            hotwarm_cluster_gb=0, cold_cluster_gb=0,
            hotwarm_per_indexer_gb=0, cold_per_indexer_gb=0,
        )
        return SizingResult(
            inp=inp, storage=empty_storage, tiers=[], hardware=[],
            warnings=warnings, architecture_notes=[
                "Calcul impossible : corrigez les erreurs (valeurs <= 0)."
            ],
            assumptions=_assumptions(inp),
        )

    storage = _compute_storage(inp)
    tiers = _compute_tiers(inp, storage)
    hardware = _compute_hardware(inp)
    notes = _architecture_notes(inp, storage)
    assumptions = _assumptions(inp)

    return SizingResult(
        inp=inp, storage=storage, tiers=tiers, hardware=hardware,
        warnings=warnings, architecture_notes=notes, assumptions=assumptions,
    )


# ===========================================================================
# Demo CLI rapide (valeurs d'exemple realistes)
# ===========================================================================
if __name__ == "__main__":
    example = SizingInput(
        daily_volume_gb=1024,                # 1 To/jour
        retention_days=365,
        hotwarm_retention_days=30,
        rf=Factor(total=3, origin=2),        # origin:2, total:3
        sf=Factor(total=2, origin=1),        # origin:1, total:2
        num_sites=2,
        num_indexers=4,
        num_search_heads=3,
        num_accelerated_dm=5,
        dm_accel_retention_days=7,
        concurrent_searches=12,
        use_es=True,
        smartstore_cold=False,
    )
    res = compute_sizing(example)
    s = res.storage
    print("=== STOCKAGE ===")
    print(f"  rawdata cluster (retention): {_gb_to_tb(s.rawdata_cluster_gb):.1f} To")
    print(f"  tsidx cluster   (retention): {_gb_to_tb(s.tsidx_cluster_gb):.1f} To")
    print(f"  DM accel cluster           : {_gb_to_tb(s.dm_accel_cluster_gb):.1f} To")
    print(f"  Capacite brute cluster     : {_gb_to_tb(s.raw_capacity_cluster_gb):.1f} To")
    print(f"  Capacite brute / indexer   : {_gb_to_tb(s.raw_capacity_per_indexer_gb):.1f} To")
    print(f"  Hot/warm / indexer         : {_gb_to_tb(s.hotwarm_per_indexer_gb):.1f} To")
    print(f"  Cold / indexer             : {_gb_to_tb(s.cold_per_indexer_gb):.1f} To")
    print("\n=== WARNINGS ===")
    for w in res.warnings:
        print(f"  [{w.level}] {w.message}")
    print("\n=== NOTES ARCHI ===")
    for n in res.architecture_notes:
        print(f"  - {n}")
