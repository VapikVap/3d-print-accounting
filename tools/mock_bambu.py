"""Эмулятор Bambu Lab P1S в LAN-режиме: MQTT по TLS (8883) + неявный FTPS (990).

Нужен, чтобы прогнать весь путь приложения теми же библиотеками, что пойдут к
живому принтеру (paho-mqtt по TLS, FTPS с переиспользованием TLS-сессии).
Формат отчётов (gcode_state, mc_percent, ams.tray[], vt_tray, tray_now) взят из
открытых описаний LAN-протокола, НЕ снят с вашего P1S — на нём всё равно нужна
«Проверить связь».

Запуск для ручной проверки приложения:
    python tools/mock_bambu.py --auto 60
и в приложении добавить принтер Bambu Lab P1S: IP 127.0.0.1, серийный 01P00MOCK000001,
код доступа 12345678 (порт 8883). Печать двухцветной детали стартует сама.
"""
import argparse
import io
import json
import os
import socket
import ssl
import struct
import tempfile
import threading
import time
import zipfile

SERIAL = "01P00MOCK000001"
ACCESS_CODE = "12345678"
FILE_NAME = "Два цвета"

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/><header_item key="X-BBL-Client-Version" value="02.08.02.61"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="printer_model_id" value="C12"/>
    <metadata key="prediction" value="{secs}"/>
    <metadata key="weight" value="15.70"/>
    <filament id="1" tray_info_idx="GFA00" type="PLA" color="#FFFFFF" used_m="4.21" used_g="12.50" />
    <filament id="2" tray_info_idx="GFA00" type="PLA" color="#000000" used_m="1.07" used_g="3.20" />
  </plate>
</config>
"""


def make_3mf(secs=60):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICE_INFO.replace("{secs}", str(int(secs))))
        z.writestr("Metadata/plate_1.gcode", "; HEADER_BLOCK_START\n; total filament weight [g] : 15.70\n")
    return buf.getvalue()


def make_cert(folder):
    """Самоподписанный сертификат, как у принтера (клиент его не проверяет)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERIAL)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650)).sign(key, hashes.SHA256()))
    cp, kp = os.path.join(folder, "mock.crt"), os.path.join(folder, "mock.key")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return cp, kp


# ======================= состояние принтера =======================

class Printer:
    def __init__(self, print_secs=40):
        self.lock = threading.Lock()
        self.print_secs = print_secs
        self.state = {
            "gcode_state": "IDLE", "mc_percent": 0, "mc_remaining_time": 0, "subtask_name": "",
            "gcode_file": "", "layer_num": 0, "total_layer_num": 0,
            "nozzle_temper": 28.0, "bed_temper": 25.0, "print_error": 0,
            "ams": {"ams": [{"id": "0", "humidity": "5", "humidity_raw": "18", "temp": "26.5", "dry_time": 0, "tray": [
                {"id": "0", "tray_type": "PLA", "tray_sub_brands": "PLA Basic", "tray_color": "FFFFFFFF",
                 "tray_info_idx": "GFA00", "remain": 80},
                {"id": "1", "tray_type": "PLA", "tray_sub_brands": "PLA Basic", "tray_color": "000000FF",
                 "tray_info_idx": "GFA00", "remain": 55},
                {"id": "2", "tray_type": "PETG", "tray_sub_brands": "PETG HF", "tray_color": "C12E1FFF",
                 "tray_info_idx": "GFG02", "remain": 100},
                {"id": "3"},
            ]}], "tray_now": "255", "ams_exist_bits": "1", "tray_exist_bits": "7"},
            "vt_tray": {"id": "254", "tray_type": "", "tray_color": "00000000", "remain": 0},
        }
        self.started = 0.0
        self.files = {"/cache": {}}
        self.changed = []   # частичные изменения для рассылки (как у P1: только изменившееся)
        # состав принтера для get_version: P1S + AMS 2 Pro (у n3f нет product_name —
        # приложение должно узнать тип по имени модуля)
        self.modules = [
            {"name": "ota", "product_name": "Bambu Lab P1S", "sw_ver": "01.08.02.00", "sn": SERIAL},
            {"name": "mc", "sw_ver": "00.00.30.70", "sn": ""},
            {"name": "n3f/0", "sw_ver": "00.00.06.40", "sn": "19F06A2C1500123"},
        ]

    def start_drying(self, minutes=240, temp=65.0):
        with self.lock:
            unit = self.state["ams"]["ams"][0]
            unit.update(dry_time=minutes, temp=str(temp))
            self.changed.append({"ams": {"ams": json.loads(json.dumps(self.state["ams"]["ams"]))}})

    def full(self):
        with self.lock:
            return {"print": dict(json.loads(json.dumps(self.state)), command="push_status", msg=0)}

    def _set(self, **kw):
        self.state.update(kw)
        self.changed.append(dict(kw))

    def start_print(self, name=FILE_NAME, secs=None):
        with self.lock:
            secs = secs or self.print_secs
            self.files["/cache"][name + ".gcode.3mf"] = make_3mf(secs)
            self.started = time.time()
            self.print_secs = secs
            self._set(gcode_state="PREPARE", subtask_name=name, gcode_file="/data/Metadata/plate_1.gcode",
                      mc_percent=0, mc_remaining_time=max(1, secs // 60), total_layer_num=50, layer_num=0)

    def tick(self):
        with self.lock:
            st = self.state["gcode_state"]
            if st not in ("PREPARE", "RUNNING"):
                return
            el = time.time() - self.started
            pct = min(100, int(el / self.print_secs * 100))
            tray = "0" if pct < 70 else "1"  # сначала белый, потом чёрный
            if self.state["ams"]["tray_now"] != tray:
                self.state["ams"]["tray_now"] = tray
                self.changed.append({"ams": {"tray_now": tray}})
            if pct >= 100:
                self._set(gcode_state="FINISH", mc_percent=100, mc_remaining_time=0, layer_num=50,
                          nozzle_temper=60.0, bed_temper=40.0)
                self.state["ams"]["tray_now"] = "255"
            elif pct != self.state["mc_percent"] or st == "PREPARE":
                self._set(gcode_state="RUNNING", mc_percent=pct, layer_num=pct // 2,
                          mc_remaining_time=max(0, int((self.print_secs - el) / 60) + 1),
                          nozzle_temper=220.0, bed_temper=55.0)

    def stop(self, state="FAILED"):
        with self.lock:
            self._set(gcode_state=state, print_error=0x0300800A if state == "FAILED" else 0)
            self.state["ams"]["tray_now"] = "255"

    def take_changes(self):
        with self.lock:
            ch, self.changed = self.changed, []
            return ch


# ======================= MQTT 3.1.1 брокер (минимальный) =======================

def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_packet(sock):
    h = _read_exact(sock, 1)[0]
    mult, length = 1, 0
    while True:
        b = _read_exact(sock, 1)[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return h, _read_exact(sock, length) if length else b""


def _enc_len(n):
    out = b""
    while True:
        b, n = n % 128, n // 128
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _str(b, i):
    n = struct.unpack(">H", b[i:i + 2])[0]
    return b[i + 2:i + 2 + n], i + 2 + n


def _publish_packet(topic, payload):
    t = topic.encode()
    body = struct.pack(">H", len(t)) + t + payload
    return b"\x30" + _enc_len(len(body)) + body


class MqttServer:
    def __init__(self, printer, ctx, host="127.0.0.1", port=8883, serial=SERIAL, code=ACCESS_CODE):
        self.printer, self.ctx, self.serial, self.code = printer, ctx, serial, code
        self.sock = socket.create_server((host, port))
        self.port = self.sock.getsockname()[1]
        self.clients = []
        self.lock = threading.Lock()
        self.connections = 0
        self.pushalls = 0
        self.credentials_seen = 0  # сколько раз нам прислали логин/пароль (для тестов подмены)

    def serve(self):
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(raw,), daemon=True).start()

    def close(self):
        self.sock.close()

    def _send(self, s, data):
        try:
            s.sendall(data)
        except OSError:
            with self.lock:
                if s in self.clients:
                    self.clients.remove(s)

    def _client(self, raw):
        try:
            s = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        report = "device/%s/report" % self.serial
        try:
            h, body = _read_packet(s)
            if h >> 4 != 1:
                return
            self.credentials_seen += 1
            _proto, i = _str(body, 0)
            flags = body[i + 1]
            i += 4  # level, flags, keepalive
            _cid, i = _str(body, i)
            user = pwd = b""
            if flags & 0x80:
                user, i = _str(body, i)
            if flags & 0x40:
                pwd, i = _str(body, i)
            if user != b"bblp" or pwd.decode() != self.code:
                s.sendall(b"\x20\x02\x00\x05")  # 5 = not authorized (неверный код доступа)
                return
            s.sendall(b"\x20\x02\x00\x00")
            self.connections += 1
            while True:
                h, body = _read_packet(s)
                kind = h >> 4
                if kind == 8:  # SUBSCRIBE
                    pid = body[:2]
                    topic, _ = _str(body, 2)
                    ok = topic.decode() == report
                    s.sendall(b"\x90\x03" + pid + (b"\x00" if ok else b"\x80"))
                    if ok:
                        with self.lock:
                            self.clients.append(s)
                elif kind == 3:  # PUBLISH от клиента
                    topic, i = _str(body, 0)
                    if (h >> 1) & 3:
                        i += 2
                    try:
                        msg = json.loads(body[i:])
                    except ValueError:
                        continue
                    if topic.decode() != "device/%s/request" % self.serial:
                        continue
                    if (msg.get("pushing") or {}).get("command") == "pushall":
                        self.pushalls += 1
                        self._send(s, _publish_packet(report, json.dumps(self.printer.full()).encode()))
                    elif (msg.get("info") or {}).get("command") == "get_version":
                        reply = {"info": {"command": "get_version", "sequence_id": msg["info"].get("sequence_id"),
                                          "module": self.printer.modules, "result": "success"}}
                        self._send(s, _publish_packet(report, json.dumps(reply).encode()))
                elif kind == 12:  # PINGREQ
                    s.sendall(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return
        except (ConnectionError, OSError, ssl.SSLError):
            pass
        finally:
            with self.lock:
                if s in self.clients:
                    self.clients.remove(s)
            try:
                s.close()
            except OSError:
                pass

    def _broadcast_loop(self):
        report = "device/%s/report" % self.serial
        while True:
            time.sleep(0.5)
            self.printer.tick()
            for ch in self.printer.take_changes():
                pkt = _publish_packet(report, json.dumps({"print": dict(ch, command="push_status")}).encode())
                with self.lock:
                    clients = list(self.clients)
                for c in clients:
                    self._send(c, pkt)


# ======================= неявный FTPS (минимальный) =======================

class FtpServer:
    def __init__(self, printer, ctx, host="127.0.0.1", port=990, code=ACCESS_CODE):
        self.printer, self.ctx, self.host, self.code = printer, ctx, host, code
        self.sock = socket.create_server((host, port))
        self.port = self.sock.getsockname()[1]
        self.downloads = []
        self.credentials_seen = 0  # сколько раз пришёл USER/PASS

    def serve(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(raw,), daemon=True).start()

    def close(self):
        self.sock.close()

    def _session(self, raw):
        try:
            s = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        f = s.makefile("rb")
        send = lambda line: s.sendall((line + "\r\n").encode())
        send("220 Bambu FTP ready")
        authed, user, pasv = False, "", None
        try:
            while True:
                line = f.readline().decode("utf-8", "replace").strip()
                if not line:
                    return
                cmd, _, arg = line.partition(" ")
                cmd = cmd.upper()
                if cmd == "USER":
                    user = arg
                    self.credentials_seen += 1
                    send("331 Password required")
                elif cmd == "PASS":
                    authed = user == "bblp" and arg == self.code
                    send("230 Login successful" if authed else "530 Login incorrect")
                elif not authed and cmd not in ("QUIT",):
                    send("530 Please login")
                elif cmd in ("PBSZ", "PROT", "TYPE"):
                    send("200 OK")
                elif cmd == "PWD":
                    send('257 "/"')
                elif cmd == "PASV":
                    pasv = socket.create_server((self.host, 0))
                    p = pasv.getsockname()[1]
                    send("227 Entering Passive Mode (%s,%d,%d)" % (self.host.replace(".", ","), p >> 8, p & 255))
                elif cmd in ("NLST", "RETR"):
                    data = self._data(cmd, arg)
                    if data is None:
                        if pasv:
                            pasv.close()
                        send("550 Failed to open")
                        continue
                    send("150 Opening data connection")
                    conn, _ = pasv.accept()
                    try:
                        conn = self.ctx.wrap_socket(conn, server_side=True)
                        conn.sendall(data)
                        conn.unwrap() if hasattr(conn, "unwrap") else None
                    except (ssl.SSLError, OSError):
                        pass
                    finally:
                        conn.close()
                        pasv.close()
                    send("226 Transfer complete")
                elif cmd == "QUIT":
                    send("221 Goodbye")
                    return
                else:
                    send("502 Not implemented")
        except (OSError, ssl.SSLError):
            pass
        finally:
            s.close()

    def _data(self, cmd, arg):
        files = self.printer.files
        if cmd == "NLST":
            d = arg or "/"
            if d == "/":
                return "\r\n".join(["/cache", "/timelapse"]).encode() + b"\r\n"
            if d not in files:
                return None
            return "".join("%s/%s\r\n" % (d, n) for n in files[d]).encode()
        d, name = os.path.split(arg)
        content = files.get(d, {}).get(name)
        if content is not None:
            self.downloads.append(arg)
        return content


def ssdp_notify(ip="127.0.0.1", serial=SERIAL, model="C12", name="P1S Combo"):
    """Объявление в сеть — как у P1S (по разбору трафика сообществом)."""
    return ("NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nServer: UPnP/1.0\r\nLocation: %s\r\n"
            "NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: %s\r\nCache-Control: max-age=1800\r\n"
            "DevModel.bambu.com: %s\r\nDevName.bambu.com: %s\r\nDevSignal.bambu.com: -48\r\n"
            "DevConnect.bambu.com: lan\r\nDevBind.bambu.com: free\r\nDevseclink.bambu.com: secure\r\n"
            "DevVersion.bambu.com: 01.08.02.00\r\nDevCap.bambu.com: 1\r\n\r\n" % (ip, serial, model, name)).encode()


def announce_forever(port, ip="127.0.0.1", every=3.0):
    """Рассылать объявление на 127.0.0.1:port (приложение слушает 2021)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        try:
            s.sendto(ssdp_notify(ip), ("127.0.0.1", port))
        except OSError:
            pass
        time.sleep(every)


def start(host="127.0.0.1", mqtt_port=8883, ftp_port=990, print_secs=40, code=ACCESS_CODE, ssdp_port=None):
    """Запустить эмулятор в фоне. Возвращает (printer, mqtt, ftp)."""
    cert, key = make_cert(tempfile.mkdtemp(prefix="mock-bambu-"))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    printer = Printer(print_secs)
    mqtt = MqttServer(printer, ctx, host, mqtt_port, code=code)
    ftp = FtpServer(printer, ctx, host, ftp_port, code=code)
    for srv in (mqtt, ftp):
        threading.Thread(target=srv.serve, daemon=True).start()
    if ssdp_port:
        threading.Thread(target=announce_forever, args=(ssdp_port, host), daemon=True).start()
    return printer, mqtt, ftp


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mqtt-port", type=int, default=8883)
    ap.add_argument("--ftp-port", type=int, default=990)
    ap.add_argument("--auto", type=float, default=60, help="длительность печати, с (0 — не печатать)")
    ap.add_argument("--pause", type=float, default=20, help="пауза между печатями, с")
    # по умолчанию не объявляемся: на 2021 слушает рабочее приложение — эмулятор попал бы в вашу базу
    ap.add_argument("--ssdp-port", type=int, default=0, help="порт для объявления на 127.0.0.1 (0 — не слать)")
    a = ap.parse_args()
    printer, mqtt, ftp = start(mqtt_port=a.mqtt_port, ftp_port=a.ftp_port, print_secs=a.auto or 60,
                               ssdp_port=a.ssdp_port)
    print("Эмулятор P1S: MQTT 127.0.0.1:%d, FTPS 127.0.0.1:%d, серийный %s, код %s"
          % (mqtt.port, ftp.port, SERIAL, ACCESS_CODE))
    n = 0
    while True:
        time.sleep(a.pause)
        if a.auto:
            n += 1
            printer.start_print("%s %d" % (FILE_NAME, n), a.auto)
            time.sleep(a.auto + 2)


if __name__ == "__main__":
    main()
