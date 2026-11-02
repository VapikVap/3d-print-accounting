"""Настольное приложение: своё окно (WebView2) + значок в трее.

Закрытие окна прячет его в трей — опрос принтеров продолжается, иначе
задание, идущее в этот момент, не будет учтено. Полный выход — «Выход» в меню трея.
Повторный запуск не создаёт вторую копию, а показывает уже открытое окно.
Внутри по-прежнему локальный сервер на 127.0.0.1 — наружу он не виден.
"""
import os
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from app import db, export, server

TITLE = "Учёт 3D-печати"
THEME_BG = {"violet": "#1a0b33", "light": "#f4f1fa", "black": "#000000", "graphite": "#1c1d21", "system": "#1a0b33"}
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
URL = "http://%s:%d/" % (server.HOST, server.PORT)


def _redirect_output():
    """У pythonw нет консоли: пишем вывод в data/app.log, чтобы ошибки не терялись."""
    if sys.stdout is None or sys.stderr is None:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        log = open(os.path.join(db.DATA_DIR, "app.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _message_box(text):
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)


def _already_running():
    """Если приложение уже запущено — показать его окно и вернуть True."""
    try:
        r = requests.get(URL + "api/app/ping", timeout=1.5)
        if r.ok and r.json().get("app") == server.APP_ID:
            requests.post(URL + "api/app/show", timeout=3, headers={"Origin": URL.rstrip("/")})
            return True
    except Exception:
        return False
    _message_box("Порт %d занят другой программой. Закройте её и запустите снова." % server.PORT)
    return True


class Api:
    """Функции, доступные интерфейсу через window.pywebview.api."""

    def __init__(self):
        # с подчёркиванием: pywebview не обходит такие поля (иначе рекурсия по .NET-объектам окна)
        self._window = None

    def save_export(self, date_from=None, date_to=None):
        import webview

        def ts(v):
            return time.mktime(time.strptime(v, "%Y-%m-%d")) if v else None
        name = "Учёт печати %s.xlsx" % time.strftime("%Y-%m-%d")
        save = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
        path = self._window.create_file_dialog(save, save_filename=name, file_types=("Excel (*.xlsx)",))
        if not path:
            return None
        path = path if isinstance(path, str) else path[0]
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        to = ts(date_to)
        with open(path, "wb") as f:
            f.write(export.build(ts(date_from), to + 86400 if to else None))
        return path

    def pick_folder(self, start=""):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind, directory=start if start and os.path.isdir(start) else "")
        return (res if isinstance(res, str) else res[0]) if res else None

    def pick_exe(self):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        res = self._window.create_file_dialog(kind, file_types=("Программы (*.exe)",))
        return (res if isinstance(res, str) else res[0]) if res else None

    def open_url(self, url):
        """Ссылки (GitHub) — в обычном браузере, а не внутри окна приложения."""
        if url.startswith("https://github.com/"):
            import webbrowser
            webbrowser.open(url)

    def open_folder(self, path):
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        os.startfile(folder)


class Tray:
    def __init__(self, on_open, on_quit):
        import pystray
        from PIL import Image
        self._notified = False
        menu = pystray.Menu(pystray.MenuItem("Открыть", lambda: on_open(), default=True),
                            pystray.MenuItem("Выход", lambda: on_quit()))
        self.icon = pystray.Icon("print-accounting", Image.open(os.path.join(STATIC, "icon.png")), TITLE, menu)

    def start(self):
        self.icon.run_detached()

    def notify_hidden(self):
        if self._notified:
            return
        self._notified = True
        try:
            self.icon.notify("Окно свёрнуто в трей, учёт печати продолжается. "
                             "Выход — правой кнопкой по значку.", TITLE)
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


def _set_app_id():
    """Свой значок и группа на панели задач, а не значок pythonw.exe."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PrintAccounting.Desktop")
    except Exception:
        pass


def _set_window_icon(window):
    try:
        from System.Drawing import Icon
        form = window.native
        ico = Icon(os.path.join(STATIC, "icon.ico"))
        if form.InvokeRequired:
            from System import Action
            form.BeginInvoke(Action(lambda: setattr(form, "Icon", ico)))  # без ожидания UI-потока
        else:
            form.Icon = ico
    except Exception as e:
        print("иконка окна:", e)


def main():
    _redirect_output()
    if _already_running():
        return
    _set_app_id()
    import webview

    try:
        srv = server.start_background()
    except OSError as e:
        _message_box("Не удалось запустить: %s" % e)
        return

    settings = db.get_settings()
    # автозагрузку приложение само не прописывает — только по галочке в «Настройках»
    to_tray = "--tray" in sys.argv  # из автозагрузки Windows — сразу в трей, без окна
    api = Api()
    window = webview.create_window(TITLE, URL, js_api=api, width=1320, height=860, min_size=(900, 600),
                                   background_color=THEME_BG.get(settings.get("theme"), "#1a0b33"),
                                   text_select=True, hidden=to_tray)
    api._window = window
    state = {"quitting": False}

    def show():
        window.show()
        window.restore()

    def quit_app():
        state["quitting"] = True
        tray.stop()
        server.poller.stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()
        window.destroy()

    def on_closing():
        if state["quitting"]:
            return True
        window.hide()
        tray.notify_hidden()
        return False  # отменить закрытие — остаёмся в трее

    tray = Tray(show, quit_app)
    server.on_show = show
    window.events.closing += on_closing
    window.events.shown += lambda: _set_window_icon(window)
    tray.start()
    webview.start(private_mode=False, storage_path=os.path.join(db.DATA_DIR, "webview"),
                  icon=os.path.join(STATIC, "icon.ico"))
    # окно закрыто (через «Выход») — завершаем всё
    if not state["quitting"]:
        tray.stop()
        server.poller.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
