@echo off
cd /d "%~dp0"
"%~dp0AgentWorkbenchCLI.exe" collect --config "%~dp0data\collector.json" --outbox "%~dp0data\outbox.db"
if errorlevel 1 pause
