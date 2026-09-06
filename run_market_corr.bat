@echo off
chcp 65001 >nul
setlocal

REM ==========================================================
REM  Global-Markets-monitor 傳導鏈資料更新
REM  排程建議：週二～週六 07:30（台北時間），涵蓋前一夜美股收盤
REM ==========================================================

REM ---- 依實際路徑修改這三行 ----
set "REPO=C:\Users\User\Desktop\ETF\Global-Markets-monitor"
set "PYEXE=python"
set "AUTO_PUSH=0"
REM ------------------------------

cd /d "%REPO%" || (echo [error] 找不到專案目錄 %REPO% & exit /b 1)

echo [%date% %time%] 開始更新傳導鏈資料
"%PYEXE%" market_corr.py --out "data\correlations.json"
if errorlevel 1 (
    echo [error] 資料產生失敗，保留上一版 correlations.json
    exit /b 1
)

if "%AUTO_PUSH%"=="1" (
    git add data/correlations.json
    git diff --cached --quiet && (
        echo [info] 資料沒有變動，略過 commit
    ) || (
        git commit -m "chore: update correlations %date%"
        git push
        if errorlevel 1 echo [warn] push 失敗，資料仍已寫入本機
    )
)

echo [%date% %time%] 完成
endlocal
