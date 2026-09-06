@echo off
chcp 65001 >nul
setlocal
set "REPO=%~dp0"
set "PYEXE=python"
set "AUTO_PUSH=1"

cd /d "%REPO%" || exit /b 1

echo [%date% %time%] start
"%PYEXE%" market_corr.py --out "data\correlations.json"
if errorlevel 1 (
    echo [error] data generation failed, keeping previous json
    exit /b 1
)

if "%AUTO_PUSH%"=="1" (
    git add -A
    git diff --cached --quiet && (
        echo [info] no change, skip commit
    ) || (
        git commit -m "chore: update %date%"
        git push
        if errorlevel 1 echo [warn] push failed, data written locally
    )
)

echo [%date% %time%] done
endlocal
