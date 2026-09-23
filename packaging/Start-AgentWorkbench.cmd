@echo off
cd /d "%~dp0"
"%~dp0AgentWorkbench.exe" open --db "%~dp0data\agent-workbench.db"
if errorlevel 1 pause
