"""Файл нарезки с принтера Bambu Lab по FTPS (LAN).

При отправке печати из Bambu Studio файл .gcode.3mf уходит прямо на принтер —
на компьютере его нет. Принтер отдаёт свои файлы по неявному FTPS: порт 990,
пользователь bblp, пароль = код доступа. Так же делает интеграция ha-bambulab.

НЕ ПРОВЕРЕНО на вашем P1S: в каких папках лежат файлы (корень SD, /cache, /model)
и обязательна ли SD-карта. Поэтому ищем в нескольких местах, а «Проверить связь»
показывает, что реально лежит на принтере.
"""
import ftplib
import os
import re
import ssl
import time

from . import db, slicer

PORT = 990
# Bambu Studio для P1/A1 указывает ftp_folder = "sdcard/" (resources/printers/C12.json)
SEARCH_DIRS = ("/", "/cache", "/model", "/sdcard", "/sdcard/cache")
EXTS = (".gcode.3mf", ".3mf")
CACHE_DIR = os.path.join(os.path.dirname(db.DB_PATH), "printer_files")  # рядом с базой
KEEP_FILES = 60


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS умеет только явный TLS (AUTH TLS); Bambu — неявный, сразу TLS.
    Канал данных должен переиспользовать TLS-сессию управляющего канала (vsftpd)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def _context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # сертификат принтера самоподписанный, только LAN
    return ctx


def connect(host, access_code, timeout=10):
    ftp = ImplicitFTP_TLS(context=_context(), timeout=timeout)
    ftp.connect(host, PORT, timeout=timeout)
    ftp.login("bblp", access_code)
    ftp.prot_p()
    return ftp


def list_files(ftp):
    """{папка: [имена .3mf]} по SEARCH_DIRS; недоступные папки пропускаются."""
    out = {}
    for d in SEARCH_DIRS:
        try:
            names = ftp.nlst(d)
        except ftplib.all_errors:
            continue
        out[d] = sorted({os.path.basename(n.rstrip("/")) for n in names
                         if n.lower().endswith(EXTS)})
    return out


def find_remote(listing, job_name):
    """Путь к файлу задания на принтере или None. Сопоставление — как у папки слайсера."""
    files = [{"name": n, "dir": d} for d, names in listing.items() for n in names]
    m = slicer.match(None, job_name, files=files)
    if not m:
        return None
    return (m["dir"].rstrip("/") + "/" + m["name"]) if m["dir"] != "/" else "/" + m["name"]


def plate_from_gcode_file(gcode_file):
    """'/data/Metadata/plate_2.gcode' -> 2 (какая пластина проекта печатается)."""
    m = re.search(r"plate_(\d+)\.gcode", gcode_file or "")
    return int(m.group(1)) if m else None


def plan_from_info(info, plate=None):
    """План из разобранного файла: нужная пластина, иначе итог по файлу."""
    plates = info.get("plates") or []
    p = next((x for x in plates if x.get("index") == plate), None) if plate else None
    if p is None and len(plates) == 1:
        p = plates[0]
    src = p or info
    return {"planned_g": src.get("filament_g"), "planned_mm": src.get("filament_mm"),
            "planned_s": src.get("est_time_s"), "filaments": src.get("filaments") or []}


def _local_path(serial, remote):
    d = os.path.join(CACHE_DIR, re.sub(r"[^\w.-]", "_", serial or "printer"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, os.path.basename(remote))


def _cleanup():
    files = []
    for root, _dirs, names in os.walk(CACHE_DIR):
        files += [os.path.join(root, n) for n in names]
    files.sort(key=os.path.getmtime, reverse=True)
    for f in files[KEEP_FILES:]:
        try:
            os.remove(f)
        except OSError:
            pass


def fetch_plan(printer, job_name, gcode_file="", connect_fn=None):
    """Скачать файл задания с принтера и вернуть (plan, local_path, сообщение).
    plan = None, если файл не найден/не скачан."""
    connect_fn = connect_fn or connect
    try:
        ftp = connect_fn(printer["host"], printer["access_code"])
    except Exception as e:
        return None, None, "FTP: нет подключения (%s)" % e
    try:
        listing = list_files(ftp)
        remote = find_remote(listing, job_name)
        if not remote:
            seen = sum(len(v) for v in listing.values())
            return None, None, "FTP: файл «%s» не найден на принтере (файлов .3mf: %d)" % (job_name, seen)
        local = _local_path(printer.get("serial"), remote)
        tmp = local + ".part"
        with open(tmp, "wb") as f:
            ftp.retrbinary("RETR " + remote, f.write)
        os.replace(tmp, local)
    except Exception as e:
        return None, None, "FTP: ошибка загрузки (%s)" % e
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    _cleanup()
    try:
        info = slicer.parse_file(local)
    except Exception as e:
        return None, local, "Файл скачан, но не разобран: %s" % e
    plan = plan_from_info(info, plate_from_gcode_file(gcode_file))
    plan["slicer_file"] = local
    return plan, local, "План с принтера: %s" % remote


def test(printer, connect_fn=None):
    """Для «Проверить связь»: подключение и список файлов на принтере."""
    t0 = time.time()
    connect_fn = connect_fn or connect
    try:
        ftp = connect_fn(printer["host"], printer["access_code"])
    except Exception as e:
        return {"ok": False, "message": "FTPS (порт 990): нет подключения — %s" % e, "listing": None}
    try:
        listing = list_files(ftp)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    n = sum(len(v) for v in listing.values())
    where = ", ".join("%s: %d" % (d, len(v)) for d, v in listing.items()) or "папки не открылись"
    return {"ok": True, "message": "FTPS работает за %.1f с, файлов .3mf — %d (%s)" % (time.time() - t0, n, where),
            "listing": listing}
