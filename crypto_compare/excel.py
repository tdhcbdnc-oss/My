"""Запись собранных метрик в Excel-шаблон.

Скрипт трогает только ячейки ввода (A–L, строки 5–14 листа «Сравнение»)
и веса на листе «Веса». Все формулы, условное форматирование и проверки
данных остаются из шаблона — Excel пересчитает балл сам.
"""
from __future__ import annotations

from datetime import date

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill

from scoring import INPUTS, SCORED

FIRST_ROW, MAX_ROWS = 5, 10
INPUT_COLS = dict(zip(INPUTS, "CDEFGHIJKL"))  # mcap -> C, ..., investors -> L
WEIGHT_ROW = {key: 4 + i for i, (key, _, _) in enumerate(SCORED)}

FILL_MANUAL = PatternFill("solid", fgColor="FFF2CC")  # жёлтый из шаблона: вводится вручную
FILL_AUTO = PatternFill("solid", fgColor="DDEBF7")    # голубой: подтянуто из API
FILL_WARN = PatternFill("solid", fgColor="F8CBAD")    # оранжевый: из API, но данные подозрительные
WARN = "⚠"


def read_manual_from_existing(path) -> dict[str, dict]:
    """Значения, которые пользователь сам вписал в прошлую версию файла (жёлтые ячейки)."""
    try:
        wb = load_workbook(path)
    except FileNotFoundError:
        return {}
    ws = wb["Сравнение"]
    out = {}
    for r in range(FIRST_ROW, FIRST_ROW + MAX_ROWS):
        name = ws[f"A{r}"].value
        if not name:
            continue
        for key, col in INPUT_COLS.items():
            c = ws[f"{col}{r}"]
            if isinstance(c.value, (int, float)) and not str(c.fill.fgColor.rgb)[-6:] in ("DDEBF7", "F8CBAD"):
                out.setdefault(str(name), {})[key] = c.value
    return out


def write(template, out_path, ecos: list[dict], values: dict, weights: dict, today: date):
    if len(ecos) > MAX_ROWS:
        raise ValueError(f"Шаблон рассчитан максимум на {MAX_ROWS} экосистем, передано {len(ecos)}")
    wb = load_workbook(template)
    ws = wb["Сравнение"]

    ws["A2"] = (f"Обновлено {today:%d.%m.%Y} скриптом collect.py. Голубые ячейки подтянуты из API "
                "(источник — в примечании к ячейке и на листе «Источники»), оранжевые — из API, но стоит "
                "проверить, жёлтые — ручной ввод. "
                "Зелёные колонки считаются автоматически.")
    ws["A2"].alignment = Alignment(wrap_text=False)

    for i in range(MAX_ROWS):
        r = FIRST_ROW + i
        for col in "AB" + "".join(INPUT_COLS.values()):
            c = ws[f"{col}{r}"]
            c.value, c.comment = None, None
            if col in INPUT_COLS.values():
                c.fill = FILL_MANUAL

    log_rows = []
    for i, e in enumerate(ecos):
        r = FIRST_ROW + i
        ws[f"A{r}"] = e["name"]
        ws[f"B{r}"] = e.get("category")
        for key, col in INPUT_COLS.items():
            v = values.get(e["name"], {}).get(key)
            if v is None:
                log_rows.append((e["name"], key, None, "нет данных — заполнить вручную", ""))
                continue
            c = ws[f"{col}{r}"]
            c.value = round(v.value, 6) if isinstance(v.value, float) else v.value
            if v.source.startswith("вручную"):
                c.fill = FILL_MANUAL
            else:
                c.fill = FILL_WARN if WARN in v.note else FILL_AUTO
                c.comment = Comment(f"{v.source}, {today:%d.%m.%Y}\n{v.note}".strip(), "collect.py",
                                    width=260, height=60)
            log_rows.append((e["name"], key, v.value, v.source, v.note))

    wv = wb["Веса"]
    for key, row in WEIGHT_ROW.items():
        if key in weights:
            wv[f"B{row}"] = weights[key]

    _write_sources_sheet(wb, log_rows, today)
    wb.save(out_path)


LABELS = {
    "mcap": "Капитализация, $ млн", "fdv": "FDV, $ млн", "tvl_now": "TVL сейчас, $ млн",
    "tvl_6m": "TVL 6 мес назад, $ млн", "addr_now": "Активные адреса/день сейчас",
    "addr_6m": "Активные адреса/день 6 мес назад", "revenue_1y": "Доход/комиссии за год, $ млн",
    "devs": "Активных разработчиков/мес", "unlock_6m": "Разлок за 6 мес, % предложения",
    "investors": "Инвесторы и команда, 1–5",
}


def _write_sources_sheet(wb, rows, today: date):
    if "Источники" in wb.sheetnames:
        del wb["Источники"]
    ws = wb.create_sheet("Источники")
    ws["A1"] = f"Откуда взята каждая цифра (сбор {today:%d.%m.%Y})"
    ws["A1"].font = Font(bold=True, size=13)
    head = ["Экосистема", "Метрика", "Значение", "Источник", "Комментарий"]
    for j, h in enumerate(head, 1):
        c = ws.cell(row=3, column=j, value=h)
        c.font = Font(bold=True)
    for i, (name, key, val, src, note) in enumerate(rows, 4):
        ws.cell(row=i, column=1, value=name)
        ws.cell(row=i, column=2, value=LABELS[key])
        c = ws.cell(row=i, column=3, value=val)
        c.number_format = "0.0%" if key == "unlock_6m" else "#,##0.##"
        ws.cell(row=i, column=4, value=src)
        ws.cell(row=i, column=5, value=note)
        fill = FILL_MANUAL if val is None else FILL_WARN if WARN in note else None
        if fill:
            for j in range(1, 6):
                ws.cell(row=i, column=j).fill = fill
    for col, w in zip("ABCDE", (18, 34, 16, 26, 60)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
