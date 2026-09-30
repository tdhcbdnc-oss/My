#!/usr/bin/env python3
"""Сбор метрик по крипто-экосистемам и запись в Excel-шаблон.

Пример:
    python collect.py --config configs/l1.yaml
    python collect.py --config configs/l2.yaml --only Arbitrum,Base

Что делает:
  1. Читает список экосистем, веса и ручные значения из YAML-конфига.
  2. Тянет данные: CoinGecko (Mcap, FDV), DeFiLlama (TVL, комиссии/доход),
     Artemis (активные адреса, разработчики — при наличии ключа),
     Tokenomist (разлоки — при наличии ключа).
  3. Что не удалось подтянуть — берёт из блока manual конфига или из
     жёлтых ячеек прошлой версии выходного файла.
  4. Пишет Excel с формулами из шаблона, лист «Источники», JSON-снимок
     и markdown-сводку в data/.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

import excel
import scoring
import sources
from sources import Value

HERE = Path(__file__).resolve().parent
WARN = excel.WARN
log = logging.getLogger("collect")


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = set(k for k, _, _ in scoring.SCORED) - set(cfg.get("weights", {}))
    if missing:
        raise SystemExit(f"В конфиге нет весов для: {', '.join(sorted(missing))}")
    return cfg


def collect(cfg: dict, today: date) -> dict[str, dict[str, Value]]:
    ecos = cfg["ecosystems"]
    s = cfg.get("sources", {})
    lookback = s.get("lookback_days", 182)
    http = sources.Http()
    values: dict[str, dict[str, Value]] = {e["name"]: {} for e in ecos}

    def merge(part: dict, label: str):
        n = 0
        for name, metrics in part.items():
            values[name].update(metrics)
            n += sum(not k.startswith("_") for k in metrics)
        log.info("%-11s %d значений", label, n)

    steps = [
        ("CoinGecko", lambda: sources.coingecko(http, ecos)),
        ("DeFiLlama TVL", lambda: sources.defillama_tvl(http, ecos, today, lookback)),
        ("DeFiLlama fees", lambda: sources.defillama_fees(http, ecos, s.get("revenue_type", "dailyFees"))),
        ("Artemis", lambda: sources.artemis(http, ecos, today, lookback, s.get("artemis", {}))),
    ]
    for label, fn in steps:
        try:
            merge(fn(), label)
        except Exception as err:  # один упавший источник не должен ронять весь сбор
            log.warning("%s: источник недоступен: %s", label, err)

    supply = {n: m["_circulating_supply"].value for n, m in values.items() if "_circulating_supply" in m}
    try:
        merge(sources.tokenomist(http, ecos, today, s.get("unlock_horizon_days", 182), supply), "Tokenomist")
    except Exception as err:
        log.warning("Tokenomist: источник недоступен: %s", err)

    for m in values.values():
        m.pop("_circulating_supply", None)
        if brk := m.pop("_addr_break", None):
            for key in ("addr_now", "addr_6m"):
                if key in m:
                    m[key].note = f"{m[key].note}. {WARN} {brk.note}"
        for now, old in (("tvl_now", "tvl_6m"), ("addr_now", "addr_6m")):
            if now in m and old in m and m[old].value and not 0.1 <= m[now].value / m[old].value <= 10:
                if WARN not in m[now].note:
                    m[now].note += f". {WARN} изменение за 6 мес больше чем в 10 раз — проверь, реально ли это"
    return values


def apply_manual(cfg: dict, values: dict, previous: dict):
    """Ручные значения: конфиг перекрывает API; прошлый Excel — только пустые места."""
    for e in cfg["ecosystems"]:
        m = values[e["name"]]
        for key, v in (e.get("manual") or {}).items():
            if key not in scoring.INPUTS:
                raise SystemExit(f"{e['name']}: неизвестная метрика в manual: {key}")
            if v is not None:
                m[key] = Value(float(v), "вручную (конфиг)")
        for key, v in previous.get(e["name"], {}).items():
            m.setdefault(key, Value(float(v), "вручную (прошлый Excel)"))


def summary_md(cfg, values, result, today) -> str:
    fmt = lambda v, p=False: "—" if v is None else (f"{v:+.0%}" if p else f"{v:,.1f}")
    lines = [f"# {cfg.get('title', 'Сравнение экосистем')} — {today:%d.%m.%Y}", "",
             "| Место | Экосистема | Балл | Рост TVL | Рост адресов | Mcap/TVL | Mcap/Доход | FDV/Mcap | Нет данных | Проверить |",
             "|---:|---|---:|---:|---:|---:|---:|---:|---|---|"]
    order = sorted(result, key=lambda n: (result[n]["rank"] or 99, n))
    for n in order:
        r = result[n]
        miss = [excel.LABELS[k].split(",")[0] for k in scoring.INPUTS if k not in values[n]]
        warn = sorted({excel.LABELS[k].split(",")[0].replace(" сейчас", "").replace(" 6 мес назад", "")
                       for k, v in values[n].items() if WARN in v.note})
        lines.append(f"| {r['rank'] or '—'} | {n} | {fmt(r['score'])} | {fmt(r['tvl_growth'], True)} | "
                     f"{fmt(r['addr_growth'], True)} | {fmt(r['mcap_tvl'])} | {fmt(r['mcap_rev'])} | "
                     f"{fmt(r['fdv_mcap'])} | {', '.join(miss) or '—'} | {', '.join(warn) or '—'} |")
    lines += ["", f"{WARN} в колонке «Проверить» — в данных источника резкая ступенька (часто это смена методики, "
              "а не реальное изменение). Подробности — на листе «Источники»; при необходимости впиши значение в manual.",
              "", "Балл относительный (min-max внутри выборки), это не торговый сигнал."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=HERE / "configs" / "l1.yaml", type=Path)
    ap.add_argument("--out", type=Path, help="путь к xlsx (по умолчанию из конфига)")
    ap.add_argument("--only", help="через запятую: собрать только эти экосистемы")
    ap.add_argument("--no-history", action="store_true", help="не сохранять снимок в data/")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = load_config(a.config)
    if a.only:
        keep = {x.strip().lower() for x in a.only.split(",")}
        cfg["ecosystems"] = [e for e in cfg["ecosystems"] if e["name"].lower() in keep]
    today = datetime.now(timezone.utc).date()
    out = a.out or HERE / cfg.get("output", "output/crypto_ecosystems_comparison.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)

    values = collect(cfg, today)
    apply_manual(cfg, values, excel.read_manual_from_existing(out))

    template = HERE / cfg.get("template", "templates/crypto_ecosystems_comparison.xlsx")
    excel.write(template, out, cfg["ecosystems"], values, cfg["weights"], today)
    log.info("Excel: %s", out)

    plain = {n: {k: v.value for k, v in m.items()} for n, m in values.items()}
    result = scoring.score(plain, cfg["weights"])
    md = summary_md(cfg, values, result, today)
    print("\n" + md)

    if not a.no_history:
        slug = a.config.stem
        hist = HERE / "data" / slug
        hist.mkdir(parents=True, exist_ok=True)
        snap = {
            "date": today.isoformat(), "config": str(a.config.name), "weights": cfg["weights"],
            "ecosystems": {n: {"inputs": {k: {"value": v.value, "source": v.source, "note": v.note}
                                          for k, v in values[n].items()},
                               "score": result[n]["score"], "rank": result[n]["rank"],
                               "derived": {k: result[n][k] for k, _, _ in scoring.SCORED}}
                           for n in values},
        }
        (hist / f"{today}.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        (hist / "latest.md").write_text(md, encoding="utf-8")
        log.info("Снимок: %s", hist / f"{today}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
