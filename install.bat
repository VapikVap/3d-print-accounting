@echo off
rem 3D print accounting installer. The cmd part is ASCII only; the PowerShell part
rem below the marker is read as UTF-8 and executed. Safe to run again = update.
rem No chcp here on purpose: switching cmd to UTF-8 breaks how it seeks in this file.
set "P3D_INSTALLER=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:P3D_INSTALLER,[Text.Encoding]::UTF8); iex $t.Substring($t.LastIndexOf('#PS-START'))"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
"%SystemRoot%\System32\timeout.exe" /t 5 >nul
exit /b 0

#PS-START
# ===== Установщик «Учёт 3D-печати» =====
# Сам скачивает приложение с GitHub (если запущен отдельно), ставит Python и WebView2
# при необходимости, библиотеки, ярлыки — и запускает. Повторный запуск = обновление;
# папка data (ваша база) и .venv не трогаются.
# Для проверки: P3D_CHECK=1 — только показать, что найдено, ничего не менять;
# P3D_DIR — папка установки; P3D_ZIP — взять приложение из этого zip, а не с GitHub;
# P3D_TEST=1 — всё поставить, но без ярлыков и запуска.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # иначе скачивание в PowerShell 5 очень медленное
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo      = 'VapikVap/3d-print-accounting'
$AppName   = 'Учёт 3D-печати'
$PyVersion = '3.12.10'
$PyUrl     = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe"
$WebView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
$Check     = $env:P3D_CHECK -eq '1'
$Log       = Join-Path $env:TEMP '3d-print-accounting-install.log'
"=== $(Get-Date) ===" | Out-File $Log -Append -Encoding utf8

function Say([string]$m, [string]$color = 'Gray') { Write-Host $m -ForegroundColor $color; $m | Out-File $Log -Append -Encoding utf8 }
function Step([string]$m) { Say ''; Say "▶ $m" 'Cyan' }
function Ok([string]$m) { Say "  ✓ $m" 'Green' }
function Download([string]$url, [string]$out) {
    Say "  скачиваю: $url"
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -Headers @{ 'User-Agent' = '3d-print-accounting-installer' }
}

function Test-Python([string]$exe, [string[]]$pre = @()) {
    # путь к python.exe версии 3.10+ или $null; заглушка Microsoft Store не пройдёт
    try {
        $out = & $exe @pre -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $ver, $path = "$out".Trim().Split('|')
        if ([version]$ver -ge [version]'3.10') { return $path }
    } catch {}
    return $null
}

function Find-Python {
    $found = Test-Python 'py' @('-3')
    if ($found) { return $found }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') {
        $found = Test-Python $cmd.Source
        if ($found) { return $found }
    }
    $globs = @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
               "$env:LOCALAPPDATA\Python\pythoncore-3*\python.exe",
               "$env:ProgramFiles\Python3*\python.exe")
    foreach ($g in $globs) {
        foreach ($f in (Get-ChildItem $g -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            $found = Test-Python $f.FullName
            if ($found) { return $found }
        }
    }
    return $null
}

function Test-WebView2 {
    $id = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($k in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$id",
                     "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id",
                     "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$id")) {
        $pv = (Get-ItemProperty $k -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne '0.0.0.0') { return $pv }
    }
    return $null
}

function Get-AppVersion([string]$dir) {
    $f = Join-Path $dir 'app\__init__.py'
    if (-not (Test-Path $f)) { return $null }
    $m = Select-String -Path $f -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return $null
}

function Stop-App([string]$dir) {
    # обновлять файлы, пока приложение работает, нельзя — закрываем только эту копию
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$dir*desktop.py*" }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($procs) { Start-Sleep -Seconds 2; Ok 'приложение закрыто для обновления (задания сохранены в базе)' }
}

try {
    Say "Установка «$AppName»" 'White'
    if ($Check) { Say '(режим проверки: ничего не скачиваю и не меняю)' 'Yellow' }

    # ---------- 1. Приложение ----------
    Step 'Приложение'
    $here = if ($env:P3D_INSTALLER) { Split-Path -Parent $env:P3D_INSTALLER } else { '' }
    if ($env:P3D_DIR) { $AppDir = $env:P3D_DIR }
    elseif ($here -and (Test-Path (Join-Path $here 'app\server.py'))) { $AppDir = $here }
    else { $AppDir = Join-Path $env:LOCALAPPDATA 'Programs\3d-print-accounting' }
    $local = $here -and ($AppDir -eq $here) -and -not $env:P3D_ZIP
    Say "  папка: $AppDir"
    $current = Get-AppVersion $AppDir

    if ($local) {
        Ok "использую файлы рядом с установщиком (версия $current)"
    } else {
        $tag = $null
        if ($env:P3D_ZIP) {
            $zip = $env:P3D_ZIP
        } else {
            $rels = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases?per_page=10" -UseBasicParsing `
                -Headers @{ 'User-Agent' = '3d-print-accounting-installer' }
            $rel = @($rels | Where-Object { -not $_.draft })[0]
            if (-not $rel) { throw 'На GitHub нет опубликованных версий' }
            $tag = $rel.tag_name
            $zip = Join-Path $env:TEMP "3d-print-accounting-$tag.zip"
        }
        $latest = if ($tag) { $tag.TrimStart('v') } else { 'из файла' }
        if ($current -and $tag -and $current -eq $latest) {
            Ok "уже установлена последняя версия $current"
        } elseif ($Check) {
            Say "  будет установлена версия $latest (сейчас: $(if ($current) { $current } else { 'нет' }))" 'Yellow'
        } else {
            if ($tag) { Download "https://github.com/$Repo/archive/refs/tags/$tag.zip" $zip }
            $tmp = Join-Path $env:TEMP ("3dpa-" + [guid]::NewGuid().ToString('N'))
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $src = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
            if (-not (Test-Path (Join-Path $src 'app\server.py'))) { throw 'В архиве нет приложения' }
            if ($current) { Stop-App $AppDir }
            New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
            # копируем поверх, кроме ваших данных и окружения
            & robocopy $src $AppDir /E /XD data .venv /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "Не удалось скопировать файлы (robocopy $LASTEXITCODE)" }
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            if ($tag) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
            Ok "установлена версия $(Get-AppVersion $AppDir)$(if ($current) { " (была $current)" })"
        }
    }

    # ---------- 2. Python ----------
    Step 'Python 3.10+'
    $venvPy = Join-Path $AppDir '.venv\Scripts\python.exe'
    $py = $null
    if (Test-Python $venvPy) {
        Ok 'окружение приложения уже готово'
    } else {
        $py = Find-Python
        if ($py) { Ok "найден: $py" }
        elseif ($Check) { Say "  не найден — будет установлен Python $PyVersion (только для вас, без прав администратора)" 'Yellow' }
        else {
            Say "  не найден — ставлю Python $PyVersion (только для вас, без прав администратора)"
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                & winget install --id Python.Python.3.12 -e --scope user --silent --disable-interactivity `
                    --accept-package-agreements --accept-source-agreements | Out-File $Log -Append -Encoding utf8
                $py = Find-Python
            }
            if (-not $py) {
                $exe = Join-Path $env:TEMP "python-$PyVersion-amd64.exe"
                Download $PyUrl $exe
                Start-Process -FilePath $exe -Wait -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
                    'Include_launcher=1', 'Include_test=0', 'Shortcuts=0'
                Remove-Item $exe -Force -ErrorAction SilentlyContinue
                $py = Find-Python
            }
            if (-not $py) { throw 'Python не установился. Поставьте его вручную с python.org (галочка «Add Python to PATH») и запустите установщик снова.' }
            Ok "установлен: $py"
        }
    }

    # ---------- 3. WebView2 (окно приложения) ----------
    Step 'Microsoft Edge WebView2'
    $wv = Test-WebView2
    if ($wv) { Ok "есть, версия $wv" }
    elseif ($Check) { Say '  нет — будет установлен с сайта Microsoft' 'Yellow' }
    else {
        $exe = Join-Path $env:TEMP 'MicrosoftEdgeWebview2Setup.exe'
        Download $WebView2Url $exe
        Start-Process -FilePath $exe -Wait -ArgumentList '/silent', '/install'
        Remove-Item $exe -Force -ErrorAction SilentlyContinue
        if (Test-WebView2) { Ok 'установлен' } else { Say '  не удалось проверить установку — если окно не откроется, поставьте WebView2 с сайта Microsoft' 'Yellow' }
    }

    if ($Check) { Step 'Проверка закончена'; exit 0 }

    # ---------- 4. Библиотеки ----------
    Step 'Библиотеки (пару минут при первой установке)'
    if (-not (Test-Python $venvPy)) {
        & $py -m venv (Join-Path $AppDir '.venv') 2>&1 | Out-File $Log -Append -Encoding utf8
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python' }
    }
    & $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $AppDir 'requirements.txt') 2>&1 |
        Out-File $Log -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить библиотеки (нужен интернет). Подробности в журнале.' }
    Ok 'готово'

    if ($env:P3D_TEST -eq '1') { Step 'Тестовый режим: без ярлыков и запуска'; exit 0 }

    # ---------- 5. Ярлыки и запуск ----------
    Step 'Ярлыки и запуск'
    & $venvPy (Join-Path $AppDir 'tools\create_shortcuts.py') | Out-File $Log -Append -Encoding utf8
    Ok 'ярлык «Учёт 3D-печати» на рабочем столе и в меню «Пуск»'
    Start-Process -FilePath (Join-Path $AppDir '.venv\Scripts\pythonw.exe') `
        -ArgumentList ('"' + (Join-Path $AppDir 'app\desktop.py') + '"') -WorkingDirectory $AppDir
    Say ''
    Say "Готово! «$AppName» запускается. Дальше — по ярлыку на рабочем столе." 'Green'
    exit 0
} catch {
    Say ''
    Say "✗ Ошибка: $($_.Exception.Message)" 'Red'
    Say "Журнал установки: $Log"
    Say "Если не получается — напишите: https://github.com/$Repo/issues/new/choose"
    exit 1
}
