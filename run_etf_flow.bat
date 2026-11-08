@echo off
REM ETF flow page data (units increase / foreign / trust top 5) + push. Double-click to update by hand.
REM The scheduled run_market_corr.bat (06:30 / 18:30) also runs etf_flow.py, so this is only for manual updates.
chcp 65001 >nul
setlocal
set "REPO=%~dp0"
set "PYEXE=python"
cd /d "%REPO%" || exit /b 1

echo [%date% %time%] etf_flow start
"%PYEXE%" etf_flow.py --out "data\etf_flow.json"
if errorlevel 1 echo [warn] etf_flow failed, keeping previous etf_flow.json

set /a LOCKWAIT=0
:waitlock
if exist ".git\index.lock" if %LOCKWAIT% lss 30 (
    set /a LOCKWAIT+=1
    timeout /t 5 /nobreak >nul
    goto waitlock
)

git add etf_flow.py etf_flow.html index.html etf.html run_market_corr.bat run_etf_flow.bat data\etf_flow.json
git diff --cached --quiet && (
    echo [info] no change, skip commit
) || (
    git commit -m "etf flow %date%"
    git pull --rebase --autostash
    git push
    if errorlevel 1 echo [warn] push failed, data written locally
)
echo [%date% %time%] etf_flow done
endlocal
pause
