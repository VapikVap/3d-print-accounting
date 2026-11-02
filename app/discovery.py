"""Автопоиск принтеров Bambu Lab в локальной сети.

Принтер сам рассылает объявление SSDP (NOTIFY, NT: urn:bambulab-com:device:3dprinter:1)
на 239.255.255.250, порты 2021 и 1990 — так его находит Bambu Studio. В объявлении:
Location — IP, USN — серийный номер, DevModel.bambu.com — код модели (C12 = P1S),
DevName — имя, DevConnect — lan/cloud, DevVersion — прошивка.
Формат — по разбору трафика P1S сообществом (не официальная документация).

Код доступа в объявлении нет и быть не должно: его вводят с экрана принтера.
Порт открывается с общим доступом (SO_REUSEADDR) — Bambu Studio слушает тот же.
"""
import socket
import struct
import threading
import time

from . import bambu_models, db, security

MCAST = "239.255.255.250"
PORTS = (2021, 1990)
FORGET_AFTER = 600.0  # не видели 10 минут — убираем из «найденных»
AUTOADD_LIMIT = (3, 600.0)  # не больше 3 новых принтеров за 10 минут: поток подделок не засорит список
_autoadded = []  # время автодобавлений
NT = "urn:bambulab-com:device:3dprinter:1"


def parse_ssdp(data, sender_ip=""):
    """Объявление принтера Bambu -> dict, чужие SSDP-пакеты -> None."""
    try:
        text = data.decode("utf-8", errors="replace")
    except AttributeError:
        text = str(data)
    lines = [l.strip() for l in text.replace("\r\n", "\n").split("\n") if l.strip()]
    if not lines or not (lines[0].upper().startswith("NOTIFY") or lines[0].upper().startswith("HTTP/1.1 200")):
        return None
    h = {}
    for l in lines[1:]:
        k, sep, v = l.partition(":")
        if sep:
            h[k.strip().lower()] = v.strip()
    kind = h.get("nt") or h.get("st") or ""
    if "bambulab" not in kind.lower() and "devmodel.bambu.com" not in h:
        return None
    serial = h.get("usn", "").strip()
    if serial.lower().startswith("uuid:"):
        serial = serial[5:]
    serial = serial.split("::")[0].strip()
    ip = h.get("location", "").strip() or sender_ip
    ip = ip.replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
    if not serial or not ip:
        return None
    code = h.get("devmodel.bambu.com", "")
    return {
        "serial": serial, "ip": ip, "model_code": code, "model": bambu_models.model_name(code),
        "name": h.get("devname.bambu.com", ""), "connect": h.get("devconnect.bambu.com", ""),
        "bind": h.get("devbind.bambu.com", ""), "version": h.get("devversion.bambu.com", ""),
        "signal": h.get("devsignal.bambu.com", ""),
    }


def short_model(model):
    return (model or "").replace("Bambu Lab ", "")


def ignored():
    return {s for s in (db.get_settings().get("discovery_ignored") or "").split(",") if s}


def set_ignored(serials):
    db.set_settings({"discovery_ignored": ",".join(sorted(serials))})


def register(info, force=False):
    """Найденный принтер -> база: знакомому обновить IP, новый — добавить (если не скрыт
    и включено автодобавление; force — по кнопке пользователя). Возвращает id или None."""
    rows = db.all_rows("printers", "WHERE serial = ?", (info["serial"],))
    if rows:
        p = rows[0]
        vals = {}
        if info["ip"] and p["host"] != info["ip"]:
            if not _same_printer(p, info["ip"]):
                return p["id"]
            vals["host"] = info["ip"]
            db.log_event("info", p["name"], "IP принтера изменился: %s → %s" % (p["host"] or "—", info["ip"]))
        if info["model"] and not p.get("model"):
            vals["model"] = info["model"]
        if vals:
            db.update("printers", p["id"], vals)
        return p["id"]
    if not force and (info["serial"] in ignored() or db.get_settings().get("discovery_autoadd", "1") != "1"):
        return None
    now = time.time()
    _autoadded[:] = [x for x in _autoadded if now - x < AUTOADD_LIMIT[1]]
    if not force and len(_autoadded) >= AUTOADD_LIMIT[0]:
        db.log_event("warn", "поиск", "Слишком много новых принтеров в сети за короткое время — %s (%s) не добавлен "
                     "автоматически. Если он ваш, добавьте в «Устройства → Найдены в сети»." % (info["serial"], info["ip"]))
        return None
    _autoadded.append(now)
    pid = db.insert("printers", {
        "name": info["name"] or short_model(info["model"]) or "Bambu Lab", "model": info["model"],
        "kind": "bambu", "host": info["ip"], "port": 8883, "serial": info["serial"], "enabled": 1,
    })
    db.log_event("info", info["name"] or info["model"],
                 "Найден в сети и добавлен: %s, IP %s, серийный %s. Введите код доступа."
                 % (info["model"], info["ip"], info["serial"]))
    return pid


def _same_printer(p, new_ip):
    """Объявление «принтер теперь на новом IP» мог прислать кто угодно. Если сертификат
    принтера уже запомнен — меняем адрес, только если по новому IP тот же сертификат
    (проверка без логина: код доступа никуда не уходит)."""
    pin = p.get("mqtt_pin")
    if not pin:
        return True  # ещё ни разу не подключались — сравнивать не с чем
    try:
        seen = security.probe(new_ip, p.get("port") or 8883)
    except Exception as e:
        db.log_event("warn", p["name"], "Объявление о новом IP %s не подтверждено (%s) — адрес не меняю" % (new_ip, e))
        return False
    if seen != pin:
        db.log_event("error", p["name"], "Подозрительное объявление в сети: принтер якобы на %s, но сертификат там "
                     "чужой — адрес не меняю, код доступа не отправлен" % new_ip)
        return False
    return True


class Discovery:
    def __init__(self, ports=PORTS, on_found=register):
        self.ports = ports
        self.on_found = on_found
        self.found = {}       # serial -> info + last_seen
        self.errors = {}      # порт -> текст ошибки (например, занят без общего доступа)
        self._socks = []
        self._threads = []
        self._stop = threading.Event()

    def start(self):
        for port in self.ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                try:
                    mreq = struct.pack("4sl", socket.inet_aton(MCAST), socket.INADDR_ANY)
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError:
                    pass  # без мультикаста всё равно получим широковещательные/прямые пакеты
                s.settimeout(1.0)
            except OSError as e:
                self.errors[port] = str(e)
                continue
            self._socks.append(s)
            th = threading.Thread(target=self._loop, args=(s,), name="discovery-%d" % port, daemon=True)
            th.start()
            self._threads.append(th)
        return self

    def stop(self):
        self._stop.set()
        for th in self._threads:
            th.join(timeout=3)  # дождаться, пока поток допишет в базу
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass

    @property
    def port(self):
        """Фактический порт первого сокета (в тестах порт 0 — случайный)."""
        return self._socks[0].getsockname()[1] if self._socks else None

    def _loop(self, s):
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self.handle(data, addr[0])

    def handle(self, data, sender_ip=""):
        info = parse_ssdp(data, sender_ip)
        if not info:
            return None
        prev = self.found.get(info["serial"])
        info["last_seen"] = time.time()
        if (prev is None or prev.get("ip") != info["ip"]) and self.on_found:
            try:
                info["printer_id"] = self.on_found(info)
            except Exception as e:
                db.log_event("error", "поиск", "Ошибка при добавлении найденного принтера: %s" % e)
        else:
            info["printer_id"] = (prev or {}).get("printer_id")
        self.found[info["serial"]] = info  # в списке — только после записи в базу
        return info

    def listing(self):
        now = time.time()
        return [dict(v, age_s=round(now - v["last_seen"])) for v in self.found.values()
                if now - v["last_seen"] < FORGET_AFTER]
