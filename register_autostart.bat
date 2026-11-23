@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ============================================================
echo   Slack_AIbot Windows 로그인 자동 시작 등록
echo ============================================================
schtasks /Create /SC ONLOGON /TN "Slack_AIbot_Controller" /TR "\"D:\99_MyPJT\GoogleAI_PJT\Slack_AIbot\run_bot.bat\"" /F
schtasks /Query /TN "Slack_AIbot_Controller" /V /FO LIST
pause
