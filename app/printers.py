"""Опрос принтеров (Moonraker, Bambu) и розеток, учёт заданий.

Фоновый поток раз в POLL_INTERVAL секунд:
  1. читает все розетки (мощность, счётчик энергии);
  2. читает статус каждого принтера;
  3. ведёт задания: создаёт при начале печати, обновляет прогресс/расход/энергию,
     закрывает при завершении и считает себестоимость.

Разбор ответов устройств вынесен в чистые функции parse_* — их покрывают тесты.
"""
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests

from . import bambu, bambu_ftp, db, logic, parts, slicer

POLL_INTERVAL = 5.0
HTTP_TIMEOUT = 4.0
MIN_INTERVAL = {"yandex": 15.0}  # облачный API: не дёргаем каждые 5 с
ACTIVE = ("printing", "paused")
FINAL = ("complete", "cancelled", "error", "standby")
CONNECTING = "подключение…"
NEED_CODE = "нужен код доступа"
OFFLINE_AFTER = 3  # опросов подряд без ответа до записи «Нет связи» в журнал


def _base_url(host, default_port):
    host = (host or "").strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return host
    if ":" not in host and default_port:
        host = "%s:%s" % (host, default_port)
    return "http://" + host


def _http_error_text(e):
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return "таймаут подключения (устройство не отвечает или неверный IP)"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "соединение отклонено/недоступно (порт закрыт или сервис не запущен)"
    if isinstance(e, requests.exceptions.ReadTimeout):
        return "таймаут ответа"
    if isinstance(e, requests.exceptions.HTTPError):
        return "HTTP %s" % e.response.status_code
    return str(e)


# ======================= Moonraker =======================

def mr_get(printer, path, params=None):
    url = _base_url(printer["host"], printer.get("port") or 7125) + path
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


MR_QUERY = "print_stats&virtual_sdcard&extruder&heater_bed"
# статус задания в /server/history -> статус приложения
HISTORY_STATUS = {"completed": "complete", "cancelled": "cancelled", "error": "error",
                  "klippy_shutdown": "error", "klippy_disconnect": "error", "server_exit": "error"}


def parse_moonraker_status(j):
    """Ответ /printer/objects/query -> общий статус."""
    st = (j.get("result") or {}).get("status") or {}
    ps = st.get("print_stats") or {}
    vs = st.get("virtual_sdcard") or {}
    return {
        "raw_state": ps.get("state"),
        "state": ps.get("state"),
        "filename": ps.get("filename") or "",
        "filament_mm": ps.get("filament_used"),
        "print_duration": ps.get("print_duration"),
        "total_duration": ps.get("total_duration"),
        "progress": vs.get("progress"),
        "nozzle_temp": (st.get("extruder") or {}).get("temperature"),
        "bed_temp": (st.get("heater_bed") or {}).get("temperature"),
        "message": ps.get("message") or "",
    }


def parse_moonraker_metadata(j):
    m = j.get("result") or {}
    return {
        "planned_s": m.get("estimated_time"),
        "planned_mm": m.get("filament_total"),
        "planned_g": m.get("filament_weight_total"),
        "slicer": m.get("slicer"),
    }


def moonraker_status(printer):
    return parse_moonraker_status(mr_get(printer, "/printer/objects/query?" + MR_QUERY))


def moonraker_metadata(printer, filename):
    try:
        return parse_moonraker_metadata(mr_get(printer, "/server/files/metadata", {"filename": filename}))
    except Exception:
        return {}


def moonraker_history_job(printer, filename, started_at):
    """Последнее задание из истории Moonraker с тем же именем файла."""
    try:
        j = mr_get(printer, "/server/history/list", {"limit": 10, "order": "desc"})
    except Exception:
        return None
    same = [job for job in (j.get("result") or {}).get("jobs") or [] if job.get("filename") == filename]
    if started_at:
        # тот же файл мог печататься раньше — берём запись, начавшуюся рядом с нашим заданием
        same = [job for job in same if abs((job.get("start_time") or 0) - started_at) < 120]
        same.sort(key=lambda job: abs((job.get("start_time") or 0) - started_at))
    return same[0] if same else None


def moonraker_test(printer):
    steps, raw, ok = [], {}, True
    for name, path in (("server_info", "/server/info"),
                       ("printer_info", "/printer/info"),
                       ("status", "/printer/objects/query?" + MR_QUERY),
                       ("history", "/server/history/list?limit=3&order=desc")):
        try:
            raw[name] = mr_get(printer, path)
            steps.append("%s: OK" % path)
        except Exception as e:
            raw[name] = None
            steps.append("%s: %s" % (path, _http_error_text(e)))
            if name in ("server_info", "status"):
                ok = False
            if name == "server_info":
                break
    parsed = parse_moonraker_status(raw["status"]) if raw.get("status") else None
    fname = (parsed or {}).get("filename")
    if not fname and raw.get("history"):
        jobs = (raw["history"].get("result") or {}).get("jobs") or []
        fname = jobs[0].get("filename") if jobs else None
    if fname:
        try:
            raw["metadata"] = mr_get(printer, "/server/files/metadata", {"filename": fname})
            steps.append("metadata(%s): OK" % fname)
        except Exception as e:
            steps.append("metadata(%s): %s" % (fname, _http_error_text(e)))
    if ok and parsed:
        missing = [k for k in ("state", "filament_mm") if parsed.get(k) is None]
        if missing:
            steps.append("В ответе нет полей: %s — пришлите сырой ответ, разберём." % ", ".join(missing))
    return {"ok": ok, "message": "; ".join(steps), "parsed": parsed, "raw": raw}


# ======================= Розетки =======================

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_shelly1(j):
    """Shelly Gen1 /status: meters[].power (Вт), meters[].total — ватт-минуты.
    У моделей с emeters (EM, 3EM) total — Вт*ч."""
    if j.get("meters"):
        m = j["meters"][0]
        total = _num(m.get("total"))
        energy = total / 60000.0 if total is not None else None
        power = _num(m.get("power"))
    elif j.get("emeters"):
        power = sum(_num(m.get("power")) or 0 for m in j["emeters"])
        totals = [_num(m.get("total")) for m in j["emeters"] if m.get("total") is not None]
        energy = sum(totals) / 1000.0 if totals else None
    else:
        raise ValueError("в ответе нет meters/emeters")
    relays = j.get("relays") or []
    return {"power_w": power, "energy_kwh": energy, "on": relays[0].get("ison") if relays else None}


def parse_shelly2(j):
    """Shelly Gen2/Plus /rpc/Switch.GetStatus?id=0: apower (Вт), aenergy.total (Вт*ч)."""
    if "apower" not in j:
        raise ValueError("в ответе нет apower")
    total = _num((j.get("aenergy") or {}).get("total"))
    return {"power_w": _num(j.get("apower")),
            "energy_kwh": total / 1000.0 if total is not None else None,
            "on": j.get("output")}


def parse_tasmota(j):
    """Tasmota /cm?cmnd=Status%208: StatusSNS.ENERGY.Power (Вт), Total (кВт*ч)."""
    en = (j.get("StatusSNS") or {}).get("ENERGY")
    if not en:
        raise ValueError("в ответе нет StatusSNS.ENERGY (нет датчика энергии?)")
    p = en.get("Power")
    if isinstance(p, list):
        p = sum(_num(x) or 0 for x in p)
    return {"power_w": _num(p), "energy_kwh": _num(en.get("Total")), "on": None}


def parse_tuya(status, plug):
    """tinytuya status(): {'dps': {...}}. Номера и множители — из настроек розетки."""
    if not isinstance(status, dict) or "dps" not in status:
        err = (status or {}).get("Error") if isinstance(status, dict) else None
        raise ValueError("Tuya: %s" % (err or "нет dps в ответе"))
    dps = status["dps"]
    p_key, e_key = str(plug.get("dps_power") or ""), str(plug.get("dps_energy") or "")
    p = _num(dps.get(p_key)) if p_key else None
    e = _num(dps.get(e_key)) if e_key else None
    return {
        "power_w": p * float(plug.get("power_scale") or 0.1) if p is not None else None,
        "energy_kwh": e * float(plug.get("energy_scale") or 0.001) if e is not None else None,
        "on": dps.get("1"),
    }


def parse_yandex(j, instance="power"):
    """Ответ GET /v1.0/devices/{id}: ищем свойство с parameters.instance == instance."""
    if j.get("status") not in (None, "ok"):
        raise ValueError("Яндекс: %s %s" % (j.get("status"), j.get("message", "")))
    power = None
    for prop in j.get("properties") or []:
        inst = (prop.get("parameters") or {}).get("instance") or (prop.get("state") or {}).get("instance")
        if inst == instance:
            power = _num((prop.get("state") or {}).get("value"))
    on = None
    for cap in j.get("capabilities") or []:
        if (cap.get("state") or {}).get("instance") == "on":
            on = cap["state"].get("value")
    if power is None:
        names = [((p.get("parameters") or {}).get("instance")) for p in j.get("properties") or []]
        raise ValueError("Яндекс: нет свойства '%s'. Есть: %s" % (instance, ", ".join(map(str, names)) or "ничего"))
    return {"power_w": power, "energy_kwh": None, "on": on}


TUYA_VERSIONS = ("3.3", "3.4", "3.5", "3.1")


def _tuya_status(plug, version):
    import tinytuya
    d = tinytuya.OutletDevice(dev_id=plug["device_id"], address=plug["host"],
                              local_key=plug["plug_key"], version=float(version))
    d.set_socketTimeout(3)
    d.set_socketRetryLimit(1)
    try:
        return d.status()
    finally:
        try:
            d.close()
        except Exception:
            pass


def read_plug_raw(plug):
    """Сырой ответ розетки (dict). Исключение при ошибке связи."""
    kind = plug["kind"]
    if kind == "shelly1":
        r = requests.get(_base_url(plug["host"], None) + "/status", timeout=HTTP_TIMEOUT)
    elif kind == "shelly2":
        r = requests.get(_base_url(plug["host"], None) + "/rpc/Switch.GetStatus", params={"id": 0},
                         timeout=HTTP_TIMEOUT)
    elif kind == "tasmota":
        r = requests.get(_base_url(plug["host"], None) + "/cm", params={"cmnd": "Status 8"},
                         timeout=HTTP_TIMEOUT)
    elif kind == "yandex":
        r = requests.get("https://api.iot.yandex.net/v1.0/devices/%s" % plug["device_id"],
                         headers={"Authorization": "Bearer %s" % plug["token"]}, timeout=8)
    elif kind == "tuya":
        return _tuya_status(plug, plug.get("tuya_version") or "3.3")
    else:
        raise ValueError("неизвестный тип розетки: %s" % kind)
    r.raise_for_status()
    return r.json()


def parse_plug(plug, raw):
    kind = plug["kind"]
    if kind == "shelly1":
        return parse_shelly1(raw)
    if kind == "shelly2":
        return parse_shelly2(raw)
    if kind == "tasmota":
        return parse_tasmota(raw)
    if kind == "tuya":
        return parse_tuya(raw, plug)
    if kind == "yandex":
        return parse_yandex(raw, plug.get("yandex_instance") or "power")
    raise ValueError(kind)


def read_plug(plug):
    out = parse_plug(plug, read_plug_raw(plug))
    out["ts"] = time.time()
    return out


def plug_test(plug):
    """Проверка связи с розеткой: сырой ответ + разбор. Для Tuya подбирает версию протокола."""
    if plug["kind"] == "tuya":
        tried = []
        versions = [plug["tuya_version"]] if plug.get("tuya_version") else []
        versions += [v for v in TUYA_VERSIONS if v not in versions]
        for v in versions:
            try:
                raw = _tuya_status(plug, v)
            except Exception as e:
                tried.append("%s: %s" % (v, e))
                continue
            if isinstance(raw, dict) and "dps" in raw:
                try:
                    parsed = parse_tuya(raw, plug)
                except ValueError as e:
                    parsed, msg = None, str(e)
                else:
                    msg = "Версия протокола %s подходит. dps: %s" % (v, ", ".join(sorted(raw["dps"])))
                    if parsed["power_w"] is None:
                        msg += ". Нет dps мощности '%s' — выберите номер по сырому ответу." % plug.get("dps_power")
                return {"ok": parsed is not None, "message": msg, "parsed": parsed, "raw": raw,
                        "tuya_version": v}
            tried.append("%s: %s" % (v, (raw or {}).get("Error") if isinstance(raw, dict) else raw))
        return {"ok": False, "message": "Ни одна версия не ответила. " + "; ".join(tried),
                "parsed": None, "raw": None}
    try:
        raw = read_plug_raw(plug)
    except Exception as e:
        return {"ok": False, "message": _http_error_text(e), "parsed": None, "raw": None}
    try:
        parsed = parse_plug(plug, raw)
    except Exception as e:
        return {"ok": False, "message": "Ответ получен, но не разобран: %s" % e, "parsed": None, "raw": raw}
    msg = "Мощность: %s Вт" % parsed["power_w"]
    msg += "; счётчик: %s кВт·ч" % parsed["energy_kwh"] if parsed["energy_kwh"] is not None else "; счётчика нет, энергия будет считаться по мощности"
    return {"ok": True, "message": msg, "parsed": parsed, "raw": raw}


# ======================= Задания =======================

def active_job(printer_id):
    rows = db.all_rows("jobs", "WHERE printer_id = ? AND status IN ('printing','paused') ORDER BY id DESC LIMIT 1",
                       (printer_id,))
    return rows[0] if rows else None


def grams_from_mm(job, filament):
    """Граммы по длине: если слайсер дал и мм, и граммы — берём его соотношение
    (учитывает реальную плотность из профиля), иначе по параметрам катушки."""
    mm = job.get("filament_mm")
    if mm is None:
        return None
    if job.get("planned_g") and job.get("planned_mm"):
        return mm * job["planned_g"] / job["planned_mm"]
    f = filament or {}
    return logic.mm_to_grams(mm, f.get("diameter_mm", 1.75), f.get("density", 1.24))


def recalc_job(job_id):
    """Пересчитать себестоимость и списание со склада для задания."""
    job = db.get_row("jobs", job_id)
    if not job:
        return None
    printer = db.get_row("printers", job["printer_id"]) if job["printer_id"] else None
    if parts.list_parts(job_id):
        # несколько пластиков: стоимость и списание по каждой катушке
        if job["stock_deducted_g"] and job["filament_id"]:  # раньше списывали одной катушкой — вернуть
            f = db.get_row("filaments", job["filament_id"])
            if f:
                db.update("filaments", f["id"], {"remaining_g": (f["remaining_g"] or 0) + job["stock_deducted_g"]})
            db.update("jobs", job_id, {"stock_deducted_g": 0})
        if job["filament_source"] == "manual" and job["filament_g"] is not None:
            parts.distribute(job_id, job["filament_g"])  # ручной итог главнее плана
        grams, fil_cost, main = parts.apply_costs(job_id, job["status"] in ACTIVE)
        costs = logic.compute_costs(dict(job, filament_g=grams), printer, None, db.get_settings(), filament_cost=fil_cost)
        vals = {k: v for k, v in costs.items() if k.startswith("cost_") or k == "price"}
        vals["filament_g"] = grams
        if main:
            vals["filament_id"] = main
        db.update("jobs", job_id, vals)
        return db.get_row("jobs", job_id)
    fil = db.get_row("filaments", job["filament_id"]) if job["filament_id"] else None
    costs = logic.compute_costs(job, printer, fil, db.get_settings())
    vals = {k: v for k, v in costs.items() if k.startswith("cost_") or k == "price"}
    if job["status"] not in ACTIVE and fil:
        diff = costs["grams"] - (job["stock_deducted_g"] or 0)
        if abs(diff) > 0.001:
            db.update("filaments", fil["id"], {"remaining_g": (fil["remaining_g"] or 0) - diff})
            vals["stock_deducted_g"] = costs["grams"]
    db.update("jobs", job_id, vals)
    return db.get_row("jobs", job_id)


def _plan_from_slicer(filename):
    folder = db.get_settings().get("slicer_dir")
    f = slicer.match(folder, filename) if folder else None
    if not f:
        return {}
    return {"slicer_file": f["path"], "planned_g": f.get("filament_g"),
            "planned_mm": f.get("filament_mm"), "planned_s": f.get("est_time_s"),
            "filaments": f.get("filaments") or []}


AUTO_SOURCES = ("", None, "plan", "plan_progress")  # расход, который можно пересчитать по плану


def _plan_grams(job_id, planned_g, factor):
    """Граммы по плану × factor: по каждому пластику, если они есть, иначе по сумме."""
    if parts.list_parts(job_id):
        return parts.scale(job_id, factor)
    return round(planned_g * factor, 2) if planned_g else None



class Poller:
    def __init__(self, interval=POLL_INTERVAL):
        self.interval = interval
        self.live = {}            # printer_id -> статус для интерфейса
        self.plug_live = {}       # plug_id -> последнее показание/ошибка
        self._plug_prev = {}      # plug_id -> предыдущее показание для энергии
        self._plug_next = {}      # plug_id -> когда можно читать снова
        self._bambu = {}          # printer_id -> (подпись настроек, BambuClient)
        self._stop = threading.Event()
        self._thread = None
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._tick_lock = threading.Lock()
        self.fetch_plan = bambu_ftp.fetch_plan   # подменяется в тестах
        self._ftp_tried = set()                  # job_id, для которых уже качали файл

    def _schedule(self, fn, *args):
        """Фоновая работа (FTP может занимать секунды). В тестах — синхронно."""
        return self._pool.submit(fn, *args)

    # ---- жизненный цикл ----
    def start(self):
        self._thread = threading.Thread(target=self._run, name="poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        for _sig, c in self._bambu.values():
            c.stop()

    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.tick()
            except Exception:
                db.log_event("error", "poller", traceback.format_exc(limit=3))
            self._stop.wait(max(0.5, self.interval - (time.time() - t0)))

    # ---- Bambu клиенты ----
    def bambu_client(self, printer):
        sig = (printer["host"], printer["serial"], printer["access_code"], printer.get("port") or 8883)
        cur = self._bambu.get(printer["id"])
        if cur and cur[0] == sig:
            return cur[1]
        if cur:
            cur[1].stop()
        c = bambu.BambuClient(printer["host"], printer["serial"], printer["access_code"], sig[3])
        c.start()
        self._bambu[printer["id"]] = (sig, c)
        return c

    def existing_bambu_client(self, printer_id):
        cur = self._bambu.get(printer_id)
        return cur[1] if cur else None

    # ---- один цикл опроса ----
    def tick(self, now=None):
        with self._tick_lock:
            now = now or time.time()
            plugs = {p["id"]: p for p in db.all_rows("plugs", "WHERE enabled = 1")}
            printers = db.all_rows("printers", "WHERE enabled = 1")
            enabled_ids = {p["id"] for p in printers}
            for pid in list(self._bambu):
                if pid not in enabled_ids:
                    self._bambu.pop(pid)[1].stop()

            deltas = self._read_plugs(plugs, now)
            futures = {p["id"]: self._pool.submit(self._read_printer, p) for p in printers}
            for p in printers:
                try:
                    status, err = futures[p["id"]].result(timeout=15)
                except Exception as e:
                    status, err = None, str(e)
                try:
                    self._handle(p, status, err, deltas.get(p.get("plug_id")), now)
                except Exception:
                    db.log_event("error", p["name"], traceback.format_exc(limit=3))

    def _read_plugs(self, plugs, now):
        due = [p for pid, p in plugs.items() if self._plug_next.get(pid, 0) <= now]
        futures = {p["id"]: self._pool.submit(read_plug, p) for p in due}
        deltas = {}
        for pid, fut in futures.items():
            plug = plugs[pid]
            self._plug_next[pid] = now + MIN_INTERVAL.get(plug["kind"], 0) - 0.5
            try:
                reading = fut.result(timeout=15)
            except Exception as e:
                was_ok = self.plug_live.get(pid, {}).get("ok", True)
                self.plug_live[pid] = {"ok": False, "error": str(e), "ts": now}
                if was_ok:
                    db.log_event("warn", plug["name"], "Розетка не отвечает: %s" % e)
                continue
            reading["ts"] = now
            prev = self._plug_prev.get(pid)
            deltas[pid] = logic.energy_delta_kwh(prev, reading) if prev else (0.0, "")
            self._plug_prev[pid] = reading
            self.plug_live[pid] = dict(reading, ok=True, error="")
        return deltas

    def _read_printer(self, p):
        try:
            if p["kind"] == "moonraker":
                return moonraker_status(p), ""
            if p["kind"] == "bambu":
                if not p.get("access_code"):
                    return None, NEED_CODE  # найден автопоиском, код ещё не введён — не подключаемся
                st = self.bambu_client(p).status()
                if not st["connected"] and not st["state"]:
                    return None, st["error"] or CONNECTING
                if st["age_s"] is not None and st["age_s"] > 120:
                    self.bambu_client(p).request_full()
                return st, ""
            return None, "неизвестный тип принтера"
        except Exception as e:
            return None, _http_error_text(e)

    def _handle(self, p, status, err, plug_delta, now):
        pid = p["id"]
        job = active_job(pid)
        prev_live = self.live.get(pid) or {}
        live = {"online": status is not None, "error": err, "ts": now,
                "plug": self.plug_live.get(p.get("plug_id")) if p.get("plug_id") else None}
        if status:
            live.update(status)
        # в журнал — только устойчивая потеря связи (3 опроса подряд), без тревоги на старте
        fails = 0 if status else prev_live.get("fails", 0) + 1
        live["fails"] = fails
        live["reported"] = prev_live.get("reported", False)
        if err == CONNECTING and fails < OFFLINE_AFTER:
            live["online"] = None  # интерфейс: «подключение…»
        if err == NEED_CODE:
            live.update(online=None, need_code=True)
        elif fails == OFFLINE_AFTER:
            db.log_event("warn", p["name"], "Нет связи: %s" % err)
            live["reported"] = True
        elif status and live["reported"]:
            db.log_event("info", p["name"], "Связь восстановлена")
            live["reported"] = False

        # энергия идёт в активное задание, даже если принтер временно не отвечает
        if job and plug_delta and plug_delta[0] > 0:
            src = plug_delta[1] if job["energy_source"] in ("", plug_delta[1]) else job["energy_source"]
            if job["energy_source"] == "power" and plug_delta[1] == "counter":
                src = "counter"
            db.update("jobs", job["id"], {"energy_kwh": (job["energy_kwh"] or 0) + plug_delta[0],
                                          "energy_source": src})
            job = db.get_row("jobs", job["id"])

        if not status:
            self.live[pid] = live
            return
        state = status.get("state")

        if state in ACTIVE:
            if job and status.get("filename") and job["filename"] != status["filename"]:
                self._finalize(p, job, status, "cancelled", now)
                job = None
            if not job:
                job = self._create_job(p, status, now)
            self._update_active(p, job, status, now)
        elif job and state in FINAL:
            final = "cancelled" if state == "standby" else state
            self._finalize(p, job, status, final, now)
        live["job_id"] = (active_job(pid) or {}).get("id")
        self.live[pid] = live

    def _create_job(self, p, status, now):
        fn = status.get("filename") or "без имени"
        started, note = now, ""
        if p["kind"] == "moonraker" and status.get("total_duration"):
            started = now - float(status["total_duration"])
        prog, rem = status.get("progress") or 0, status.get("remaining_s")
        if p["kind"] == "bambu" and 0.02 <= prog < 1 and rem:
            # приложение увидело печать уже начатой: прошло ≈ осталось × p / (1 − p)
            started = now - rem * prog / (1 - prog)
            note = "Время начала оценено: приложение увидело печать на %d %%" % round(prog * 100)
        vals = {"printer_id": p["id"], "printer_name": p["name"], "filename": fn, "status": status["state"],
                "started_at": started, "filament_id": p.get("filament_id"), "progress": prog, "notes": note}
        plan = {}
        if p["kind"] == "moonraker":
            md = moonraker_metadata(p, fn)
            plan = {"planned_g": md.get("planned_g"), "planned_mm": md.get("planned_mm"), "planned_s": md.get("planned_s")}
        if not plan.get("planned_g") and not plan.get("planned_mm"):
            plan = _plan_from_slicer(fn) or plan
        vals.update(plan)
        job_id = db.insert("jobs", vals)
        if plan.get("filaments"):
            parts.set_parts(job_id, p["id"], plan["filaments"], status.get("trays"))
        db.log_event("info", p["name"], "Начало печати: %s" % fn)
        if p["kind"] == "bambu" and p.get("access_code") and job_id not in self._ftp_tried:
            # файл, отправленный из Bambu Studio, лежит на принтере — берём план оттуда
            self._ftp_tried.add(job_id)
            self._schedule(self._fetch_bambu_plan, p, job_id, fn, status.get("gcode_file") or "", status.get("trays"))
        return db.get_row("jobs", job_id)

    def _fetch_bambu_plan(self, p, job_id, name, gcode_file, trays=None):
        try:
            plan, _local, msg = self.fetch_plan(p, name, gcode_file)
        except Exception as e:
            plan, msg = None, "FTP: %s" % e
        if plan and plan.get("filaments"):
            msg += "; пластики: " + ", ".join("%s %s %.1f г" % (f["material"], f["color_hex"], f["planned_g"])
                                             for f in plan["filaments"])
        db.log_event("info" if plan else "warn", p["name"], msg)
        if plan:
            apply_plan(job_id, plan, trays)


    def _update_active(self, p, job, status, now):
        vals = {"status": status["state"]}
        if status.get("progress") is not None:
            vals["progress"] = status["progress"]
        if p["kind"] == "moonraker" and status.get("total_duration") is not None:
            vals["duration_s"] = float(status["total_duration"])
        else:
            vals["duration_s"] = now - (job["started_at"] or now)
        if status.get("filament_mm") is not None:
            vals["filament_mm"] = float(status["filament_mm"])
            fil = db.get_row("filaments", job["filament_id"]) if job["filament_id"] else None
            j = dict(job)
            j["filament_mm"] = vals["filament_mm"]
            g = grams_from_mm(j, fil)
            vals["filament_g"] = round(g, 2) if g is not None else None
            vals["filament_source"] = "printer"
        elif job["filament_source"] in AUTO_SOURCES and (job.get("planned_g") or parts.list_parts(job["id"])):
            vals["filament_g"] = _plan_grams(job["id"], job["planned_g"], vals.get("progress") or 0)
            vals["filament_source"] = "plan_progress"
        if not job["slicer_file"] and not job["planned_g"]:
            plan = _plan_from_slicer(job["filename"])
            if plan:
                vals.update(plan)
                if plan.get("filaments"):
                    parts.set_parts(job["id"], p["id"], plan["filaments"], status.get("trays"))
        db.update("jobs", job["id"], vals)
        recalc_job(job["id"])

    def _finalize(self, p, job, status, final, now):
        vals = {"status": final, "ended_at": now}
        if p["kind"] == "moonraker":
            vals["duration_s"] = float(status.get("total_duration") or 0) or (now - (job["started_at"] or now))
            hist = moonraker_history_job(p, job["filename"], job["started_at"])
            mm = status.get("filament_mm")
            if hist:
                vals["external_id"] = str(hist.get("job_id") or "")
                if hist.get("status") in HISTORY_STATUS:
                    vals["status"] = HISTORY_STATUS[hist["status"]]
                if hist.get("filament_used") is not None:
                    mm = hist["filament_used"]
                if hist.get("total_duration"):
                    vals["duration_s"] = float(hist["total_duration"])
                if hist.get("end_time"):
                    vals["ended_at"] = float(hist["end_time"])
            # после сброса (standby) print_stats обнуляется — не затираем накопленное
            if mm is not None and (mm > 0 or job["filament_mm"] is None):
                vals["filament_mm"] = float(mm)
            mm_final = vals.get("filament_mm", job["filament_mm"])
            if mm_final is not None:
                fil = db.get_row("filaments", job["filament_id"]) if job["filament_id"] else None
                j = dict(job)
                j["filament_mm"] = mm_final
                vals["filament_g"] = round(grams_from_mm(j, fil), 2)
                vals["filament_source"] = "printer"
        else:
            vals["duration_s"] = now - (job["started_at"] or now)
            planned = job["planned_g"]
            if not planned and not parts.list_parts(job["id"]):
                plan = _plan_from_slicer(job["filename"])
                planned = plan.get("planned_g")
                if plan:
                    vals.update(plan)
                    if plan.get("filaments"):
                        parts.set_parts(job["id"], p["id"], plan["filaments"], status.get("trays"))
            if final == "complete":
                vals["progress"] = 1.0
            if job["filament_source"] in AUTO_SOURCES and (planned or parts.list_parts(job["id"])):
                factor = 1.0 if final == "complete" else (job["progress"] or 0)
                vals["filament_g"] = _plan_grams(job["id"], planned, factor)
                vals["filament_source"] = "plan" if final == "complete" else "plan_progress"
        if not job["energy_source"]:
            hours = (vals.get("duration_s") or 0) / 3600.0
            vals["energy_kwh"] = round((p.get("avg_power_w") or 0) * hours / 1000.0, 4)
            vals["energy_source"] = "estimate"
        db.update("jobs", job["id"], vals)
        recalc_job(job["id"])
        names = {"complete": "Печать завершена", "cancelled": "Печать прервана", "error": "Печать с ошибкой"}
        db.log_event("info", p["name"], "%s: %s" % (names.get(vals["status"], vals["status"]), job["filename"]))


def apply_plan(job_id, plan, trays=None):
    """Записать план (и пластики) в задание. Если задание уже закончилось (файл пришёл
    позже) — пересчитать расход по плану, не трогая данные с принтера и ручные правки."""
    job = db.get_row("jobs", job_id)
    if not job:
        return
    vals = {k: plan[k] for k in ("planned_g", "planned_mm", "planned_s", "slicer_file") if plan.get(k) is not None}
    db.update("jobs", job_id, vals)
    if plan.get("filaments"):
        parts.set_parts(job_id, job["printer_id"], plan["filaments"], trays)
    if job["filament_source"] in AUTO_SOURCES and (plan.get("planned_g") or parts.list_parts(job_id)):
        done = job["status"] == "complete"
        factor = 1.0 if done else (job["progress"] or 0)
        g = _plan_grams(job_id, plan.get("planned_g"), factor)
        db.update("jobs", job_id, {"filament_g": g, "filament_source": "plan" if done else "plan_progress"})
    recalc_job(job_id)


def printer_test(printer, poller=None):
    if printer["kind"] == "moonraker":
        return moonraker_test(printer)
    if printer["kind"] == "bambu":
        if not (printer.get("host") and printer.get("serial") and printer.get("access_code")):
            return {"ok": False, "message": "Укажите IP, серийный номер и код доступа", "parsed": None, "raw": None}
        client = poller.existing_bambu_client(printer["id"]) if poller else None
        res = bambu.test_connection(printer["host"], printer["serial"], printer["access_code"],
                                    printer.get("port") or 8883, client=client)
        ftp = bambu_ftp.test(printer)
        res["message"] += ". " + ftp["message"]
        res["raw"] = {"mqtt_report": res.get("raw"), "ftp_files": ftp["listing"]}
        return res
    return {"ok": False, "message": "неизвестный тип", "parsed": None, "raw": None}


def dumps_safe(obj):
    return json.dumps(db.redact(obj), ensure_ascii=False, default=str)
