@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo ============================================================
echo   Slack_AIbot 시작
echo ============================================================
".venv\Scripts\python.exe" -m src.main
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [오류] 봇이 종료되었습니다. 위 안내 또는 logs\bot.log 를 확인하세요.
    pause
)
