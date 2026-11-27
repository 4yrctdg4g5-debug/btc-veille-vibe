#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collecteur continu BTC — projet btc-veille-vibe.

Rôle : accumuler des séries de marché brutes sur le Mac (toutes les 10 min
via launchd), écrire un snapshot `market.json` lisible depuis le cloud, et
pousser le tout sur un dépôt Git.

Philosophie : ce collecteur ne fait PAS d'analyse éditoriale. Il collecte,
agrège (horaire, journalier) et publie. Les niveaux (POC/LVN, pivots,
swings, Fib), la leverage map et les jugements restent produits par le
briefing Vibe côté cloud, à partir de ses propres collectes.

Sources (une panne d'une source ne doit jamais arrêter les autres) :
  - Binance USDT-M futures : ticker 24h, klines 1h (cache incrémental),
    open interest (hist 1h, accumulation au-delà de la fenêtre 30 j de
    l'API), taux de financement, premium index, ratios long/short.
  - Hyperliquid : mark price, funding horaire, open interest.
  - alternative.me : Fear & Greed (historique accumulé).
  - CoinGecko : dominance BTC.
  - mempool.space : difficulté, retarget estimé, hashrate (SMA 7j/30j).
  - Yahoo Finance (secours Stooq) : futures ES/NQ (ES=F, NQ=F) + panier
    actions US (top 10 du Nasdaq-100 + META, MSTR, SMH) pour le bloc "us"
    du market.json (contexte d'ouverture US) ; valeurs brutes, jamais
    d'analyse ici.

Sorties (data/market/) :
  - market.json       snapshot enrichi du run (publié, lu par point-marche)
  - snapshots.csv     une ligne par run (10 min), append-only
  - hourly.csv        une ligne par bougie horaire clôturée (jour de Paris)
  - daily.csv         une ligne par jour écoulé (jour de Paris)
  - state/*.json      séries accumulées irremplaçables (OI > 30 j, L/S, F&G,
                      financement, dominance) — reconstituables en partie
                      depuis snapshots.csv, mais versionnées Git telles quelles
  - cache/            caches trivialement refetchables (klines, mining) — gitignorés
  - market.log        log du run (gitignoré)

Utilisation :
  python3 market.py                run normal (avec push Git si remote configuré)
  python3 market.py --no-git       run sans tentative de push
  python3 market.py --init full    premier run : backfill complet des klines

Aucune donnée personnelle (config.json, paliers DCA) n'est lue ni publiée
par ce script : market.json ne contient que du marché.
"""

import csv
import datetime as dt
import fcntl
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

# ---------------------------------------------------------------------------
# Configuration locale
# ---------------------------------------------------------------------------

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
UTC = dt.timezone.utc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "market")
STATES = os.path.join(DATA, "state")
CACHE = os.path.join(DATA, "cache")
LOG_PATH = os.path.join(DATA, "market.log")
LOCK_PATH = os.path.join(DATA, ".lock")

MARKET_JSON = os.path.join(DATA, "market.json")
SNAP_CSV = os.path.join(DATA, "snapshots.csv")
HOURLY_CSV = os.path.join(DATA, "hourly.csv")
DAILY_CSV = os.path.join(DATA, "daily.csv")

SYMBOL = "BTCUSDT"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) btc-veille-vibe/1.0"
TIMEOUT = 25

KLINE_KEEP_DAYS = 420        # fenêtre glissante de bougies horaires (~10 000)
OI_KEEP_DAYS = 400
FG_KEEP_DAYS = 400
DOM_KEEP_DAYS = 400
FUND_KEEP_DAYS = 400
LS_KEEP_DAYS = 90
RVOL_SAME_HOUR_N = 20        # RVOL horaire = vs moyenne des 20 mêmes heures
RVOL_DAY_N = 20              # RVOL journalier = vs moyenne des 20 jours préc.
MINING_TTL = 6 * 3600
DOM_TTL = 60 * 60
FG_TTL = 60 * 60

# Actions/futures US : contexte d'ouverture (biais, jamais une prédiction).
# Panier fixé le 23/11/2026 depuis le dépôt SEC T2-2026 du fonds de suivi du
# Nasdaq-100 (roulé aux prix du 08/09/2026) : top 10 NDX (~48 % de l'indice)
# + META + MSTR (proxy BTC) + SMH (complexe semi-conducteurs). À réviser à
# chaque publication trimestrielle des poids. Ajouter un symbole ici suffit :
# le snapshot publie automatiquement toutes les clés de ce dictionnaire.
US_SYMBOLS = {  # Yahoo Finance (chart API, sans clé)
    "es_futures": "ES=F",   # futur S&P 500 : agrégat, direction d'ouverture
    "nq_futures": "NQ=F",   # futur Nasdaq-100 : agrégat, direction d'ouverture
    "nvda": "NVDA",         # ~8-9 % du NDX, plus grosse weight
    "aapl": "AAPL",
    "googl": "GOOGL",
    "mu": "MU",
    "msft": "MSFT",
    "amd": "AMD",
    "amzn": "AMZN",
    "tsla": "TSLA",
    "intc": "INTC",
    "avgo": "AVGO",
    "meta": "META",
    "mstr": "MSTR",         # proxy BTC coté
    "smh": "SMH",           # ETF semi-conducteurs : secteur
}
US_STOOQ = {  # secours Stooq (clôtures journalières seulement)
    "es_futures": "es.f",
    "nq_futures": "nq.f",
    "nvda": "nvda.us",
    "aapl": "aapl.us",
    "googl": "googl.us",
    "mu": "mu.us",
    "msft": "msft.us",
    "amd": "amd.us",
    "amzn": "amzn.us",
    "tsla": "tsla.us",
    "intc": "intc.us",
    "avgo": "avgo.us",
    "meta": "meta.us",
    "mstr": "mstr.us",
    "smh": "smh.us",
}

US_LIVE_MAX_AGE_MIN = 20     # au-delà, une cotation Yahoo n'est plus "live"

UA_HEADERS = {"User-Agent": UA, "Accept": "application/json"}

os.makedirs(DATA, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("market")

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def now_paris() -> dt.datetime:
    return dt.datetime.now(PARIS)


def iso(d: dt.datetime) -> str:
    return d.isoformat(timespec="seconds")


def ms_to_paris(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=PARIS)


def http_json(url: str, payload=None, params: str = "") -> dict:
    """GET (ou POST) JSON. Échoue proprement (exception) sur HTTP/parse error."""
    if params:
        url = f"{url}?{params}"
    if payload is None:
        req = urllib.request.Request(url, headers=UA_HEADERS)
    else:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=body, headers={**UA_HEADERS, "Content-Type": "application/json"}
        )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: str, obj) -> None:
    """Écriture atomique (tmp + rename)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_csv(path: str, header: list, row: list) -> None:
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(header)
        w.writerow(row)


def csv_tail(path: str, n=1):
    """Dernières lignes d'un CSV (sans l'en-tête)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[1:][-n:]


def pct(a, b):
    """Variation en % de b vers a (None si b invalide)."""
    try:
        if b is None or b == 0 or a is None:
            return None
        return round((a - b) * 100.0 / b, 3)
    except TypeError:
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r6(x, nd=6):
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def fetch_ticker() -> dict:
    d = http_json("https://fapi.binance.com/fapi/v1/ticker/24hr", params=f"symbol={SYMBOL}")
    return {
        "price": float(d["lastPrice"]),
        "chg_24h_pct": float(d["priceChangePercent"]),
        "hi_24h": float(d["highPrice"]),
        "lo_24h": float(d["lowPrice"]),
        "vol_btc_24h": float(d["volume"]),
        "vol_usdt_24h": float(d["quoteVolume"]),
    }


def fetch_klines_hourly(full_init: bool) -> list:
    """Klines 1h incrémentales.

    Cache local : on retélécharge à chaque run les 2 dernières bougies
    connues (la dernière a été enregistrée en cours d'heure, donc tronquée)
    puis les nouvelles ; le cache est écrasé par heure d'ouverture.
    """
    cache_path = os.path.join(CACHE, "klines_1h.json")
    cached = read_json(cache_path, [])
    candles = [c for c in cached if isinstance(c, list) and len(c) >= 12]
    start_ms = None
    if candles and not full_init:
        start_ms = int(candles[-min(2, len(candles))][0])
    else:
        # fenêtre initiale : KLINE_KEEP_DAYS
        start_ms = int((time.time() - KLINE_KEEP_DAYS * 86400) * 1000)
        candles = []
    url = "https://fapi.binance.com/fapi/v1/klines"
    while True:
        d = http_json(url, params=f"symbol={SYMBOL}&interval=1h&limit=1000&startTime={start_ms}")
        if not d:
            break
        candles.extend(d)  # après les anciennes : la version fraîche gagne
        if len(d) < 1000:
            break
        start_ms = int(d[-1][0]) + 1
        time.sleep(0.15)
    # dédoublonne (dernière version gagne) + coupe sur la fenêtre glissante
    seen = {}
    for c in candles:
        seen[int(c[0])] = c
    keys = sorted(seen)
    cut = int((time.time() - KLINE_KEEP_DAYS * 86400) * 1000)
    keys = [k for k in keys if k >= cut]
    # garde la bougie en cours (dernière), même non clôturée
    out = [seen[k] for k in keys]
    write_json(cache_path, out)
    return out


def fetch_oi_hist() -> dict:
    """Open interest hist 1h (fenêtre API ~30 j) fusionné dans l'état persistant."""
    state_path = os.path.join(STATES, "oi_hist.json")
    state = read_json(state_path, {})
    d = http_json(
        "https://fapi.binance.com/futures/data/openInterestHist",
        params=f"symbol={SYMBOL}&period=1h&limit=500",
    )
    for item in d:
        t = int(item["timestamp"])
        state[str(t)] = {
            "oi_btc": float(item["sumOpenInterest"]),
            "oi_usd": float(item["sumOpenInterestValue"]),
        }
    cut_ms = int((time.time() - OI_KEEP_DAYS * 86400) * 1000)
    state = {k: v for k, v in state.items() if int(k) >= cut_ms}
    write_json(state_path, state)
    return state


def fetch_funding() -> list:
    """Événements de financement (historique accumulé)."""
    state_path = os.path.join(STATES, "funding_hist.json")
    hist = read_json(state_path, [])
    d = http_json(
        "https://fapi.binance.com/fapi/v1/fundingRate", params=f"symbol={SYMBOL}&limit=90"
    )
    # état persisté au format {"t","rate"} + réponse API au format brut
    by_key = {int(e["t"]): float(e["rate"]) for e in hist}
    for e in d:
        by_key[int(e["fundingTime"])] = float(e["fundingRate"])
    keys = sorted(by_key)[-FUND_KEEP_DAYS * 3 * 4 :]
    out = [{"t": k, "rate": by_key[k]} for k in keys]
    write_json(state_path, out)
    return out


def fetch_premium() -> dict:
    d = http_json("https://fapi.binance.com/fapi/v1/premiumIndex", params=f"symbol={SYMBOL}")
    return {
        "mark": float(d["markPrice"]),
        "index": float(d["indexPrice"]),
        "rate": float(d["lastFundingRate"]),
    }


def fetch_ls_hist() -> dict:
    """Ratios long/short (3 endpoints, fenêtre API 30 j) accumulés."""
    state_path = os.path.join(STATES, "ls_hist.json")
    state = read_json(state_path, [])
    by_key = {int(x["t"]): x for x in state}
    endpoints = {
        "top_pos": "topLongShortPositionRatio",
        "top_acc": "topLongShortAccountRatio",
        "glob_acc": "globalLongShortAccountRatio",
    }
    for key, ep in endpoints.items():
        d = http_json(
            f"https://fapi.binance.com/futures/data/{ep}",
            params=f"symbol={SYMBOL}&period=1d&limit=30",
        )
        for item in d:
            t = int(item["timestamp"])
            e = by_key.setdefault(t, {"t": t})
            e[key] = r6(float(item["longShortRatio"]), 4)
    keys = sorted(by_key)
    cut_ms = int((time.time() - LS_KEEP_DAYS * 86400) * 1000)
    keys = [k for k in keys if k >= cut_ms]
    out = [by_key[k] for k in keys]
    write_json(state_path, out)
    return {x["t"]: x for x in out}


def fetch_hyperliquid() -> dict:
    d = http_json(
        "https://api.hyperliquid.xyz/info",
        payload={"type": "metaAndAssetCtxs"},
    )
    meta, ctxs = d[0], d[1]
    universe = meta["universe"]
    for i, u in enumerate(universe):
        if u.get("name") == "BTC":
            c = ctxs[i]
            return {
                "mark": float(c["markPx"]),
                "funding_hourly_pct": float(c["funding"]) * 100.0,
                "oi_sz": float(c["openInterest"]),
                "day_vol_sz": float(c["dayNtlVlm"]) / float(c["markPx"]),
                "day_ntl_usd": float(c["dayNtlVlm"]),
            }
    raise ValueError("BTC introuvable dans l'univers Hyperliquid")


def fetch_fg_hist() -> dict:
    """Fear & Greed alternative.me, historique journalier accumulé."""
    state_path = os.path.join(STATES, "fg_hist.json")
    state = read_json(state_path, [])
    by_key = {int(x["t"]): int(x["v"]) for x in state}
    cache_path = os.path.join(CACHE, "fg_fetch.json")
    last_fetch = read_json(cache_path, {"t": 0})
    if time.time() - last_fetch.get("t", 0) > FG_TTL or not by_key:
        d = http_json("https://api.alternative.me/fng/", params="limit=31&format=json")
        for item in d["data"]:
            t = int(item["timestamp"])
            by_key[t] = int(item["value"])
        write_json(cache_path, {"t": time.time()})
        keys = sorted(by_key)[-FG_KEEP_DAYS:]
        out = [{"t": k, "v": by_key[k]} for k in keys]
        write_json(state_path, out)
        return {x["t"]: x["v"] for x in out}
    return {x["t"]: x["v"] for x in state}


def fetch_dom_hist() -> dict:
    """Dominance BTC (CoinGecko), point horaire accumulé."""
    state_path = os.path.join(STATES, "dom_hist.json")
    state = read_json(state_path, [])
    by_key = {int(x["t"]): x["v"] for x in state}
    cache_path = os.path.join(CACHE, "dom_fetch.json")
    last_fetch = read_json(cache_path, {"t": 0})
    if time.time() - last_fetch.get("t", 0) > DOM_TTL or not by_key:
        dom = None
        for attempt in range(2):
            try:
                d = http_json("https://api.coingecko.com/api/v3/global")
                dom = float(d["data"]["market_cap_percentage"]["btc"])
                break
            except Exception:
                if attempt == 1:
                    raise
                time.sleep(3)
        by_key[int(time.time() * 1000)] = r6(dom, 3)
        write_json(cache_path, {"t": time.time()})
        cut_ms = int((time.time() - DOM_KEEP_DAYS * 86400) * 1000)
        keys = sorted(k for k in by_key if k >= cut_ms)
        out = [{"t": k, "v": by_key[k]} for k in keys]
        write_json(state_path, out)
        return {k: by_key[k] for k in keys}
    return {k: by_key[k] for k in by_key}


def fetch_mining() -> dict:
    """Difficulté, retarget estimé, hashrate + SMA 7j/30j (TTL 6 h)."""
    cache_path = os.path.join(CACHE, "mining.json")
    cached = read_json(cache_path, None)
    if cached and time.time() - cached.get("_fetched", 0) < MINING_TTL:
        return cached
    adj = http_json("https://mempool.space/api/v1/difficulty-adjustment")
    # difficulté courante via l'endpoint blocks tip
    tip = http_json("https://mempool.space/api/v1/blocks")
    hr = http_json("https://mempool.space/api/v1/mining/hashrate/3y")
    series = hr["hashrates"][-60:]  # ~60 derniers points (jour)
    vals = [p["avgHashrate"] for p in series]
    sma7 = mean(vals[-7:])
    sma30 = mean(vals[-30:])
    # mempool exprime ses hashrates en H/s -> 1 EH/s = 1e18 H/s
    out = {
        "_fetched": time.time(),
        "difficulty": int(tip[0]["difficulty"]),
        "height": int(tip[0]["height"]),
        "retarget_change_pct": r6(float(adj["difficultyChange"]), 3),
        "retarget_est_date": (
            iso(dt.datetime.fromtimestamp(int(adj["estimatedRetargetDate"]) / 1000, tz=PARIS))
            if adj.get("estimatedRetargetDate")
            else None
        ),
        "retarget_remaining_time_s": adj.get("remainingTime"),
        "retarget_remaining_blocks": adj.get("remainingBlocks"),
        "hashrate_ehs": r6(hr["currentHashrate"] / 1e18, 1),
        "hashrate_sma7_ehs": r6(sma7 / 1e18, 1) if sma7 else None,
        "hashrate_sma30_ehs": r6(sma30 / 1e18, 1) if sma30 else None,
        "series_tail_ehs": [
            {"t": int(p["timestamp"]), "ehs": r6(p["avgHashrate"] / 1e18, 1)} for p in series
        ],
    }
    write_json(cache_path, out)
    return out


def _yahoo_quote(sym: str) -> dict:
    """Un symbole via Yahoo chart API (sans clé) : cours live ou premarket."""
    last_err = None
    for host in ("query1", "query2"):
        try:
            encoded = urllib.parse.quote(sym, safe="")
            url = (
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{encoded}"
                "?interval=5m&range=1d"
            )
            req = urllib.request.Request(url, headers=UA_HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            meta = d["chart"]["result"][0]["meta"]
            last = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if last is None or not prev:
                raise ValueError(f"prix indisponible pour {sym}")
            quote_ms = meta.get("regularMarketTime")
            age_min = (time.time() - int(quote_ms)) / 60.0 if quote_ms else None
            live = age_min is not None and age_min < US_LIVE_MAX_AGE_MIN
            q = {
                "symbol": sym,
                "name": meta.get("shortName") or sym,
                "price": r6(float(last), 2),
                "prev_close": r6(float(prev), 2),
                "chg_pct": pct(float(last), float(prev)),
                "quote_time": (
                    iso(dt.datetime.fromtimestamp(int(quote_ms), tz=PARIS)) if quote_ms else None
                ),
                "live": live,  # cotation de moins de 20 min
                "source": "yahoo",
            }
            if not live:
                q["stale_reason"] = (
                    "quote_time absent" if age_min is None
                    else f"cotation vieille de {age_min:.0f} min (> {US_LIVE_MAX_AGE_MIN} min) : marché fermé ou hors séance"
                )
            return q
        except Exception as e:
            last_err = e
    raise RuntimeError(f"yahoo indisponible pour {sym} : {last_err}")


def _stooq_quote(sym: str) -> dict:
    """Secours Stooq : historique quotidien CSV, clôtures de séance seulement."""
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        lines = [ln for ln in resp.read().decode("utf-8").splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError(f"historique stooq vide pour {sym}")
    closes = []
    for ln in lines[1:]:  # Date,Open,High,Low,Close,Volume
        parts = ln.split(",")
        if len(parts) >= 5 and parts[4]:
            try:
                closes.append(float(parts[4]))
            except ValueError:
                continue
    if len(closes) < 2:
        raise ValueError(f"clotures insuffisantes pour {sym}")
    last, prev = closes[-1], closes[-2]
    return {
        "symbol": sym,
        "name": sym,
        "price": r6(last, 2),
        "prev_close": r6(prev, 2),
        "chg_pct": pct(last, prev),
        "quote_time": None,
        "live": False,  # clôture de séance, pas du live
        "stale_reason": "secours stooq : clôture de séance, pas de cotation live",
        "source": "stooq",
    }


def fetch_us_market() -> dict:
    """Bloc "us" du market.json : futures ES/NQ + panier top 10 NDX + META,
    MSTR, SMH (contexte d'ouverture US).

    Une panne d'un symbole ne bloque pas les autres ; si tous échouent,
    lève une exception pour que le run marque "us" indisponible sans casser
    le reste du snapshot. Valeurs brutes uniquement : le jugement (biais
    d'ouverture, lecture) reste produit côté briefing Vibe.
    """
    quotes = {}
    sources = []
    unavailable = []
    for key, sym in US_SYMBOLS.items():
        try:
            quotes[key] = _yahoo_quote(sym)
            sources.append("yahoo")
            continue
        except Exception as e:
            log.info("us: yahoo en echec pour %s (%s), essai stooq", sym, e)
        try:
            quotes[key] = _stooq_quote(US_STOOQ[key])
            sources.append("stooq")
        except Exception as e:
            log.info("us: stooq aussi en echec pour %s (%s)", sym, e)
            quotes[key] = None
            unavailable.append(key)
    if all(q is None for q in quotes.values()):
        raise RuntimeError("aucune source US disponible (yahoo + stooq)")
    if sources and all(s == "yahoo" for s in sources):
        src = "yahoo"
    elif sources and all(s == "stooq" for s in sources):
        src = "stooq"
    else:
        src = "mix"
    out = {"as_of": iso(now_paris()), "source": src}
    for key in US_SYMBOLS:
        out[key] = quotes.get(key)
    out["unavailable"] = unavailable
    return out


# ---------------------------------------------------------------------------
# Agrégations
# ---------------------------------------------------------------------------


def parse_candles(raw: list) -> dict:
    """klines Binance -> {open_ms: dict parsé}, + liste triée."""
    out = {}
    for k in raw:
        t = int(k[0])
        out[t] = {
            "open_ms": t,
            "close_ms": int(k[6]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol_btc": float(k[5]),
            "vol_usdt": float(k[7]),
            "taker_buy_btc": float(k[9]),
        }
    return out


def candle_delta(c: dict) -> float:
    """Delta taker net (achat agressif) en BTC pour la bougie."""
    return r6(2.0 * c["taker_buy_btc"] - c["vol_btc"], 3)


def same_hour_profile(candles_idx: dict, ts_key: int, exclude_ms: int) -> float:
    """Moyenne des volumes USDT des N dernières bougies à la même heure de Paris."""
    hour = ms_to_paris(ts_key).hour
    keys = sorted(candles_idx)
    matches = [k for k in keys if ms_to_paris(k).hour == hour and k < exclude_ms]
    return r6(mean([candles_idx[k]["vol_usdt"] for k in matches[-RVOL_SAME_HOUR_N:]]), 2)


def series_oi_at(oi_state: dict, ms: int):
    """OI (BTC) au plus proche instant <= ms."""
    keys = sorted(int(k) for k in oi_state)
    best = None
    for k in keys:
        if k <= ms:
            best = k
        else:
            break
    return oi_state[str(best)]["oi_btc"] if best is not None else None


def write_hourly_rows(candles: list, oi_state: dict) -> list:
    """Append les bougies horaires clôturées non encore écrites. Retourne rows."""
    header = [
        "open_paris", "open_unix", "high", "low", "open", "close",
        "vol_usdt", "taker_buy_btc", "delta_btc", "rvol_hour", "oi_end_btc",
    ]
    idx = parse_candles(candles)
    done = {r[1] for r in csv_tail(HOURLY_CSV, n=10**9)}
    now_ms = int(time.time() * 1000)
    keys = sorted(idx)
    rows = []
    for k in keys:
        c = idx[k]
        if str(k) in done or c["close_ms"] > now_ms:
            continue
        prof = same_hour_profile(idx, k, exclude_ms=k)
        rvol = pct(c["vol_usdt"], prof) if prof else None
        rows.append(
            {
                "open_paris": iso(ms_to_paris(k)),
                "open_unix": k,
                "high": c["high"],
                "low": c["low"],
                "open": c["open"],
                "close": c["close"],
                "vol_usdt": r6(c["vol_usdt"], 2),
                "taker_buy_btc": r6(c["taker_buy_btc"], 3),
                "delta_btc": candle_delta(c),
                "rvol_hour": rvol,
                "oi_end_btc": series_oi_at(oi_state, k + 3600_000),
            }
        )
    for r in rows:
        append_csv(HOURLY_CSV, header, [r[h] for h in header])
    return rows


def load_hourly_csv() -> dict:
    """hourly.csv -> {date_paris: [rows]} pour l'agrégat journalier."""
    days = {}
    if not os.path.exists(HOURLY_CSV):
        return days
    with open(HOURLY_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = row["open_paris"][:10]
            days.setdefault(d, []).append({k: _num(v) for k, v in row.items()})
    return days


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except ValueError:
        return v


def write_daily_rows(oi_state: dict, fund_hist: list, fg_hist: dict, dom_hist: dict, mining: dict) -> None:
    """Écrit la ligne du jour écoulé (Paris) si absente."""
    header = [
        "date", "open", "high", "low", "close", "vol_usdt", "rvol_day",
        "cvd_day_btc", "oi_end_btc", "oi_chg_pct", "fund_avg_pct",
        "fg_open", "fg_close", "dom_close", "hashrate_ehs",
    ]
    days = load_hourly_csv()
    done = {r[0] for r in csv_tail(DAILY_CSV, n=10**9)}
    today = now_paris().date()
    # comble au besoin les 3 derniers jours (ex. Mac éteint)
    for i in range(1, 4):
        d = today - dt.timedelta(days=i)
        dstr = str(d)
        if dstr in done or dstr not in days:
            continue
        rows = days[dstr]
        day_start_ms = int(
            dt.datetime.combine(d, dt.time.min, tzinfo=PARIS).timestamp() * 1000
        )
        day_end_ms = day_start_ms + 86400_000
        oi_end = None
        oi_start = None
        for r in rows:
            oi = r.get("oi_end_btc")
            if oi is not None:
                oi_end = oi
                if oi_start is None:
                    oi_start = oi
        if oi_start is None:
            oi_start = series_oi_at(oi_state, day_start_ms)
        prev_oi = series_oi_at(oi_state, day_start_ms - 3600_000)
        fund_day = [e["rate"] for e in fund_hist if day_start_ms <= e["t"] < day_end_ms]
        # F&G : clés alternative.me en secondes -> ms
        fg_day = sorted(k for k in fg_hist if day_start_ms <= k * 1000 < day_end_ms)
        fg_prevs = [k for k in sorted(fg_hist) if k * 1000 < day_start_ms]
        fg_open = fg_hist[fg_prevs[-1]] if fg_prevs else None
        fg_close = fg_hist[fg_day[-1]] if fg_day else fg_open
        dom_day = [k for k in sorted(dom_hist) if day_start_ms <= k < day_end_ms]
        dom_close = dom_hist[dom_day[-1]] if dom_day else None
        vols = [r["vol_usdt"] for r in days.get(dstr, [])]
        prev_days = [str(d - dt.timedelta(days=j)) for j in range(1, RVOL_DAY_N + 1)]
        prev_vols = [sum(r["vol_usdt"] for r in days.get(p, [])) for p in prev_days]
        prev_vols = [v for v in prev_vols if v]
        rvol_day = pct(sum(vols), mean(prev_vols)) if prev_vols else None
        hr_ehs = None
        for p in mining.get("series_tail_ehs", []):
            ts = dt.datetime.fromtimestamp(p["t"], tz=UTC).astimezone(PARIS)
            if ts.date() <= d:
                hr_ehs = p["ehs"]
        row = [
            dstr,
            r6(rows[0]["open"], 1),
            r6(max(r["high"] for r in rows), 1),
            r6(min(r["low"] for r in rows), 1),
            r6(rows[-1]["close"], 1),
            r6(sum(vols), 2),
            rvol_day,
            r6(sum(r["delta_btc"] for r in rows), 3),
            r6(oi_end, 1) if oi_end else None,
            pct(oi_end, prev_oi),
            r6(mean(fund_day) * 100.0, 5) if fund_day else None,
            fg_open,
            fg_close,
            dom_close,
            hr_ehs,
        ]
        append_csv(DAILY_CSV, header, row)
        log.info("daily.csv: ligne ajoutée pour %s", dstr)


# ---------------------------------------------------------------------------
# Snapshot market.json
# ---------------------------------------------------------------------------


def build_market_json(errors: list, ticker, candles, oi_state, fund_hist,
                       premium, ls_hist, hl, fg_hist, dom_hist, mining, us=None) -> dict:
    now_p = now_paris()
    now_ms = int(time.time() * 1000)
    idx = parse_candles(candles)
    keys = sorted(idx)

    # --- prix / variations
    last = idx[keys[-1]] if keys else {}
    closes_1h_7d = [idx[k]["close"] for k in keys[-24 * 7:]]
    chg_7d = pct(ticker["price"], closes_1h_7d[0]) if closes_1h_7d else None

    # --- volume : 24 dernières bougies horaires complétées + bougie en cours
    completed = [idx[k] for k in keys if idx[k]["close_ms"] <= now_ms]
    last24 = completed[-24:]
    cur_hour_key = keys[-1] if keys else None
    cur = idx.get(cur_hour_key)
    cur_prof = same_hour_profile(idx, cur_hour_key, exclude_ms=cur_hour_key) if cur else None
    elapsed_frac = 1.0
    if cur and cur["close_ms"] > now_ms:
        span = cur["close_ms"] - cur["open_ms"]
        elapsed_frac = min(1.0, (now_ms - cur["open_ms"]) / span)
    rvol_cur = pct(cur["vol_usdt"] / max(elapsed_frac, 0.1), cur_prof) if cur and cur_prof else None

    candles_24 = [
        {
            "t": iso(ms_to_paris(k)),
            "close": r6(idx[k]["close"], 1),
            "vol_usdt": r6(idx[k]["vol_usdt"], 0),
            "vol_ratio_btc": r6(idx[k]["vol_usdt"] / idx[k]["close"], 1),
            "delta_btc": candle_delta(idx[k]),
            "rvol_hour": pct(idx[k]["vol_usdt"], same_hour_profile(idx, k, exclude_ms=k)),
        }
        for k in keys[-24:]
        if idx[k]["close_ms"] <= now_ms
    ]

    # --- dérivés
    oi_keys = sorted(int(k) for k in oi_state)
    oi_now = oi_state[str(oi_keys[-1])] if oi_keys else {}
    oi_1h = oi_state[str(oi_keys[-2])]["oi_btc"] if len(oi_keys) > 1 else None
    oi_4h = oi_state[str(oi_keys[-5])]["oi_btc"] if len(oi_keys) > 5 else None
    oi_24h = oi_state[str(oi_keys[-25])]["oi_btc"] if len(oi_keys) > 25 else None
    fund_now = fund_hist[-1]["rate"] * 100.0 if fund_hist else None
    fund_prevs = fund_hist[-4:]
    ls_now = ls_hist[sorted(ls_hist)[-1]] if ls_hist else {}
    day_keys = sorted(ls_hist)
    ls_prev = ls_hist[day_keys[-2]] if len(day_keys) > 1 else {}

    # --- sentiment
    fg_keys = sorted(fg_hist)
    fg_now = fg_hist[fg_keys[-1]] if fg_keys else None
    fg_prev = fg_hist[fg_keys[-2]] if len(fg_keys) > 1 else None
    dom_keys = sorted(dom_hist)
    dom_now = dom_hist[dom_keys[-1]] if dom_keys else None

    return {
        "meta": {
            "snapshot_at": iso(now_p),
            "version": "vibe-market/1.1",
            "collector": "mac-launchd, 10 min",
            "errors": errors,
            "conventions": {
                "prix": "BTCUSDT (Binance USDT-M), USD arrondis",
                "rvol_hour": "écart en % du volume de la bougie horaire vs la moyenne des 20 dernières bougies à la même heure de Paris (0 = normal, +100 = double, -50 = moitié)",
                "rvol_hour_current": "proratisé sur la fraction écoulée de l'heure en cours",
                "delta_btc": "2 x taker buy base - volume : flux agressif net acheteur en BTC (positif = achats dominants)",
                "oi": "open interest Binance en BTC",
                "funding": "Binance = taux par période de 8 h en % ; Hyperliquid = taux horaire en % (annualisé = x 24 x 365)",
                "ls": "ratios long/short Binance : top_pos = positions des top traders, top_acc = comptes des top traders, glob_acc = tous les comptes",
                "fg": "Fear & Greed alternative.me (0-100)",
                "us": "futures ES/NQ (CME, Yahoo ES=F/NQ=F) et panier actions US (top 10 du Nasdaq-100 au dépôt T2-2026 + META, MSTR proxy BTC, SMH semi-conducteurs) : live=true si quote_time a moins de 20 min (yahoo), sinon live=false avec stale_reason (cotation ancienne, ou clôture de séance stooq) ; chg_pct = variation vs clôture de la session précédente ; le jugement (biais d'ouverture US, breadth du panier) est produit côté briefing, jamais par le collecteur",
                "timezone": "Europe/Paris",
            },
        },
        "price": {
            "binance": r6(ticker["price"], 1),
            "hyperliquid": r6(hl["mark"], 1),
            "hl_vs_binance_pct": pct(hl["mark"], ticker["price"]),
            "chg_24h_pct": r6(ticker["chg_24h_pct"], 2),
            "chg_7d_pct": chg_7d,
            "hi_24h": r6(ticker["hi_24h"], 1),
            "lo_24h": r6(ticker["lo_24h"], 1),
            "premium_index_pct": pct(premium["mark"], premium["index"]) if premium else None,
        },
        "volume": {
            "vol_usdt_24h": r6(ticker["vol_usdt_24h"], 0),
            "rvol_hour_current": rvol_cur,
            "vol_usdt_heure_en_cours": r6(cur["vol_usdt"], 0) if cur else None,
            "cvd_24h_btc": r6(sum(candle_delta(c) for c in last24), 3) if last24 else None,
            "candles_24h": candles_24,
        },
        "derivatives": {
            "oi_btc": r6(oi_now.get("oi_btc"), 1),
            "oi_usd": r6(oi_now.get("oi_usd"), 0),
            "oi_chg_pct": {"1h": pct(oi_now.get("oi_btc"), oi_1h), "4h": pct(oi_now.get("oi_btc"), oi_4h), "24h": pct(oi_now.get("oi_btc"), oi_24h)},
            "hyperliquid_oi_btc": r6(hl["oi_sz"], 0),
            "funding_binance_8h_pct": r6(fund_now, 5),
            "funding_binance_avg_last32h_pct": r6(mean([e["rate"] for e in fund_prevs]) * 100.0, 5) if fund_prevs else None,
            "funding_hyperliquid_hourly_pct": r6(hl["funding_hourly_pct"], 5),
            "ls_top_positions": ls_now.get("top_pos"),
            "ls_top_accounts": ls_now.get("top_acc"),
            "ls_global_accounts": ls_now.get("glob_acc"),
            "ls_top_positions_veille": ls_prev.get("top_pos"),
        },
        "sentiment": {
            "fear_greed": fg_now,
            "fear_greed_veille": fg_prev,
            "dominance_btc_pct": dom_now,
        },
        "mining": {
            "difficulty": mining.get("difficulty"),
            "hashrate_ehs": mining.get("hashrate_ehs"),
            "hashrate_sma7_ehs": mining.get("hashrate_sma7_ehs"),
            "hashrate_sma30_ehs": mining.get("hashrate_sma30_ehs"),
            "retarget_change_pct": mining.get("retarget_change_pct"),
            "retarget_est_date": mining.get("retarget_est_date"),
        }
        if mining
        else None,
        "us": us,
    }


# ---------------------------------------------------------------------------
# Push Git
# ---------------------------------------------------------------------------


def git_push(no_git: bool) -> None:
    if no_git or not os.path.isdir(os.path.join(ROOT, ".git")):
        return
    stamp = now_paris().strftime("%Y-%m-%d %H:%M")

    def run(args):
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)

    def git_err(p):
        return (p.stderr or p.stdout or "").strip().replace("\n", " | ")[:500]

    try:
        p = run(["git", "add", "-A", "data"])
        if p.returncode != 0:
            log.warning("git add en échec (code %s) : %s", p.returncode, git_err(p))
            return
        if run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            p = run(["git", "commit", "--quiet", "-m", f"collecte {stamp}"])
            if p.returncode != 0:
                log.warning("git commit en échec (code %s) : %s", p.returncode, git_err(p))
                return
        else:
            log.info("git : rien de nouveau à committer")
        p = run(["git", "pull", "--rebase", "--autostash", "--quiet"])
        if p.returncode != 0:
            log.warning("git pull --rebase en échec (code %s) : %s", p.returncode, git_err(p))
            run(["git", "rebase", "--abort"])  # laisse le dépôt propre pour le run suivant
            return
        p = run(["git", "push", "--quiet"])
        if p.returncode != 0:
            log.warning("git push en échec (code %s) : %s", p.returncode, git_err(p))
            return
    except Exception as e:  # git absent, timeout...
        log.warning("git en échec : %s", e)
        return
    log.info("git push ok")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    os.makedirs(STATES, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)

    # lock : jamais deux runs en parallèle (launchd)
    lock_f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("un run est déjà en cours, abandon")
        return 0

    full_init = "--init" in sys.argv and "full" in sys.argv
    no_git = "--no-git" in sys.argv
    t0 = time.time()
    errors = []

    def guarded(name, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            log.warning("source %s en échec : %s", name, e)
            errors.append({"source": name, "error": str(e)[:200]})
            return None

    ticker = guarded("binance_ticker", fetch_ticker)
    if ticker is None:
        log.error("ticker Binance indisponible, run abandonné (réessai au prochain tick)")
        return 1
    candles = guarded("binance_klines", fetch_klines_hourly, full_init)
    candle_idx = parse_candles(candles or [])
    oi_state = guarded("binance_oi", fetch_oi_hist) or read_json(
        os.path.join(STATES, "oi_hist.json"), {}
    )
    fund_hist = guarded("binance_funding", fetch_funding) or read_json(
        os.path.join(STATES, "funding_hist.json"), []
    )
    premium = guarded("binance_premium", fetch_premium)
    ls_hist = guarded("binance_ls", fetch_ls_hist) or {}
    hl = guarded("hyperliquid", fetch_hyperliquid) or {}
    fg_hist = guarded("alternative_fg", fetch_fg_hist) or {}
    dom_hist = guarded("coingecko_dom", fetch_dom_hist) or {}
    mining = guarded("mempool_mining", fetch_mining) or {}
    us = guarded("us_market", fetch_us_market) or None

    if candle_idx:
        write_hourly_rows(candles or [], oi_state)
    write_daily_rows(oi_state, fund_hist, fg_hist, dom_hist, mining)

    # ligne snapshot + market.json
    last_oi = None
    oi_keys = sorted(int(k) for k in oi_state)
    if oi_keys:
        last_oi = oi_state[str(oi_keys[-1])]
    ls_keys = sorted(ls_hist)
    ls_now = ls_hist[ls_keys[-1]] if ls_keys else {}
    fg_keys = sorted(fg_hist)
    dom_keys = sorted(dom_hist)
    append_csv(
        SNAP_CSV,
        ["ts", "price", "hl", "oi_btc", "fund_8h_pct", "fund_hl_pct",
         "ls_top_pos", "ls_glob_acc", "fg", "dom"],
        [
            iso(now_paris()), r6(ticker["price"], 1), r6(hl.get("mark"), 1),
            r6(last_oi.get("oi_btc"), 1) if last_oi else None,
            r6(fund_hist[-1]["rate"] * 100.0, 5) if fund_hist else None,
            r6(hl.get("funding_hourly_pct"), 5),
            ls_now.get("top_pos"), ls_now.get("glob_acc"),
            fg_hist[fg_keys[-1]] if fg_keys else None,
            dom_hist[dom_keys[-1]] if dom_keys else None,
        ],
    )

    snapshot = build_market_json(
        errors, ticker, candles or [], oi_state, fund_hist, premium,
        ls_hist, hl, fg_hist, dom_hist, mining, us,
    )
    write_json(MARKET_JSON, snapshot)
    state_txt = "OK" if not errors else f"{len(errors)} erreur(s)"
    log.info(
        "run fini en %.1fs — %s — prix %s",
        time.time() - t0, state_txt, ticker["price"],
    )
    git_push(no_git)
    return 0


if __name__ == "__main__":
    sys.exit(main())
