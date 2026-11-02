"""Ярлыки «Учёт 3D-печати» на рабочем столе и в меню «Пуск».

Запуск через pythonw.exe — без консольного окна. Команда PowerShell передаётся
как -EncodedCommand (UTF-16), чтобы не ломалась кириллица в путях.
"""
import base64
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "Учёт 3D-печати"


def ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def _decode(b):
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp866", "replace")


def main(remove=False):
    pythonw = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
    if not remove and not os.path.exists(pythonw):
        sys.exit("Нет .venv — сначала запустите start.bat")
    script = os.path.join(BASE, "app", "desktop.py")
    icon = os.path.join(BASE, "app", "static", "icon.ico")
    lines = ["$ErrorActionPreference = 'Stop'", "$ProgressPreference = 'SilentlyContinue'", "$sh = New-Object -ComObject WScript.Shell"]
    for folder in ("Desktop", "Programs"):
        lines.append("$p = Join-Path ([Environment]::GetFolderPath(%s)) %s"
                     % (ps_quote(folder), ps_quote(NAME + ".lnk")))
        if remove:
            lines.append("if (Test-Path $p) { Remove-Item $p }")
            continue
        lines += [
            "$l = $sh.CreateShortcut($p)",
            "$l.TargetPath = %s" % ps_quote(pythonw),
            "$l.Arguments = %s" % ps_quote('"%s"' % script),
            "$l.WorkingDirectory = %s" % ps_quote(BASE),
            "$l.IconLocation = %s" % ps_quote(icon + ",0"),
            "$l.Description = %s" % ps_quote("Учёт расхода и себестоимости 3D-печати"),
            "$l.Save()",
            "Write-Output $p",
        ]
    enc = base64.b64encode("\n".join(lines).encode("utf-16-le")).decode()
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         capture_output=True)
    text = "".join(_decode(b) for b in (out.stdout, out.stderr))
    if out.returncode:
        sys.exit("Не удалось: " + text)
    print(text.strip() or "готово")


if __name__ == "__main__":
    main(remove="--remove" in sys.argv)
