@echo off
cd /d "%~dp0"
echo In the browser, open Device and Settings and generate a pairing code.
set /p AWB_CODE=Enter the 9-digit code: 
"%~dp0AgentWorkbenchCLI.exe" pair http://127.0.0.1:8765 %AWB_CODE% --config "%~dp0data\collector.json"
if errorlevel 1 goto end
if exist "%USERPROFILE%\.codex\sessions" "%~dp0AgentWorkbenchCLI.exe" add-source codex "%USERPROFILE%\.codex\sessions" --policy stats_only --config "%~dp0data\collector.json"
if exist "%LOCALAPPDATA%\hermes\state.db" "%~dp0AgentWorkbenchCLI.exe" add-source hermes "%LOCALAPPDATA%\hermes\state.db" --policy stats_only --config "%~dp0data\collector.json"
echo.
echo Register the source IDs printed above in Device and Settings before starting collection.
:end
pause
