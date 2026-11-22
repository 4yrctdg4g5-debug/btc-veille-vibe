#!/bin/bash
# Installation du collecteur btc-veille-vibe sur macOS.
# Usage : ./install.sh [GIT_REMOTE]
#   GIT_REMOTE : URL du repo GitHub qui recevra les données
#               ex : https://github.com/4yrctdg4g5-debug/btc-veille-vibe.git
#               Si omis et aucun remote existant : le push Git est sauté silencieusement.

set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "ERREUR : python3 introuvable. Installez les Command Line Tools Xcode : xcode-select --install" >&2
  exit 1
fi

echo "== btc-veille-vibe : installation =="
echo "Dossier   : $DIR"
echo "Python    : $PYTHON"

# 1) Git : init (si besoin) + remote
if [ ! -d "$DIR/.git" ]; then
  git -C "$DIR" init -q
  echo "Git       : repo initialisé"
else
  echo "Git       : repo déjà initialisé"
fi
git -C "$DIR" branch -M main
if [ -n "${1:-}" ]; then
  if git -C "$DIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$DIR" remote set-url origin "$1"
  else
    git -C "$DIR" remote add origin "$1"
  fi
  echo "Remote    : $1"
fi
if git -C "$DIR" remote get-url origin >/dev/null 2>&1; then
  HAS_REMOTE=1
else
  HAS_REMOTE=0
  echo "Remote    : (aucun, le push sera silencieusement sauté)"
fi
if [ "$HAS_REMOTE" = "1" ]; then
  # aligne l'historique local sur le distant (repo vide ou avec README) sans toucher aux fichiers
  git -C "$DIR" fetch origin
  if git -C "$DIR" show-ref --verify --quiet refs/remotes/origin/main; then
    git -C "$DIR" reset --soft origin/main
    echo "Git       : historique aligné sur origin/main"
  fi
fi

# 2) commit initial : tout le projet (README, collecteur, docs, skill)
#    .gitignore exclut logs et caches ; data/ sera poussé par le collecteur
git -C "$DIR" add -A
git -C "$DIR" commit -q -m "Projet btc-veille-vibe : collecteur continu + docs + skill" 2>/dev/null || echo "Git       : rien à committer (déjà en place)"
if [ "$HAS_REMOTE" = "1" ]; then
  if ! git -C "$DIR" push -q -u origin main 2>/tmp/btc-veille-push.err; then
    echo "ERREUR : push GitHub refusé." >&2
    echo "GitHub n'accepte plus le mot de passe du compte pour git en HTTPS." >&2
    echo "Deux solutions (une seule fois) :" >&2
    echo "  a) GitHub CLI : brew install gh && gh auth login" >&2
    echo "  b) PAT : sur GitHub > Settings > Developer settings > Personal access tokens (fine-grained)," >&2
    echo "     créer un token limité à CE repo (Contents: Read and write), puis :" >&2
    echo "     git remote set-url origin https://VOTRE_USER:LE_TOKEN@github.com/4yrctdg4g5-debug/btc-veille-vibe.git" >&2
    echo "     (le Mac le gardera dans le trousseau après la première saisie : git config --global credential.helper osxkeychain)" >&2
    cat /tmp/btc-veille-push.err >&2
    exit 1
  fi
  echo "Git       : projet poussé sur GitHub"
fi

# 3) launchd
PLIST_SRC="$DIR/launchd/com.btcveille.vibe.market.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/com.btcveille.vibe.market.plist"
mkdir -p "$(dirname "$PLIST_DST")"
sed -e "s|__ROOT__|$DIR|g" -e "s|__PYTHON__|$PYTHON|g" "$PLIST_SRC" > "$PLIST_DST"
if command -v launchctl >/dev/null 2>&1; then
  launchctl unload "$PLIST_DST" >/dev/null 2>&1 || true
  launchctl load "$PLIST_DST"
  echo "launchd   : com.btcveille.vibe.market chargé (toutes les 10 min)"
else
  echo "launchd   : launchctl absent (pas macOS), plist écrit dans $PLIST_DST"
fi

# 4) premier run complet (backfill) : il committe et pousse data/market
echo "== Premier run (backfill complet des klines, ~1 min) =="
"$PYTHON" "$DIR/collector/market.py" --init full

# 5) état
echo "== Installation terminée =="
echo "Vérification :"
echo "  tail -2 $DIR/data/market/market.log"
echo "  jq '.meta.snapshot_at, .price.binance' $DIR/data/market/market.json"
echo "Snapshot (skill point-marche) :"
echo "  https://raw.githubusercontent.com/4yrctdg4g5-debug/btc-veille-vibe/main/data/market/market.json"
