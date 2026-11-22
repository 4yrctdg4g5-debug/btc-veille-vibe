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
    """Klines 1h incrémentales. Cache local : on ne refetch que les nouvelles."""
    cache_path = os.path.join(CACHE, "klines_1h.json")
    cached = read_json(cache_path, [])
    candles = [c for c in cached if isinstance(c, list) and len(c) >= 12]
    start_ms = None
    if candles and not full_init:
        start_ms = int(candles[-1][0]) + 1
    else:
        # fenêtre initiale : KLINE_KEEP_DAYS
        start_ms = int((time.time() - KLINE_KEEP_DAYS * 86400) * 1000)
        candles = []
    url = "https://fapi.binance.com/fapi/v1/klines"
    while True:
        d = http_json(url, params=f"symbol={SYMBOL}&interval=1h&limit=1000&startTime={start_ms}")
        if not d:
            break
        if candles and d and int(d[0][0]) <= int(candles[-1][0]):
            d = [k for k in d if int(k[0]) > int(candles[-1][0])]
        candles.extend(d)
        if len(d) < 1000:
            break
        start_ms = int(d[-1][0]) + 1
        time.sleep(0.15)
    # dédoublonne + coupe sur la fenêtre glissante
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
    y = today - dt.timedelta(days=1)
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
        prev_days = [str(y - dt.timedelta(days=j)) for j in range(1, RVOL_DAY_N + 1)]
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
                       premium, ls_hist, hl, fg_hist, dom_hist, mining) -> dict:
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
            "version": "vibe-market/1.0",
            "collector": "mac-launchd, 10 min",
            "errors": errors,
            "conventions": {
                "prix": "BTCUSDT (Binance USDT-M), USD arrondis",
                "rvol_hour": "_vol de la bougie horaire / moyenne des 20 dernières bougies à la même heure de Paris, en % (100 = normal)",
                "rvol_hour_current": "proratisé sur la fraction écoulée de l'heure en cours",
                "delta_btc": "2 x taker buy base - volume : flux agressif net acheteur en BTC (positif = achats dominants)",
                "oi": "open interest Binance en BTC",
                "funding": "Binance = taux par période de 8 h en % ; Hyperliquid = taux horaire en % (annualisé = x 24 x 365)",
                "ls": "ratios long/short Binance : top_pos = positions des top traders, top_acc = comptes des top traders, glob_acc = tous les comptes",
                "fg": "Fear & Greed alternative.me (0-100)",
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
    }


# ---------------------------------------------------------------------------
# Push Git
# ---------------------------------------------------------------------------


def git_push(no_git: bool) -> None:
    if no_git or not os.path.isdir(os.path.join(ROOT, ".git")):
        return
    stamp = now_paris().strftime("%Y-%m-%d %H:%M")
    for args in (
        ["git", "add", "-A", "data"],
        ["git", "commit", "--quiet", "-m", f"collecte {stamp}"],
        ["git", "push", "--quiet"],
    ):
        try:
            subprocess.run(
                args, cwd=ROOT, check=True, capture_output=True, timeout=120,
            )
        except Exception as e:  # offline, rien à committer, etc.
            log.info("git %s ignoré : %s", args[1], e)
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
        ls_hist, hl, fg_hist, dom_hist, mining,
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
