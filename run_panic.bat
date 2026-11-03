@echo off
REM Panic gauge only (VIX / foreign futures short / margin / maintenance ratio) + push.
REM Called at the end of stock-screener\run_margin.bat (~22:00), after per-stock margin data is in prices.db,
REM so the maintenance ratio shows the same day instead of waiting for the next morning run.
chcp 65001 >nul
setlocal
set "REPO=%~dp0"
set "PYEXE=python"
cd /d "%REPO%" || exit /b 1

echo [%date% %time%] panic start
"%PYEXE%" panic_gauge.py --out "data\panic.json"
if errorlevel 1 echo [warn] panic_gauge failed, keeping previous panic.json

set /a LOCKWAIT=0
:waitlock
if exist ".git\index.lock" if %LOCKWAIT% lss 30 (
    set /a LOCKWAIT+=1
    timeout /t 5 /nobreak >nul
    goto waitlock
)

git add data\panic.json data\panic
git diff --cached --quiet && (
    echo [info] panic.json no change, skip commit
) || (
    git commit -m "chore: panic gauge %date%"
    git pull --rebase --autostash
    git push
    if errorlevel 1 echo [warn] push failed, data written locally
)
echo [%date% %time%] panic done
endlocal
