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

REM 3) ETF flow (units increase / foreign / trust top lists, all listed+OTC ETFs)
"%PYEXE%" etf_flow.py --out "data\etf_flow.json"
if errorlevel 1 echo [warn] etf_flow failed, keeping previous etf_flow.json

REM 4) data\rotation.json is written every evening by ETF-Tracker2 (Etf_tracker_v2.py)

REM 18:30 the stock-screener daily job also commits to this repo: wait (max ~2.5 min) if git is busy
set /a LOCKWAIT=0
:waitlock
if exist ".git\index.lock" if %LOCKWAIT% lss 30 (
    set /a LOCKWAIT+=1
    timeout /t 5 /nobreak >nul
    goto waitlock
)

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
