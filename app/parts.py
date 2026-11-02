"""Пластики задания (многоцветная печать): из чего напечатана деталь и с каких катушек.

Пластики берутся из файла нарезки (тип, цвет, граммы по каждому). Катушка
подбирается так:
  1. слот AMS, где сейчас заряжен такой же тип близкого цвета, и катушка,
     привязанная к этому слоту на вкладке принтера («tray»);
  2. иначе ваша катушка того же типа с ближайшим цветом («color»), или просто
     того же типа, если цвет не задан/далёк («type»);
  3. иначе не назначена — себестоимость нити по этому пластику 0, это видно в задании.
Ручной выбор катушки («manual») при повторном расчёте не перезаписывается.
"""
from . import db

COLOR_CLOSE = 80.0  # расстояние в RGB, ближе — «тот же цвет»


def _rgb(h):
    h = (h or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def color_distance(a, b):
    ra, rb = _rgb(a), _rgb(b)
    if not ra or not rb:
        return None
    return sum((x - y) ** 2 for x, y in zip(ra, rb)) ** 0.5


def same_material(a, b):
    """'PLA' ~ 'PLA', 'PETG' ~ 'PETG HF'/'PETG-CF' не равны: сравниваем базовый тип целиком."""
    return (a or "").strip().upper() == (b or "").strip().upper() and bool(a)


def slot_map(printer_id):
    return {r["tray"]: r["filament_id"] for r in db.all_rows("printer_slots", "WHERE printer_id = ?", (printer_id,))}


def set_slot(printer_id, tray, filament_id):
    with db.connect() as c:
        if filament_id:
            c.execute("INSERT OR REPLACE INTO printer_slots(printer_id, tray, filament_id) VALUES (?,?,?)",
                      (printer_id, tray, filament_id))
        else:
            c.execute("DELETE FROM printer_slots WHERE printer_id = ? AND tray = ?", (printer_id, tray))


def _closest(items, color, key="color_hex"):
    scored = []
    for it in items:
        d = color_distance(it.get(key), color)
        scored.append((d if d is not None else 9999, it))
    scored.sort(key=lambda x: x[0])
    return scored[0] if scored else (None, None)


def choose_spool(printer_id, material, color_hex, trays=None, spools=None, slots=None):
    """-> (filament_id | None, tray | '', match)"""
    spools = spools if spools is not None else db.all_rows("filaments", "WHERE archived = 0")
    slots = slots if slots is not None else (slot_map(printer_id) if printer_id else {})
    tray_label = ""
    loaded = [t for t in trays or [] if not t.get("empty") and same_material(t.get("material"), material)]
    if loaded:
        dist, tray = _closest(loaded, color_hex)
        if dist is not None and (dist <= COLOR_CLOSE or not color_hex or len(loaded) == 1):
            tray_label = tray["tray"]
            if slots.get(tray_label):
                return slots[tray_label], tray_label, "tray"
    same = [s for s in spools if same_material(s.get("material"), material)]
    if not same:
        return None, tray_label, ""
    dist, sp = _closest(same, color_hex)
    if dist is not None and dist <= COLOR_CLOSE:
        return sp["id"], tray_label, "color"
    if len(same) == 1 or not color_hex:
        return same[0]["id"], tray_label, "type"
    return sp["id"], tray_label, "type"


def list_parts(job_id):
    return db.all_rows("job_filaments", "WHERE job_id = ? ORDER BY slot", (job_id,))


def set_parts(job_id, printer_id, filaments, trays=None):
    """Создать/обновить пластики задания из плана. Ручные назначения сохраняются."""
    old = {p["slot"]: p for p in list_parts(job_id)}
    spools = db.all_rows("filaments", "WHERE archived = 0")
    slots = slot_map(printer_id) if printer_id else {}
    for f in filaments:
        prev = old.pop(f["slot"], None)
        vals = {"material": f.get("material") or "", "color_hex": f.get("color_hex") or "",
                "planned_g": f.get("planned_g")}
        if prev and prev["match"] == "manual":
            db.update("job_filaments", prev["id"], vals)
            continue
        fid, tray, match = choose_spool(printer_id, vals["material"], vals["color_hex"], trays, spools, slots)
        vals.update(filament_id=fid, tray=tray, match=match)
        if prev:
            db.update("job_filaments", prev["id"], vals)
        else:
            db.insert("job_filaments", dict(vals, job_id=job_id, slot=f["slot"]))
    for p in old.values():  # пластиков больше нет в плане
        if not p["deducted_g"]:
            db.delete("job_filaments", p["id"])


def scale(job_id, factor):
    """Граммы каждого пластика = план × factor (1 — завершено, прогресс — прервано/идёт)."""
    total = 0.0
    for p in list_parts(job_id):
        g = round((p["planned_g"] or 0) * factor, 2)
        db.update("job_filaments", p["id"], {"grams": g})
        total += g
    return round(total, 2)


def distribute(job_id, total):
    """Ручной итог граммов задания -> по пластикам пропорционально плану."""
    ps = list_parts(job_id)
    planned = sum(p["planned_g"] or 0 for p in ps)
    for p in ps:
        share = (p["planned_g"] or 0) / planned if planned else 1.0 / len(ps)
        db.update("job_filaments", p["id"], {"grams": round(total * share, 2)})


def apply_costs(job_id, active):
    """Стоимость по каждому пластику и списание с катушек. -> (граммы, стоимость, главная катушка)."""
    total_g, total_cost, main = 0.0, 0.0, (0, None)
    for p in list_parts(job_id):
        fil = db.get_row("filaments", p["filament_id"]) if p["filament_id"] else None
        g = p["grams"] or 0.0
        cost = round(g * float((fil or {}).get("price_kg") or 0) / 1000.0, 2)
        vals = {"cost": cost}
        if not active and fil:
            diff = g - (p["deducted_g"] or 0)
            if abs(diff) > 0.001:
                db.update("filaments", fil["id"], {"remaining_g": (fil["remaining_g"] or 0) - diff})
                vals["deducted_g"] = g
        db.update("job_filaments", p["id"], vals)
        total_g += g
        total_cost += cost
        if g > main[0]:
            main = (g, p["filament_id"])
    return round(total_g, 2), round(total_cost, 2), main[1]


def return_stock(part):
    """Вернуть списанное на катушку (смена катушки у пластика, удаление задания)."""
    if part["filament_id"] and part["deducted_g"]:
        f = db.get_row("filaments", part["filament_id"])
        if f:
            db.update("filaments", f["id"], {"remaining_g": (f["remaining_g"] or 0) + part["deducted_g"]})
    db.update("job_filaments", part["id"], {"deducted_g": 0})
