"""Выгрузка заданий в Excel. В файл попадают только задания, названия и суммы —
никаких настроек устройств и секретов."""
import io
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import db

STATUS_RU = {"printing": "печать", "paused": "пауза", "complete": "завершено",
             "cancelled": "прервано", "error": "ошибка", "manual": "вручную"}
SOURCE_RU = {"printer": "принтер", "plan": "план слайсера", "plan_progress": "план × прогресс",
             "manual": "вручную", "counter": "счётчик розетки", "power": "мощность розетки",
             "estimate": "оценка", "": ""}

COLUMNS = [
    ("Дата начала", 18), ("Принтер", 18), ("Файл", 40), ("Статус", 12), ("Время, ч", 10),
    ("Филамент", 22), ("Расход, г", 10), ("Источник расхода", 18), ("План, г", 10),
    ("Энергия, кВт·ч", 14), ("Источник энергии", 18), ("Филамент, ₽", 12), ("Энергия, ₽", 12),
    ("Амортизация, ₽", 14), ("Работа, ₽", 11), ("Себестоимость, ₽", 16), ("Цена, ₽", 12), ("Заметки", 30),
]


def _ts(v):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(v)) if v else ""


def build(date_from=None, date_to=None):
    where, params = [], []
    if date_from:
        where.append("started_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("started_at < ?")
        params.append(date_to)
    sql = ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY started_at"
    jobs = db.all_rows("jobs", sql, tuple(params))
    fils = {f["id"]: f for f in db.all_rows("filaments")}

    wb = Workbook()
    ws = wb.active
    ws.title = "Задания"
    head_fill = PatternFill("solid", fgColor="3B2370")
    for i, (name, width) in enumerate(COLUMNS, 1):
        c = ws.cell(row=1, column=i, value=name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    for j in jobs:
        f = fils.get(j["filament_id"]) or {}
        ws.append([
            _ts(j["started_at"]), j["printer_name"], j["filename"], STATUS_RU.get(j["status"], j["status"]),
            round((j["duration_s"] or 0) / 3600, 2),
            ("%s %s %s" % (f.get("material", ""), f.get("name", ""), f.get("color", ""))).strip(),
            j["filament_g"], SOURCE_RU.get(j["filament_source"] or "", j["filament_source"]), j["planned_g"],
            j["energy_kwh"], SOURCE_RU.get(j["energy_source"] or "", j["energy_source"]),
            j["cost_filament"], j["cost_energy"], j["cost_depreciation"], j["cost_labor"],
            j["cost_total"], j["price"], j["notes"],
        ])
    n = len(jobs) + 1
    if jobs:
        ws.append([])
        total_row = n + 2
        ws.cell(row=total_row, column=1, value="Итого").font = Font(bold=True)
        for col in (5, 7, 10, 12, 13, 14, 15, 16, 17):
            L = get_column_letter(col)
            c = ws.cell(row=total_row, column=col, value="=SUM(%s2:%s%d)" % (L, L, n))
            c.font = Font(bold=True)
    ws.freeze_panes = "A2"

    ws3 = wb.create_sheet("Пластики")
    ws3.append(["Дата начала", "Принтер", "Файл", "№", "Тип (по файлу)", "Цвет", "Слот AMS", "Катушка",
                "План, г", "Расход, г", "Стоимость, ₽"])
    for c in ws3[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
    jobs_by_id = {j["id"]: j for j in jobs}
    if jobs_by_id:
        ids = list(jobs_by_id)
        rows = db.all_rows("job_filaments", "WHERE job_id IN (%s) ORDER BY job_id, slot" % ",".join("?" * len(ids)),
                           tuple(ids))
        for p in rows:
            j = jobs_by_id[p["job_id"]]
            f = fils.get(p["filament_id"]) or {}
            ws3.append([_ts(j["started_at"]), j["printer_name"], j["filename"], p["slot"], p["material"], p["color_hex"],
                        p["tray"], ("%s %s %s" % (f.get("material", ""), f.get("name", ""), f.get("color", ""))).strip()
                        or "не назначена", p["planned_g"], p["grams"], p["cost"]])
    for col, w in zip("ABCDEFGHIJK", (18, 18, 40, 5, 14, 10, 10, 26, 10, 10, 12)):
        ws3.column_dimensions[col].width = w

    ws2 = wb.create_sheet("Филамент")
    ws2.append(["Название", "Материал", "Цвет", "Цена за кг, ₽", "Остаток, г"])
    for f in fils.values():
        ws2.append([f["name"], f["material"], f["color"], f["price_kg"], round(f["remaining_g"] or 0, 1)])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
