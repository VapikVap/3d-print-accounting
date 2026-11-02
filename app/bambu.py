"""Bambu Lab в LAN-режиме: MQTT over TLS, порт 8883, пользователь bblp,
пароль = код доступа, отчёты в топике device/{serial}/report.

Принтер шлёт полный отчёт только в ответ на pushall, дальше — частичные
изменения (особенно P1/A1), поэтому состояние накапливается слиянием.
Сертификат принтера самоподписанный, поэтому цепочка не проверяется, но отпечаток
сертификата запоминается при первом входе и сверяется до отправки кода (security.py).
"""
import json
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from . import security
from .slicer import normalize_color

# gcode_state -> общий статус приложения
STATE_MAP = {
    "RUNNING": "printing",
    "PREPARE": "printing",
    "SLICING": "printing",
    "PAUSE": "paused",
    "FINISH": "complete",
    "FAILED": "error",
    "IDLE": "standby",
}
ACTIVE = ("printing", "paused")
PUSHALL_MIN_INTERVAL = 300.0  # с


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _tray(label, index, t):
    material = t.get("tray_type") or ""
    remain = t.get("remain")
    try:
        remain = int(remain)
    except (TypeError, ValueError):
        remain = None
    return {"tray": label, "index": index, "material": material,
            "name": t.get("tray_sub_brands") or "",
            "color_hex": normalize_color(t.get("tray_color")) if material else "",  # у пустого слота 00000000
            "remain": remain if remain is not None and remain >= 0 else None,  # -1 = неизвестно
            "empty": not material}


# Модули из ответа get_version: префикс имени -> тип системы подачи.
# По открытым разборам протокола (ha-bambulab), не по документации Bambu: n3f — AMS 2 Pro,
# n3s — AMS HT, ams_f1 — AMS lite. Если принтер отдаёт product_name — верим ему.
MODULE_TYPES = (("n3f/", "AMS 2 Pro"), ("n3s/", "AMS HT"), ("ams_f1/", "AMS lite"), ("ams/", "AMS"))


def parse_modules(modules):
    """[{name, product_name, sw_ver, sn}, ...] -> {'firmware', 'product', 'ams': {id: {type, sw_ver}}}."""
    out = {"firmware": "", "product": "", "ams": {}}
    for m in modules or []:
        name = str(m.get("name") or "")
        if name == "ota":
            out["firmware"] = m.get("sw_ver") or ""
            out["product"] = m.get("product_name") or ""
        for prefix, kind in MODULE_TYPES:
            if name.startswith(prefix):
                try:
                    uid = int(name.split("/", 1)[1])
                except (ValueError, IndexError):
                    uid = 0
                out["ams"][uid] = {"type": m.get("product_name") or kind, "sw_ver": m.get("sw_ver") or ""}
                break
    return out


def _unit_label(uid):
    """0..3 -> A..D; AMS HT (id 128+) -> HT1.."""
    return "HT%d" % (uid - 127) if uid >= 128 else "ABCD"[uid % 4]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_ams(p, modules=None):
    """Блоки AMS и слоты (A1..D4, HT1, EXT) из отчёта.
    tray_now: номер активного слота (блок*4+слот), 254 — внешняя, 255 — нет."""
    trays, units = [], []
    ams = p.get("ams") if isinstance(p.get("ams"), dict) else {}
    types = parse_modules(modules)["ams"]
    for unit in ams.get("ams") or []:
        try:
            uid = int(unit.get("id", 0))
        except (TypeError, ValueError):
            continue
        label = _unit_label(uid)
        kind = (types.get(uid) or {}).get("type") or ("AMS HT" if uid >= 128 else "AMS")
        dry = _num(unit.get("dry_time"))
        units.append({
            "id": uid, "label": label, "type": kind, "detected": uid in types,
            "humidity_pct": _num(unit.get("humidity_raw")),   # % (новые прошивки / AMS 2 Pro)
            "humidity_level": _num(unit.get("humidity")),     # уровень 1..5 (старый AMS)
            "temp": _num(unit.get("temp")),
            "drying_min": dry if dry and dry > 0 else None,   # осталось сушить, мин
        })
        for t in unit.get("tray") or []:
            try:
                tid = int(t.get("id", 0))
            except (TypeError, ValueError):
                continue
            tray = _tray(label if uid >= 128 else label + str(tid + 1), uid if uid >= 128 else uid * 4 + tid, t)
            tray["unit"] = label
            trays.append(tray)
    if isinstance(p.get("vt_tray"), dict):
        trays.append(dict(_tray("EXT", 254, p["vt_tray"]), unit="EXT"))
    now = str(ams.get("tray_now", ""))
    active = next((t["tray"] for t in trays if str(t["index"]) == now), None)
    return trays, active, units


def interpret(print_state, modules=None):
    """Сырой словарь 'print' (+ модули из get_version) -> общий статус."""
    p = print_state or {}
    gstate = p.get("gcode_state")
    pct = p.get("mc_percent")
    rem = p.get("mc_remaining_time")  # минуты
    name = p.get("subtask_name") or p.get("gcode_file") or ""
    trays, active_tray, units = parse_ams(p, modules)
    mod = parse_modules(modules)
    return {
        "trays": trays,
        "active_tray": active_tray,
        "ams_units": units,
        "firmware": mod["firmware"],
        "raw_state": gstate,
        "state": STATE_MAP.get(str(gstate).upper(), str(gstate).lower() if gstate else None),
        "filename": name,
        "gcode_file": p.get("gcode_file") or "",  # путь внутри 3mf: .../plate_N.gcode
        "progress": (float(pct) / 100.0) if pct is not None else None,
        "remaining_s": float(rem) * 60 if rem is not None else None,
        "layer": p.get("layer_num"),
        "total_layers": p.get("total_layer_num"),
        "nozzle_temp": p.get("nozzle_temper"),
        "bed_temp": p.get("bed_temper"),
        "task_id": p.get("task_id") or p.get("job_id") or "",
        "print_error": p.get("print_error"),
    }


class BambuClient:
    def __init__(self, host, serial, access_code, port=8883, pin=None, on_pin=None):
        self.host, self.serial, self.port = host, serial, int(port or 8883)
        self._code = access_code
        self._ctx = security.PinnedContext(pin)
        self._on_pin = on_pin          # сохранить отпечаток после первого входа с кодом
        self.pin_mismatch = False
        self.state = {}
        self.last_raw = None          # последний сырой отчёт (без секретов — их там нет)
        self.last_report_ts = 0.0
        self.connected = False
        self.error = ""
        self._last_pushall = 0.0
        self.modules = None           # состав принтера из get_version (тип AMS, прошивки)
        self.last_version = None      # сырой ответ get_version — для «Проверить связь»
        self._lock = threading.Lock()
        self._client = None

    @property
    def topic_report(self):
        return "device/%s/report" % self.serial

    @property
    def topic_request(self):
        return "device/%s/request" % self.serial

    def start(self):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="print-acc-%s-%d" % (self.serial[-6:], int(time.time())))
        c.username_pw_set("bblp", self._code)
        c.tls_set_context(self._ctx)  # сверка отпечатка — до того, как уйдёт код доступа
        c.on_connect = self._on_connect
        c.on_connect_fail = self._on_connect_fail
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=5, max_delay=60)
        self._client = c
        try:
            c.connect_async(self.host, self.port, keepalive=30)
            c.loop_start()
        except Exception as e:
            self.error = "Не удалось подключиться: %s" % e

    def stop(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
        self.connected = False

    def request_full(self, force=False):
        """Запрос полного отчёта. Частый pushall для P1/A1 не рекомендуется —
        без force не чаще раза в PUSHALL_MIN_INTERVAL."""
        now = time.time()
        if not force and now - self._last_pushall < PUSHALL_MIN_INTERVAL:
            return False
        if self._client and self.connected:
            self._last_pushall = now
            self._client.publish(self.topic_request, json.dumps(
                {"pushing": {"sequence_id": str(int(time.time())), "command": "pushall"}}))
            return True
        return False

    def request_version(self):
        """Состав принтера: какие модули (AMS / AMS 2 Pro / AMS HT) и версии прошивок."""
        if self._client and self.connected:
            self._client.publish(self.topic_request, json.dumps(
                {"info": {"sequence_id": str(int(time.time())), "command": "get_version"}}))

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or getattr(reason_code, "value", 1) == 0:
            self.connected = True
            self.error = ""
            self.pin_mismatch = False
            if not self._ctx.expected and self._ctx.seen:
                # первый вход с верным кодом: запоминаем сертификат этого принтера
                self._ctx.expected = self._ctx.seen
                if self._on_pin:
                    self._on_pin(self._ctx.seen)
            client.subscribe(self.topic_report)
            self.request_full(force=True)  # после (пере)подключения состояние надо собрать заново
            self.request_version()
        else:
            self.connected = False
            # 134/135 — неверный логин/пароль (код доступа) или нет LAN-режима
            self.error = "MQTT отказ: %s" % reason_code

    def _on_connect_fail(self, client, userdata):
        if self._ctx.expected and self._ctx.seen and self._ctx.seen != self._ctx.expected:
            self.pin_mismatch = True
            self.connected = False
            self.error = security.PIN_MISMATCH

    @property
    def pin(self):
        return self._ctx.expected

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        if reason_code and str(reason_code) != "Success":
            self.error = "MQTT отключение: %s" % reason_code

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except ValueError:
            return
        with self._lock:
            self.last_raw = payload
            if isinstance(payload.get("print"), dict):
                deep_merge(self.state, payload["print"])
                self.last_report_ts = time.time()
            info = payload.get("info")
            if isinstance(info, dict) and info.get("command") == "get_version":
                self.modules = info.get("module") or []
                self.last_version = payload

    def status(self):
        with self._lock:
            st = interpret(self.state, self.modules)
        st["connected"] = self.connected
        st["pin_mismatch"] = self.pin_mismatch
        st["age_s"] = time.time() - self.last_report_ts if self.last_report_ts else None
        st["error"] = self.error
        return st

    def snapshot_raw(self):
        with self._lock:
            return json.loads(json.dumps(self.last_raw)) if self.last_raw else None


def test_connection(host, serial, access_code, port=8883, wait_s=10, client=None, pin=None):
    """Проверка связи: ждём первый отчёт. Если уже есть рабочий клиент — используем его
    (у Bambu ограничено число одновременных MQTT-подключений)."""
    own = client is None
    if own:
        client = BambuClient(host, serial, access_code, port, pin=pin)
        client.start()
    else:
        client.request_full()
    t0 = time.time()
    try:
        if not own:
            client.request_version()
        while time.time() - t0 < wait_s:
            if client.last_report_ts and client.state.get("gcode_state") is not None \
                    and (client.modules is not None or time.time() - t0 > 4):
                break  # ответ на get_version ждём недолго: старые прошивки могут молчать
            if client.error and ("отказ" in client.error or client.pin_mismatch):
                break
            time.sleep(0.2)
        st = client.status()
        with client._lock:
            raw = json.loads(json.dumps({"report": {"print": client.state}, "get_version": client.last_version}))
        if client.pin_mismatch:
            return {"ok": False, "message": security.PIN_MISMATCH + ". Код доступа НЕ отправлен. Если вы обновили "
                    "прошивку или заменили принтер — нажмите «Доверять заново».", "parsed": st, "raw": None,
                    "pin_mismatch": True}
        if client.last_report_ts:
            msg = "Отчёт получен, gcode_state=%s" % st["raw_state"]
            if st.get("firmware"):
                msg += ", прошивка %s" % st["firmware"]
            if st["ams_units"]:
                msg += ". Подача: " + ", ".join(
                    "%s — %s%s" % (u["label"], u["type"], "" if u["detected"] else " (тип не подтверждён принтером)")
                    for u in st["ams_units"])
            else:
                msg += ". AMS не обнаружен"
            return {"ok": True, "message": msg, "parsed": st, "raw": raw, "mqtt_pin": client.pin}
        if client.error:
            return {"ok": False, "message": client.error +
                    ". Проверьте код доступа и что на принтере включён режим LAN.",
                    "parsed": st, "raw": raw}
        if client.connected:
            return {"ok": False, "message": "Подключение есть, но отчётов нет за %d с. "
                    "Проверьте серийный номер (топик %s)." % (wait_s, client.topic_report),
                    "parsed": st, "raw": raw}
        return {"ok": False, "message": "Нет подключения к %s:%s за %d с (порт закрыт или неверный IP)."
                % (host, port, wait_s), "parsed": st, "raw": raw}
    finally:
        if own:
            client.stop()
