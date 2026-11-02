"""Запуск вместе с Windows: значение в HKCU\\...\\Run (только для текущего пользователя,
права администратора не нужны). Приложение стартует сразу в трей (--tray)."""
import os
import sys

from . import db

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "Учёт 3D-печати"


def command():
    pythonw = os.path.join(db.BASE_DIR, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return '"%s" "%s" --tray' % (pythonw, os.path.join(db.BASE_DIR, "app", "desktop.py"))


def is_enabled():
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, NAME)[0] == command()
    except OSError:
        return False


def apply(enabled):
    """Включить/выключить автозапуск. Возвращает текст ошибки или ''."""
    if os.name != "nt":
        return "только для Windows"
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(k, NAME)
                except FileNotFoundError:
                    pass
        return ""
    except OSError as e:
        return str(e)
