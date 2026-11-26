# btc-veille-vibe

Routine de veille marché Bitcoin, version « mise à niveau » :
**un seul brief par jour (9h00)** au lieu de trois, une **skill interactive de
point marché** dans le chat, et une **collecte de données continue sur le Mac**
(versionnée Git) qui remplace la capitalisation par les seuls briefs.

Ce dossier est autonome et partageable. Il contient le côté « Mac » de la
routine (collecteur + installation) et la documentation des prompts/skills du
côté assistant (Vibe ou autre).

## Architecture

```
        ┌─────────────────── Mac (ce repo) ───────────────────┐
        │  launchd → toutes les 10 min                        │
        │    collector/market.py                               │
        │      ├─ Binance USDT-M (prix, klines, OI, funding,   │
        │      │   premium, L/S)                               │
        │      ├─ Hyperliquid (mark, funding horaire, OI)      │
        │      ├─ alternative.me (F&G), CoinGecko (dominance) │
        │      ├─ mempool.space (difficulté, hashrate)         │
        │      └─ écrit data/market/… (snapshot + séries)     │
        │  git commit + push à chaque run                     │
        └───────────────────────┬─────────────────────────────┘
                                │ repo Git (public ou privé)
        ┌─────────────────── Assistant (Vibe) ───────────────┐
        │  Brief 9h00 (tâche planifiée, autonome : sa collec- │
        │  te propre tourne côté cloud ; le repo est une      │
        │  source de recoupement et de continuité)            │
        │  Skill « point-marche-btc » : lit le snapshot frais │
        │  du repo (raw.githubusercontent.com), répond dans   │
        │  le chat, ligne de vérification falsifiable        │
        │  Track record mensuel (dernier jour du mois,       │
        │  intégré au brief 9h)                               │
        └─────────────────────────────────────────────────────┘
```

Découpage des rôles, volontaire :

- **Le Mac accumule des séries brutes** (10 min) : c'est le but premier.
  Trois briefs par jour ne suffisaient pas à capitaliser des données ;
  144 snapshots/jour, si. Les séries horaires/journalières (`hourly.csv`,
  `daily.csv`, `state/*.json`) servent aux backtests et au track record.
- **Le brief de 9h00 reste auto-suffisant** : sa collecte tourne côté
  assistant (elle existait déjà, testée). Le repo est un recoupement, pas une
  dépendance : si le Mac est éteint, le brief paraît quand même.
- **La skill point-marche lit le repo en priorité** : réponse rapide et
  fraîche (moins de 30 min) sans relancer tout le pipeline ; sinon elle
  collecte ad hoc.
- **Le collecteur ne fait aucune analyse éditoriale** : pas de niveaux, pas de
  biais. Niveaux (POC/LVN, pivots, swings, Fib), leverage map et jugements
  restent produits par le brief côté assistant. Les données personnelles
  (paliers DCA) vivent dans la connaissance de l'assistant, **jamais dans ce
  repo**.

## Contenu

```
btc-veille-vibe/
├── README.md                      ← ce fichier
├── install.sh                     ← installation macOS (launchd + Git)
├── .gitignore
├── collector/market.py            ← le collecteur continu (stdlib only)
├── launchd/com.btcveille.vibe.market.plist.template
├── docs/TACHE_BRIEF_MATIN.md      ← prompt de la tâche planifiée unique
├── docs/POINT_MARCHE.md           ← usage de la skill point-marche-btc
└── skills/point-marche-btc.md    ← texte de la skill (pour réimplémenter)
```

Données produites dans `data/market/` :

| Fichier | Contenu | En Git ? |
|---|---|---|
| `market.json` | snapshot enrichi du dernier run, lisible par la skill point-marche | oui |
| `snapshots.csv` | une ligne par run (10 min) : prix, OI, funding, L/S, F&G, dominance | oui |
| `hourly.csv` | une ligne par bougie horaire clôturée (jour de Paris) : OHLC, volume, delta taker, RVOL horaire, OI en fin de bougie | oui |
| `daily.csv` | une ligne par jour écoulé (jour de Paris) : OHLC, RVOL jour, CVD jour, OI, funding moyen, F&G, dominance, hashrate | oui |
| `state/*.json` | séries accumulées irremplaçables (OI > 30 j, L/S, F&G, financement, dominance) | oui |
| `cache/*.json` | caches refetchables (klines, mining) | non |
| `market.log`, `.lock` | log et verrou | non |

## Installation (macOS)

Prérequis : `python3` (système, pas de venv nécessaire), `git`.

1. Créer un repo Git qui recevra les données (GitHub public ou privé ; les
   données sont purement du marché, aucune donnée personnelle n'y est
   poussée).
2. Dans le Terminal, depuis ce dossier :

```bash
./install.sh git@github.com:VOTRE_USER/btc-veille-vibe.git   # ou l'URL https
```

`install.sh` fait : `git init` + remote → installe le plist launchd
(10 min, au chargement puis toutes les 600 s) → premier run complet
(backfill des klines) → premier commit/push.

3. Vérifier après quelques minutes :

```bash
tail -2 data/market/market.log
jq '.meta.snapshot_at, .price.binance' data/market/market.json
```

4. Donner l'URL raw du snapshot à l'assistant (pour la skill point-marche) :
   `https://raw.githubusercontent.com/VOTRE_USER/btc-veille-vibe/main/data/market/market.json`

Pour retirer le collecteur : `launchctl unload
~/Library/LaunchAgents/com.btcveille.vibe.market.plist && rm
~/Library/LaunchAgents/com.btcveille.vibe.market.plist`.

## La routine côté assistant (résumé)

- **Un brief par jour à 9h00** (heure de Paris), dans la conversation.
  - Samedi : + récap de la semaine, bilan du biais hebdo, nouveau biais
    de la semaine suivante (il réécrit le fichier de biais).
  - Dernier jour du mois : + revue mensuelle du track record (biais jour,
    biais hebdo, analogues de cycle) et biais du mois suivant.
  - Les anciens briefs 14h00 (pré-open US) et vendredi 22h30 (hebdo) sont
    supprimés : un seul appel par jour, les volets fusionnent.
- **Point marché à la demande** : dans le chat (« point marché », « où en
  est le BTC »), la skill lit le snapshot Git (moins de 30 min) et répond
  en quelques lignes avec une condition de vérification chiffrée, pour
  arbitrer pendant un trade. Enregistrement sur demande explicite, pour
  alimentation du track record.
- Le prompt complet de la tâche : `docs/TACHE_BRIEF_MATIN.md` ; le texte de
  la skill : `skills/point-marche-btc.md`.

## Conventions des données (`market.json`)

Le champ `meta.conventions` documente chaque métrique en ligne (RVOL horaire
= bougie / moyenne des 20 dernières même heures de Paris, delta = 2×taker
achat − volume, funding Binance par 8 h vs Hyperliquid horaire, OI en BTC,
timezone Europe/Paris…). Un consommateur (humain ou IA) doit pouvoir lire le
snapshot sans documentation externe.

## Ce que ce projet n'est pas

- Pas de conseil financier : information et outillage de décision.
- Aucune clé d'API : uniquement des endpoints publics gratuits.
- Pas de backtest intégré pour l'instant : les séries sont la matière
  première (une brique backtest est le prolongement naturel).
