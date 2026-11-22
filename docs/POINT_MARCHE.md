# La skill point-marche-btc (usage)

Texte complet : `skills/point-marche-btc.md`.

## Ce que c'est

Un point marché intraday, à la demande, dans la conversation. Conçu pour
l'usage interactif : Simon suit un trade, un mouvement l'inquiète ou
l'intéresse, il demande « point marché » ou « où en est le BTC » et obtient en
quelques secondes un point chiffré avec une condition de vérification.

## Comment ça marche

1. La skill lit le snapshot `data/market/market.json` du repo (via l'URL raw
   GitHub). S'il a moins de 30 minutes, c'est la base du point.
2. Elle s'adosse au dernier brief archivé (biais en vigueur, niveaux,
   invalidations) : le point est CONFRONTÉ au brief, pas flottant.
3. Snapshot périmé : collecte ad hoc (Binance/Hyperliquid). Collecteur HS
   depuis plus de 6 h : la skill le signale (à relancer sur le Mac).

## Format

- 3-5 lignes chiffrées (prix, RVOL horaire, ΔOI, funding, L/S, distance aux
  niveaux du biais).
- Verdict feu + direction écrite, rapport EN LIGNE / EN ÉCART au biais.
- Ligne `check :` une condition chiffrée et datée (falsifiable).
- « Information, pas un conseil financier. »

## Enregistrement (track record)

Sur demande explicite only (« enregistre-le ») : le point et sa ligne `check`
sont archivés dans la rubrique `points/AAAA-MM.md` de la connaissance de
veille, puis scorés par la revue mensuelle (tenu / raté / non déclenché),
comme les biais. C'est ce qui transforme l'interactif en données
capitalisables, sans pollution : rien n'est écrit sans demande.
