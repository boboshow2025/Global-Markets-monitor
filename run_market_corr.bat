@echo off
chcp 65001 >nul
setlocal
set "REPO=%~dp0"
set "PYEXE=python"
set "AUTO_PUSH=1"

cd /d "%REPO%" || exit /b 1

echo [%date% %time%] start

REM 1) pre-open signal
"%PYEXE%" market_corr.py --out "data\correlations.json"
if errorlevel 1 echo [warn] market_corr failed, keeping previous correlations.json

REM 2) panic gauge (VIX / foreign futures short / margin)
"%PYEXE%" panic_gauge.py --out "data\panic.json"
if errorlevel 1 echo [warn] panic_gauge failed, keeping previous panic.json

REM 3) data\rotation.json is written every evening by ETF-Tracker2 (Etf_tracker_v2.py)

if "%AUTO_PUSH%"=="1" (
    git add -A
    git diff --cached --quiet && (
        echo [info] no change, skip commit
    ) || (
        git commit -m "chore: update %date%"
        git pull --rebase --autostash
        git push
        if errorlevel 1 echo [warn] push failed, data written locally
    )
)

echo [%date% %time%] done
endlocal
