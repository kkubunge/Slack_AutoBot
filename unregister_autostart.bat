@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ============================================================
echo   Slack_AIbot Windows 로그인 자동 시작 해제
echo ============================================================
schtasks /Delete /TN "Slack_AIbot_Controller" /F
pause
