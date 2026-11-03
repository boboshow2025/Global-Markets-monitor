@echo off
REM Register the 18:30 weekday task for run_market_corr.bat (double-click once)
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_schedule_evening.ps1"
pause
