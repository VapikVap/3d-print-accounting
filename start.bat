@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Создаю окружение .venv ...
    python -m venv .venv || (echo Не найден Python. Установите Python 3.10+ с python.org & pause & exit /b 1)
)
echo Проверяю зависимости ...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (echo Ошибка установки зависимостей & pause & exit /b 1)
if not exist "%USERPROFILE%\Desktop\Учёт 3D-печати.lnk" ".venv\Scripts\python.exe" tools\create_shortcuts.py
start "" ".venv\Scripts\pythonw.exe" app\desktop.py
