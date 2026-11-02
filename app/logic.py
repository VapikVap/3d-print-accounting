"""Расчёты: граммы из миллиметров, энергия, себестоимость задания."""
import math


def _f(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def mm_to_grams(length_mm, diameter_mm=1.75, density=1.24):
    """Масса нити по длине: объём цилиндра (см^3) * плотность (г/см^3)."""
    r_cm = _f(diameter_mm, 1.75) / 20.0
    volume_cm3 = math.pi * r_cm * r_cm * (_f(length_mm) / 10.0)
    return volume_cm3 * _f(density, 1.24)


def energy_delta_kwh(prev, cur, max_gap_s=60.0):
    """Прирост энергии между двумя показаниями розетки.

    prev/cur: dict(ts, power_w, energy_kwh). Если у розетки есть счётчик и он
    не уменьшился — берём разницу счётчика. Иначе (только мощность, сброс
    счётчика) — интегрируем мощность трапецией. Разрыв больше max_gap_s
    обрезается, чтобы пропуск связи не давал огромную ошибку.
    Возвращает (кВт*ч, источник).
    """
    if not prev or not cur:
        return 0.0, ""
    e0, e1 = prev.get("energy_kwh"), cur.get("energy_kwh")
    if e0 is not None and e1 is not None and e1 >= e0:
        return e1 - e0, "counter"
    p0, p1 = prev.get("power_w"), cur.get("power_w")
    if p0 is None and p1 is None:
        return 0.0, ""
    p0 = p1 if p0 is None else p0
    p1 = p0 if p1 is None else p1
    dt = max(0.0, min(_f(cur.get("ts")) - _f(prev.get("ts")), max_gap_s))
    return (p0 + p1) / 2.0 * dt / 3600.0 / 1000.0, "power"


def job_grams(job, filament):
    """Граммы задания: явные граммы, иначе пересчёт из мм по параметрам нити."""
    if job.get("filament_g") is not None:
        return _f(job["filament_g"])
    if job.get("filament_mm") is not None:
        f = filament or {}
        return mm_to_grams(job["filament_mm"], f.get("diameter_mm", 1.75), f.get("density", 1.24))
    return 0.0


def compute_costs(job, printer, filament, settings, filament_cost=None):
    """Себестоимость задания. Возвращает dict с полями cost_* и price.
    filament_cost — готовая стоимость нити (несколько катушек, см. parts.apply_costs)."""
    printer = printer or {}
    filament = filament or {}
    grams = job_grams(job, filament)
    hours = _f(job.get("duration_s")) / 3600.0
    kwh = _f(job.get("energy_kwh"))
    if not kwh and job.get("energy_source") in ("", None, "estimate") and hours:
        kwh = _f(printer.get("avg_power_w")) * hours / 1000.0
    c_fil = filament_cost if filament_cost is not None else grams * _f(filament.get("price_kg")) / 1000.0
    c_en = kwh * _f(settings.get("tariff_kwh"))
    c_dep = hours * _f(printer.get("depreciation_hour"))
    c_lab = _f(settings.get("labor_minutes")) / 60.0 * _f(settings.get("labor_hour"))
    total = c_fil + c_en + c_dep + c_lab
    price = total * (1 + _f(settings.get("markup_percent")) / 100.0)
    r = lambda v: round(v, 2)
    return {
        "grams": round(grams, 2),
        "energy_kwh_used": round(kwh, 4),
        "cost_filament": r(c_fil),
        "cost_energy": r(c_en),
        "cost_depreciation": r(c_dep),
        "cost_labor": r(c_lab),
        "cost_total": r(total),
        "price": r(price),
    }
