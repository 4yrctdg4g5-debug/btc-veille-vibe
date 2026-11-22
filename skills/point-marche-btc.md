---
name: point-marche-btc
description: "Charge cette skill quand Simon demande un point marché BTC ad hoc en conversation (ex « point marché », « où en est le BTC », vérifier un mouvement pendant un trade, entre deux briefs du matin). Elle produit un point intraday falsifiable dans le chat, jamais un brief et jamais un canvas, en lisant en priorité le snapshot du collecteur continu (repo Git btc-veille-vibe), sinon une collecte ad hoc."
---

# Point marché BTC (intraday, à la demande)

## Rôle

Répondre entre deux briefs quotidiens (unique brief programmé : 9h00), à la demande,
pendant que Simon suit un trade ou une mèche de prix. C'est un POINT, pas un brief :
court, chiffré, falsifiable, dans la conversation.

## Sources, dans l'ordre

1. **Snapshot du collecteur continu** : `data/market/market.json` du repo Git
   `btc-veille-vibe` (URL raw complète dans KNOWLEDGE.md de « veille-btc »,
   section « Collecteur continu »). Lecture via un petit script Python (urllib)
   dans le bac à sable d'exécution, ou via les outils web. Si le snapshot est
   frais (moins de 30 min) : c'est la base chiffrée du point, inutile de
   recollecter. S'il a plus de 6 h : signaler « ⚠️ collecteur HS depuis
   (durée), à relancer sur le Mac » et passer à la collecte ad hoc.
2. **Contexte éditorial** : le dernier brief archivé de « veille-btc »
   (historique/), biais-hebdo.md et biais-mensuel.md : les niveaux, scénarios,
   conditions d'activation et invalidations en vigueur sont LA référence ; le
   point les cite, il ne les réinvente pas.
3. **Collecte ad hoc** (si le snapshot est périmé, ou pour compléter dans la
   fenêtre 0-30 min) : la collecte de la skill « veille-btc-pipeline », ou à
   défaut des requêtes ciblées Binance/Hyperliquid (ticker, OI, funding,
   klines 1h récentes). Timeout ≥ 240 s si collecte complète.
4. **News** : uniquement si la question porte dessus ou si un mouvement
   violent n'a pas d'explication dans les données ; recherche web, grammaire
   du filtre éditorial de KNOWLEDGE.md. Sinon, pas de news (le brief de 9h les
   a couvertes).

## Format de réponse (dans le chat)

- Ligne de contexte : « Point du (jour) (hh:mm) Paris · données : snapshot
  (hh:mm) / collecte ad hoc / dégradé ».
- ⚡ 3-5 lignes : prix + variation 24 h/7 j, RVOL horaire en cours, ΔOI (1 h /
  4 h / 24 h), funding (Binance 8 h + Hyperliquid horaire), L/S, position du
  prix par rapport aux niveaux du biais en vigueur (distance à l'invalidation
  et à la bascule/cible la plus proche).
- Verdict : feu 🟢🟠🔴 + direction écrite en toutes lettres, rapporté au biais
  du jour et au biais hebdo : EN LIGNE / EN ÉCART, en une ligne chacun si
  pertinent.
- Ligne `check :` une seule condition chiffrée et datée qui, si elle se
  réalise, valide ou invalide la lecture du point (ex : « check : clôture 1h
  au-dessus de 61 200 dans les 2 prochaines heures → bascule confirmée »).
  Falsifiable et vérifiable sur les données, pas une opinion.
- Si la collecte le montre et que c'est pertinent : une ligne liquidation /
  wick inhabituel, ou OI qui saute.
- Clôture systématique : « Information, pas un conseil financier. »

## Règles

- Aucun chiffre improvisé : tout vient du snapshot ou d'une collecte du run ;
  sinon écrire « indisponible » et dégrader sans inventer.
- Le feu n'est jamais le seul porteur du sens : direction en toutes lettres.
- Jamais de conseil d'achat/vente, jamais de taille de position (ce rôle est
  celui de la skill « topo-position-btc » si Simon ouvre le sujet de position).
- Le « point » doit pouvoir être faux : la ligne `check` est obligatoire.
- Interactif : si Simon enchaine (creuse un volet, demande un scénario, un
  niveau, vérifie une news), rester dans ce format court sauf demande
  explicite d'une analyse longue.
- **Enregistrement sur demande explicite only** (« enregistre-le »,
  « garde ce point ») : append le point daté avec sa ligne `check` dans
  « veille-btc » rubrique `points/AAAA-MM.md` (créer le fichier du mois
  s'il n'existe pas) pour qu'il soit scoré par le track record mensuel.
  Sans demande explicite, n'écrire nulle part.

## Dégradation

Collecteur mort et collecte ad hoc bloquée (APIs HS) : dire « point marché
indisponible ce run : (raison) » et ne rien improviser ni de prix ni de
niveaux.
