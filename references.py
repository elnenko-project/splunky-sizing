"""
references.py
=============
Constantes de référence Splunk utilisées par le moteur de dimensionnement.

PRINCIPE DE TRANSPARENCE
------------------------
Chaque valeur est annotée par sa SOURCE et son niveau de confiance :

    - OFFICIEL : valeur tirée de la documentation officielle Splunk
                 (Capacity Planning Manual, Reference Hardware, SVA,
                 ES Performance Reference). Vérifiée juin 2026.
    - TERRAIN  : règle empirique communément admise (Splunk Lantern,
                 Splunk Community, SplunkTrust, partenaires). Indicative,
                 dépend fortement du type de données et de la charge de
                 recherche. À ajuster selon le contexte.

Toutes ces valeurs sont des DEFAUTS surchargeables depuis l'UI : rien
n'est codé en dur de façon opaque dans le moteur de calcul.

Références consultées (juin 2026) :
  - Estimate your storage requirements (Splunk Enterprise 10.0 / latest)
  - Reference hardware (Splunk Enterprise 10.0 / latest)
  - Performance reference for Splunk Enterprise Security 7.x
  - Splunk Lantern - Platform capacity considerations
  - Splunk Community / SplunkTrust (rule-of-thumb volume par indexer)
"""

from dataclasses import dataclass, field
from typing import Dict


# ---------------------------------------------------------------------------
# Petit conteneur pour tracer la provenance d'une valeur de référence.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Ref:
    """Une valeur de référence accompagnée de sa source et d'un commentaire."""
    value: float
    source: str           # "OFFICIEL" ou "TERRAIN"
    note: str             # explication courte / provenance


# ===========================================================================
# 1. COMPRESSION / STOCKAGE
# ===========================================================================
# OFFICIEL — Splunk : "rawdata ~15% des données pré-indexées, tsidx ~35%,
# soit ~50% au total". Ces deux ratios sont les valeurs canoniques utilisées
# pour estimer le stockage à partir du volume de licence quotidien.
COMPRESSION_RAWDATA = Ref(
    0.15, "OFFICIEL",
    "rawdata ~= 15% des donnees pre-indexees (Splunk Capacity Manual)",
)
COMPRESSION_TSIDX = Ref(
    0.35, "OFFICIEL",
    "tsidx ~= 35% des donnees pre-indexees (Splunk Capacity Manual)",
)
# => rawdata + tsidx ~= 50% : c'est l'estimation officielle de compression.


# ===========================================================================
# 2. MARGE / UTILISATION DISQUE
# ===========================================================================
# TERRAIN — on ne remplit jamais un volume a 100%. 75-85% de remplissage
# cible est la pratique courante pour laisser de la marge (working space de
# recherche, pics, bucket rolling). Splunk insiste sur le fait qu'il faut
# du "temp/working space" sur les indexers pour servir les recherches.
TARGET_DISK_UTILISATION = Ref(
    0.80, "TERRAIN",
    "Remplissage cible ~80% (marge pour working space, pics, rolling)",
)


# ===========================================================================
# 3. VOLUME PAR INDEXER (rule of thumb)
# ===========================================================================
# TERRAIN — "75 a 300 Go/jour/indexer selon la charge" (SplunkTrust).
# Avec une premium app type ES/ITSI, viser plutot < 100 Go/jour/indexer
# (Splunk Community). On expose une fourchette + une cible ES.
VOLUME_PER_INDEXER_MIN = Ref(
    75.0, "TERRAIN",
    "Min indicatif ~75 Go/jour/indexer (SplunkTrust rule of thumb)",
)
VOLUME_PER_INDEXER_MAX = Ref(
    300.0, "TERRAIN",
    "Max indicatif ~300 Go/jour/indexer hors premium app",
)
VOLUME_PER_INDEXER_ES = Ref(
    100.0, "TERRAIN",
    "Avec ES/ITSI viser < 100 Go/jour/indexer (Splunk Community)",
)


# ===========================================================================
# 4. IOPS
# ===========================================================================
# OFFICIEL — "le volume de stockage Splunk doit fournir au moins 800 IOPS
# soutenus". C'est un PLANCHER. Splunk benefice nettement au-dela.
IOPS_MIN = Ref(
    800.0, "OFFICIEL",
    "Plancher absolu : 800 IOPS soutenus (Reference Hardware)",
)
# TERRAIN — en pratique on vise bien plus haut pour hot/warm (SSD/NVMe).
# 1200+ couramment cite comme "confortable", et la doc evoque un ordre de
# grandeur ~4000 IOPS/indexer pour du SSD-level sur baie partagee.
IOPS_RECOMMENDED_HOTWARM = Ref(
    1200.0, "TERRAIN",
    "Cible confortable hot/warm >= 1200 IOPS (SSD/NVMe)",
)
IOPS_SSD_LEVEL_REFERENCE = Ref(
    4000.0, "OFFICIEL",
    "Ordre de grandeur SSD-level ~4000 IOPS/indexer (Reference Hardware)",
)


# ===========================================================================
# 5. REFERENCE HARDWARE PAR ROLE (vCPU / RAM)
# ===========================================================================
# OFFICIEL — Reference Hardware Splunk Enterprise + ES Performance Reference.
# Note : Splunk exprime souvent en "coeurs physiques" ; on fournit aussi
# l'equivalent vCPU (x2). Pour ES, les indexers de reference des tests de
# perf sont a 16 coeurs / 32 Go.
#
# Profils indexer :
#   base   : 12 coeurs / 24 vCPU, 32 Go  (reference standard)
#   mid    : 24 coeurs / 48 vCPU, 32 Go+ (overhead concurrence recherche)
#   high   : 48 coeurs / 96 vCPU         (premium apps / debit soutenu)
#
# ES recommande au minimum le profil 16 coeurs / 32 Go pour les indexers.

@dataclass(frozen=True)
class RoleSpec:
    """Spec hardware indicative pour un role Splunk."""
    role: str
    vcpu: int
    ram_gb: int
    source: str
    note: str
    disk_note: str = ""


# Indexers — profils
INDEXER_BASE = RoleSpec(
    "Indexer (base)", 24, 32, "OFFICIEL",
    "Reference hardware : 12 coeurs physiques / 24 vCPU, 32 Go RAM",
    disk_note="Hot/warm sur SSD/NVMe, volume index separe de l'OS",
)
INDEXER_ES = RoleSpec(
    "Indexer (ES)", 32, 32, "OFFICIEL",
    "ES Performance Reference : indexers a 16 coeurs / 32 vCPU, 32 Go",
    disk_note="ES augmente la charge de recherche : preferer SSD/NVMe",
)
INDEXER_MID = RoleSpec(
    "Indexer (mid-range)", 48, 64, "OFFICIEL",
    "Mid-range : 24 coeurs / 48 vCPU, RAM accrue pour concurrence recherche",
)
INDEXER_HIGH = RoleSpec(
    "Indexer (high-perf)", 96, 128, "OFFICIEL",
    "High-perf : 48 coeurs / 96 vCPU pour premium apps / debit soutenu",
)

# Search heads
SEARCH_HEAD = RoleSpec(
    "Search Head", 32, 32, "OFFICIEL",
    "Reference hardware SH : 16 coeurs physiques / 32 vCPU, 32 Go RAM",
    disk_note="Min ~300 Go dedies, SSD si forte charge ad-hoc/scheduled",
)

# Composants de management — TERRAIN : Splunk dit de partir de la spec
# single-instance (12c/24vCPU, 32 Go) puis d'ajuster a l'echelle. Les
# valeurs ci-dessous sont des points de depart indicatifs courants.
CLUSTER_MANAGER = RoleSpec(
    "Cluster Manager", 16, 16, "TERRAIN",
    "Point de depart ; monter en RAM/CPU avec le nb de buckets/peers",
    disk_note="Pas de stockage d'index ; disque modeste",
)
DEPLOYER = RoleSpec(
    "Deployer (SHC)", 8, 8, "TERRAIN",
    "Role peu sollicite ; distribue la config au SHC",
)
DEPLOYMENT_SERVER = RoleSpec(
    "Deployment Server", 12, 16, "TERRAIN",
    "Dimensionner selon le nb de forwarders/clients gere",
)
MONITORING_CONSOLE = RoleSpec(
    "Monitoring Console", 12, 16, "TERRAIN",
    "Peut etre colocalise sur petit env ; dedie si gros cluster",
)
LICENSE_MANAGER = RoleSpec(
    "License Manager", 8, 8, "TERRAIN",
    "Souvent colocalise avec le CM sur les deploiements modestes",
)


# ===========================================================================
# 6. CPU INDEXATION (rule of thumb)
# ===========================================================================
# TERRAIN — Splunk Lantern : "un indexer ingerant 150 Go/jour utilise ~4
# coeurs CPU pour l'indexation". On en derive un cout CPU par Go/jour pour
# estimer la part CPU consommee par l'indexation seule.
CPU_CORES_PER_150GB_INDEXING = Ref(
    4.0, "TERRAIN",
    "~4 coeurs pour 150 Go/jour d'indexation (Splunk Lantern)",
)


# ===========================================================================
# 7. RECHERCHE CONCURRENTE
# ===========================================================================
# OFFICIEL — "une recherche utilise jusqu'a 1 coeur CPU pendant son
# execution, sur le SH ET sur chaque indexer". Sert a dimensionner le CPU
# des SH et la marge de concurrence sur les indexers.
CPU_CORES_PER_CONCURRENT_SEARCH = Ref(
    1.0, "OFFICIEL",
    "1 recherche active = jusqu'a 1 coeur CPU (SH et chaque indexer)",
)


# ===========================================================================
# 8. SEARCH HEAD CLUSTER (quorum)
# ===========================================================================
# OFFICIEL — un SHC requiert au minimum 3 membres pour l'election du
# captain et le quorum (majorite). Nombre impair recommande.
SHC_MIN_MEMBERS = Ref(
    3, "OFFICIEL",
    "Minimum 3 membres pour quorum / election du captain (SHC)",
)


# ===========================================================================
# 9. DATA MODEL ACCELERATION (HPAS / tsidx de DM)
# ===========================================================================
# TERRAIN — Il n'existe pas de ratio officiel universel pour la taille des
# tsidx de data model accelere (HPAS). Cela depend enormement du DM, du
# nombre de champs accelere et du summary_range. On expose un FACTEUR
# ESTIME par defaut (fraction du volume source couvert, par jour d'accel),
# clairement signale comme estimation a calibrer.
DM_ACCEL_FACTOR = Ref(
    0.035, "TERRAIN",
    "ESTIMATION : ~3.5% du volume/jour par DM accelere et par jour "
    "d'acceleration (a calibrer ; aucun ratio officiel universel)",
)


# ===========================================================================
# Agregat exploitable par l'UI pour afficher le tableau des hypotheses.
# ===========================================================================
def all_references() -> Dict[str, Ref]:
    """Retourne les Ref scalaires pour affichage 'transparence' dans l'UI."""
    return {
        "Compression rawdata": COMPRESSION_RAWDATA,
        "Compression tsidx": COMPRESSION_TSIDX,
        "Utilisation disque cible": TARGET_DISK_UTILISATION,
        "Volume/indexer min": VOLUME_PER_INDEXER_MIN,
        "Volume/indexer max": VOLUME_PER_INDEXER_MAX,
        "Volume/indexer ES": VOLUME_PER_INDEXER_ES,
        "IOPS plancher": IOPS_MIN,
        "IOPS recommande hot/warm": IOPS_RECOMMENDED_HOTWARM,
        "CPU indexation /150Go": CPU_CORES_PER_150GB_INDEXING,
        "CPU /recherche concurrente": CPU_CORES_PER_CONCURRENT_SEARCH,
        "SHC membres min": SHC_MIN_MEMBERS,
        "Facteur tsidx DM accel": DM_ACCEL_FACTOR,
    }


def all_role_specs() -> Dict[str, RoleSpec]:
    """Specs hardware par role pour affichage."""
    return {
        "indexer_base": INDEXER_BASE,
        "indexer_es": INDEXER_ES,
        "indexer_mid": INDEXER_MID,
        "indexer_high": INDEXER_HIGH,
        "search_head": SEARCH_HEAD,
        "cluster_manager": CLUSTER_MANAGER,
        "deployer": DEPLOYER,
        "deployment_server": DEPLOYMENT_SERVER,
        "monitoring_console": MONITORING_CONSOLE,
        "license_manager": LICENSE_MANAGER,
    }
