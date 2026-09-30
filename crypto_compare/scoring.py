"""Расчёт балла на Python — точная копия формул листов «Сравнение» и «Расчёт».

Excel считает сам; этот модуль нужен для отчёта в консоли, markdown-сводки
и истории снимков (там формул нет).
"""
from __future__ import annotations

INPUTS = ["mcap", "fdv", "tvl_now", "tvl_6m", "addr_now", "addr_6m",
          "revenue_1y", "devs", "unlock_6m", "investors"]

# Порядок совпадает со строками 4–11 листа «Веса».
SCORED = [
    ("tvl_growth", "Рост TVL 6м", +1),
    ("addr_growth", "Рост адресов 6м", +1),
    ("mcap_tvl", "Mcap / TVL", -1),
    ("mcap_rev", "Mcap / Доход", -1),
    ("devs", "Активных разработчиков", +1),
    ("unlock_6m", "Разлок за 6 мес", -1),
    ("fdv_mcap", "FDV / Mcap", -1),
    ("investors", "Инвесторы и команда", +1),
]


def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def derived(m: dict) -> dict:
    g = m.get
    tvl_g = _div(g("tvl_now"), g("tvl_6m"))
    addr_g = _div(g("addr_now"), g("addr_6m"))
    return {
        "tvl_growth": tvl_g - 1 if tvl_g is not None else None,
        "addr_growth": addr_g - 1 if addr_g is not None else None,
        "mcap_tvl": _div(g("mcap"), g("tvl_now")),
        "mcap_rev": _div(g("mcap"), g("revenue_1y")),
        "fdv_mcap": _div(g("fdv"), g("mcap")),
        "devs": g("devs"),
        "unlock_6m": g("unlock_6m"),
        "investors": g("investors"),
    }


def score(rows: dict[str, dict], weights: dict[str, float]) -> dict[str, dict]:
    """rows: {имя: {метрика_ввода: число|None}} -> {имя: {derived..., norm, score, rank}}."""
    res = {n: derived(m) for n, m in rows.items()}
    norm = {n: {} for n in rows}
    for key, _, direction in SCORED:
        vals = [r[key] for r in res.values() if r[key] is not None]
        if len(vals) < 2:
            continue
        lo, hi = min(vals), max(vals)
        for n, r in res.items():
            v = r[key]
            if v is None:
                continue
            if hi == lo:
                norm[n][key] = 0.5
            else:
                norm[n][key] = (v - lo) / (hi - lo) if direction > 0 else (hi - v) / (hi - lo)
    for n, r in res.items():
        num = sum(s * weights.get(k, 0) for k, s in norm[n].items())
        den = sum(weights.get(k, 0) for k in norm[n])
        r["norm"] = norm[n]
        r["score"] = 100 * num / den if den else None
    scores = [r["score"] for r in res.values() if r["score"] is not None]
    for r in res.values():
        r["rank"] = 1 + sum(s > r["score"] for s in scores) if r["score"] is not None else None
    return res
