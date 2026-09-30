"""Сбор метрик из открытых API.

Каждая функция возвращает словарь {имя_экосистемы: {метрика: Value}}.
Если источник недоступен или по сети нет данных, метрика просто не попадает
в результат — её можно заполнить вручную.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median

import requests

log = logging.getLogger(__name__)

TIMEOUT = 40
USER_AGENT = "crypto-compare/1.0"


@dataclass
class Value:
    value: float
    source: str
    note: str = ""


class Http:
    """requests.Session с повторами на 429/5xx и сетевые ошибки."""

    def __init__(self, retries: int = 4):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.retries = retries

    def get_json(self, url, params=None, headers=None):
        delay = 2
        for attempt in range(self.retries + 1):
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=TIMEOUT)
            except requests.RequestException as e:
                if attempt == self.retries:
                    raise
                log.debug("GET %s: %s, повтор через %ss", url, e, delay)
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == self.retries:
                        r.raise_for_status()
                    wait = int(r.headers.get("Retry-After", delay))
                    log.debug("GET %s: HTTP %s, повтор через %ss", url, r.status_code, wait)
                    time.sleep(wait)
                    delay *= 2
                    continue
                r.raise_for_status()
                return r.json()
            time.sleep(delay)
            delay *= 2


def _closest(points: list[tuple[date, float]], target: date) -> tuple[date, float] | None:
    if not points:
        return None
    return min(points, key=lambda p: abs((p[0] - target).days))


def _window_median(points: list[tuple[date, float]], end: date, days: int) -> float | None:
    vals = [v for d, v in points if end - timedelta(days=days) < d <= end]
    return median(vals) if vals else None


# --------------------------------------------------------------------------- CoinGecko

def coingecko(http: Http, ecos: list[dict]) -> dict:
    """Капитализация, FDV и циркулирующее предложение (нужно для расчёта % разлока)."""
    ids = {e["coingecko_id"]: e["name"] for e in ecos if e.get("coingecko_id")}
    if not ids:
        return {}
    headers, base = {}, "https://api.coingecko.com/api/v3"
    if key := os.getenv("COINGECKO_PRO_API_KEY"):
        headers["x-cg-pro-api-key"], base = key, "https://pro-api.coingecko.com/api/v3"
    elif key := os.getenv("COINGECKO_API_KEY"):
        headers["x-cg-demo-api-key"] = key
    data = http.get_json(f"{base}/coins/markets",
                         params={"vs_currency": "usd", "ids": ",".join(ids), "per_page": 250},
                         headers=headers)
    out = {}
    for row in data:
        name = ids.get(row["id"])
        if not name:
            continue
        m = out.setdefault(name, {})
        if row.get("market_cap"):
            m["mcap"] = Value(row["market_cap"] / 1e6, "CoinGecko")
        if row.get("fully_diluted_valuation"):
            m["fdv"] = Value(row["fully_diluted_valuation"] / 1e6, "CoinGecko")
        if row.get("circulating_supply"):
            m["_circulating_supply"] = Value(row["circulating_supply"], "CoinGecko")
    for cid, name in ids.items():
        if name not in out:
            log.warning("CoinGecko: id '%s' (%s) не найден", cid, name)
    return out


# --------------------------------------------------------------------------- DeFiLlama

def defillama_tvl(http: Http, ecos: list[dict], today: date, lookback_days: int) -> dict:
    out = {}
    for e in ecos:
        chain = e.get("defillama_chain")
        if not chain:
            continue
        try:
            hist = http.get_json(f"https://api.llama.fi/v2/historicalChainTvl/{chain}")
        except requests.HTTPError as err:
            log.warning("DeFiLlama TVL: сеть '%s' (%s): %s", chain, e["name"], err)
            continue
        pts = [(datetime.fromtimestamp(int(p["date"]), timezone.utc).date(), p["tvl"]) for p in hist]
        if not pts:
            continue
        last = pts[-1]
        old = _closest(pts, today - timedelta(days=lookback_days))
        m = out.setdefault(e["name"], {})
        m["tvl_now"] = Value(last[1] / 1e6, "DeFiLlama", f"на {last[0]}")
        if old and abs((old[0] - (today - timedelta(days=lookback_days))).days) <= 7:
            m["tvl_6m"] = Value(old[1] / 1e6, "DeFiLlama", f"на {old[0]}")
    return out


def defillama_fees(http: Http, ecos: list[dict], data_type: str) -> dict:
    """Комиссии (dailyFees) или доход (dailyRevenue) за последние 365 дней."""
    label = "комиссии" if data_type == "dailyFees" else "доход"
    out = {}
    for e in ecos:
        slug = e.get("defillama_fees")
        if not slug:
            continue
        try:
            d = http.get_json(f"https://api.llama.fi/summary/fees/{slug}",
                              params={"dataType": data_type})
        except requests.HTTPError as err:
            log.warning("DeFiLlama fees: '%s' (%s): %s", slug, e["name"], err)
            continue
        total = d.get("total1y")
        if total:
            out.setdefault(e["name"], {})["revenue_1y"] = Value(
                total / 1e6, "DeFiLlama", f"{label} за 365 дн ({d.get('name', slug)})")
    return out


# --------------------------------------------------------------------------- Artemis

ARTEMIS_BASE = "https://data-svc.artemisxyz.com/data/api"


def _artemis_series(http: Http, metric: str, ids: list[str], start: date, end: date) -> dict:
    params = {"symbols": ",".join(ids), "startDate": start.isoformat(), "endDate": end.isoformat()}
    if key := os.getenv("ARTEMIS_API_KEY"):
        params["APIKey"] = key
    data = http.get_json(f"{ARTEMIS_BASE}/{metric}/", params=params)
    res = {}
    for sym, metrics in data.get("data", {}).get("symbols", {}).items():
        series = metrics.get(metric)
        if isinstance(series, list):
            # последний день обычно неполный (0 или единицы) — отбрасываем его и нули
            res[sym] = [(date.fromisoformat(p["date"]), p["val"]) for p in series
                        if p.get("val") and date.fromisoformat(p["date"]) < end]
        else:
            log.info("Artemis %s/%s: %s", metric, sym, series)
    return res


def _level_break(points: list[tuple[date, float]], days: int, ratio: float = 5.0) -> str | None:
    """Ищет ступеньку: медиана соседних недель отличается больше чем в ratio раз.

    Так обычно выглядит смена методики у источника (например, фильтр спама),
    а не реальный рост/падение сети. Такие места стоит проверить глазами.
    """
    pts = sorted(points)
    for i in range(days, len(pts) - days):
        before = median(v for _, v in pts[i - days:i])
        after = median(v for _, v in pts[i:i + days])
        if before and after and max(before, after) / min(before, after) >= ratio:
            return (f"ступенька в данных около {pts[i][0]}: медиана {before:,.0f} → {after:,.0f} за неделю "
                    "— возможна смена методики источника, проверь вручную")
    return None


def artemis(http: Http, ecos: list[dict], today: date, lookback_days: int, cfg: dict) -> dict:
    """Активные адреса (медиана за 7 дней сейчас и 6 мес назад) и разработчики."""
    by_id = {e["artemis_id"]: e["name"] for e in ecos if e.get("artemis_id")}
    if not by_id:
        return {}
    out = {}
    avg_days = cfg.get("dau_avg_days", 7)
    old_end = today - timedelta(days=lookback_days)
    try:
        series = _artemis_series(http, "DAU", list(by_id), old_end - timedelta(days=avg_days + 3), today)
    except requests.RequestException as err:
        log.warning("Artemis DAU: %s", err)
        series = {}
    for aid, pts in series.items():
        name = by_id.get(aid)
        if not name or not pts:
            continue
        m = out.setdefault(name, {})
        last = max(d for d, _ in pts)
        now = _window_median(pts, last, avg_days)
        old = _window_median(pts, old_end, avg_days)
        if now:
            m["addr_now"] = Value(round(now), "Artemis", f"медиана за {avg_days} дн до {last}")
        if old:
            m["addr_6m"] = Value(round(old), "Artemis", f"медиана за {avg_days} дн до {old_end}")
        if brk := _level_break(pts, avg_days):
            m["_addr_break"] = Value(0, "Artemis", brk)

    dev_metric = cfg.get("dev_metric")
    if dev_metric:
        try:
            devs = _artemis_series(http, dev_metric, list(by_id), today - timedelta(days=40), today)
        except requests.RequestException as err:
            log.warning("Artemis %s: %s", dev_metric, err)
            devs = {}
        for aid, pts in devs.items():
            if pts and (name := by_id.get(aid)):
                d, v = max(pts)
                out.setdefault(name, {})["devs"] = Value(v, "Artemis", f"{dev_metric} на {d}")
    return out


# --------------------------------------------------------------------------- Tokenomist

def tokenomist(http: Http, ecos: list[dict], today: date, horizon_days: int,
               supply: dict[str, float]) -> dict:
    """Разлок за ближайшие N дней в % от циркулирующего предложения. Нужен TOKENOMIST_API_KEY."""
    key = os.getenv("TOKENOMIST_API_KEY")
    if not key:
        log.info("Tokenomist: нет TOKENOMIST_API_KEY — разлоки остаются для ручного ввода")
        return {}
    out = {}
    end = today + timedelta(days=horizon_days)
    for e in ecos:
        tid = e.get("tokenomist_id")
        circ = supply.get(e["name"])
        if not tid:
            continue
        if not circ:
            log.warning("Tokenomist: для %s нет циркулирующего предложения из CoinGecko", e["name"])
            continue
        total, page = 0.0, 1
        try:
            while True:
                d = http.get_json(f"https://api.tokenomist.ai/v5/daily-emission/{tid}",
                                  params={"start": today.isoformat(), "end": end.isoformat(),
                                          "page": page, "pageSize": 500},
                                  headers={"x-api-key": key})
                total += sum(r.get("unlockAmount") or 0 for r in d.get("data", []))
                if page >= (d.get("metadata") or {}).get("totalPages", 1):
                    break
                page += 1
        except requests.HTTPError as err:
            log.warning("Tokenomist '%s' (%s): %s", tid, e["name"], err)
            continue
        out.setdefault(e["name"], {})["unlock_6m"] = Value(
            total / circ, "Tokenomist", f"{total:,.0f} токенов / циркуляция {circ:,.0f}")
    return out
