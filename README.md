# Splunk Sizing Calculator

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

Calculateur de dimensionnement d'une infrastructure **Splunk distribuee multisite** :
stockage indexers, tiering hot/warm/cold, data model acceleration, CPU/RAM par role,
warnings de configuration et export JSON/PDF.

## Architecture du code

| Fichier | Role |
|---|---|
| `references.py` | Constantes Splunk **annotees par source** (OFFICIEL vs TERRAIN). Tout est surchargeable. |
| `sizing_engine.py` | **Moteur de calcul pur** (aucune dependance UI). `SizingInput -> SizingResult`. Toutes les formules documentees en tete de fichier. |
| `exporters.py` | Export **JSON** (fidele) et **PDF** (rapport mis en forme, reportlab). |
| `app.py` | **UI Streamlit** (presentation seulement). |
| `test_sizing_engine.py` | Tests unitaires (pytest). |

Separation stricte moteur / presentation : `sizing_engine.py` est reutilisable tel quel
dans une API FastAPI ou en CLI, sans rien reecrire.

## Installation & lancement

```bash
pip install -r requirements.txt
streamlit run app.py
```

L'application s'ouvre sur http://localhost:8501. Les parametres se saisissent dans la
barre laterale, les resultats s'affichent en temps reel (onglets Stockage, Tiers,
CPU/RAM, Architecture, Formules, Export).

### Tests

```bash
pytest -v
```

### Demo CLI rapide (sans UI)

```bash
python3 sizing_engine.py
```

## Valeurs d'exemple realistes (pre-remplies dans l'UI)

| Parametre | Valeur |
|---|---|
| Volume | 1 To/jour |
| Retention | 365 j (hot/warm 30 j) |
| RF | origin:2, total:3 |
| SF | origin:1, total:2 |
| Sites | 2 |
| Indexers | 4 |
| Search heads | 3 |
| DM acceleres | 5 |
| Recherches concurrentes | 12 |

## Modele de calcul (resume)

```
rawdata_cluster/jour = V x compression_rawdata x RF.total
tsidx_cluster/jour   = V x compression_tsidx   x SF.total
total sur retention  = (rawdata/jour + tsidx/jour) x retention
surcout DM accel     = V x facteur_DM x nb_DM x retention_accel x SF.total
capacite brute       = (donnees + DM) / utilisation_cible
par indexer          = capacite_brute_cluster / nb_indexers
```

**Multisite :** le stockage depend de `RF.total` / `SF.total` (copies totales du
cluster). L'attribut `origin` sert au placement par site et aux warnings de tolerance
de panne (regle N+1 par site).

## Sources des valeurs de reference (verifiees juin 2026)

- **OFFICIEL** : compression 15% rawdata / 35% tsidx, reference hardware (indexer
  12 coeurs/24 vCPU/32 Go, search head 16 coeurs/32 vCPU/32 Go), plancher 800 IOPS,
  ES Performance Reference (indexers 16 coeurs/32 Go), 1 recherche active ~ 1 coeur,
  SHC minimum 3 membres. (Splunk Capacity Manual, Reference Hardware, SVA, ES docs.)
- **TERRAIN** : 75-300 Go/jour/indexer (< 100 avec ES), ~4 coeurs pour 150 Go/jour
  d'indexation, IOPS confortable hot/warm >= 1200, facteur surcout tsidx des data
  models acceleres (estimation a calibrer — aucun ratio officiel universel).

Chaque valeur est annotee dans `references.py` et affichee dans l'onglet **Formules**
de l'UI. Le facteur DM accel notamment est une **estimation** clairement signalee :
calibrez-le sur vos propres summaries (`| datamodel ... | tstats` + taille HPAS reelle).

## Limites assumees

- Repartition equilibree des indexers entre sites (un desequilibre reel se gere a la main).
- Le surcout tsidx des data models acceleres n'a pas de ratio officiel : le facteur par
  defaut est indicatif.
- Les specs des composants de management (CM, deployer, MC, DS) sont des points de depart
  TERRAIN ; Splunk recommande de partir de la spec single-instance puis d'ajuster a l'echelle.

## Roadmap

- [x] Splunk Enterprise (mono-site & multisite, RF/SF origin·total)
- [ ] Support d'autres SIEM (architecture moteur generique a venir)
- [ ] Profils de donnees par source (compression reelle par sourcetype)
- [ ] Estimation de licence / cout
- [ ] Export Excel

## Contribuer

Les contributions sont bienvenues. Ouvrez une issue pour discuter d'un ajout
(nouveau SIEM, formule, reference) avant une pull request. Le moteur de calcul
(`sizing_engine.py`) est decouple de l'UI : toute nouvelle logique de sizing doit
y rester testable independamment (voir `test_sizing_engine.py`).

## Licence

Distribue sous licence **Apache-2.0**. Voir [LICENSE](LICENSE) et [NOTICE](NOTICE).
