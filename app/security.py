"""Защита от злоумышленника в локальной сети.

1. Привязка к сертификату принтера (TOFU — «доверие при первом подключении»).
   Сертификат Bambu самоподписанный, обычную проверку TLS не пройти. Поэтому при первом
   успешном входе с кодом доступа запоминаем отпечаток (SHA-256) сертификата, а дальше
   сверяем его СРАЗУ после TLS-рукопожатия — до того, как по соединению уйдёт код.
   Не совпал — соединение рвётся: возможна подмена принтера.
2. Шифрование секретов в базе через Windows DPAPI: расшифровать может только эта
   учётная запись Windows на этом компьютере.
"""
import base64
import hashlib
import os
import socket
import ssl

PIN_MISMATCH = "Сертификат принтера не совпадает с запомненным — возможна подмена принтера в сети"


class PinMismatch(ssl.SSLError):
    pass


def fingerprint(der):
    return hashlib.sha256(der).hexdigest()


class PinnedContext(ssl.SSLContext):
    """TLS-контекст без проверки цепочки (сертификат самоподписанный), но с проверкой
    отпечатка. expected=None — первое подключение: отпечаток только запоминается в seen."""

    def __new__(cls, expected=None):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, expected=None):  # протокол уже задан в __new__
        self.check_hostname = False
        self.verify_mode = ssl.CERT_NONE
        self.expected = (expected or "").lower() or None
        self.seen = None

    def wrap_socket(self, sock, *args, **kwargs):
        s = super().wrap_socket(sock, *args, **kwargs)
        try:
            s.do_handshake()  # рукопожатие сейчас, чтобы сверить сертификат до любых данных
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            s.setblocking(True)
            s.do_handshake()
        der = s.getpeercert(binary_form=True)
        self.seen = fingerprint(der) if der else None
        if self.expected and self.seen != self.expected:
            try:
                s.close()
            finally:
                raise PinMismatch(PIN_MISMATCH)
        return s


def probe(host, port, timeout=5.0):
    """Отпечаток сертификата на host:port — только рукопожатие, без логина и данных."""
    ctx = PinnedContext()
    with socket.create_connection((host, int(port)), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host):
            return ctx.seen


# ======================= DPAPI =======================

PREFIX = "dpapi:"
_ENTROPY = b"3d-print-accounting/secrets/v1"


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, _keep1 = blob(data)
    ent, _keep2 = blob(_ENTROPY)
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))  # 0x1: без окон
    if not ok:
        raise OSError("DPAPI: %d" % ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def available():
    return os.name == "nt"


def encrypt(value):
    """Секрет -> 'dpapi:…'. Пустое остаётся пустым; не Windows — как есть."""
    if not value or not available() or str(value).startswith(PREFIX):
        return value
    return PREFIX + base64.b64encode(_dpapi(str(value).encode("utf-8"), True)).decode("ascii")


def decrypt(value):
    """'dpapi:…' -> секрет. Не удалось (база с другого компьютера/пользователя) -> ''."""
    if not value or not str(value).startswith(PREFIX):
        return value or ""
    try:
        return _dpapi(base64.b64decode(value[len(PREFIX):]), False).decode("utf-8")
    except Exception:
        return ""
