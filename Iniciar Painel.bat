@echo off
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\TwitchMinerPainel\python\pythonw.exe"
rem Ambiente de desenvolvimento (.venv) tem preferencia quando existir
if exist ".venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
  call "%~dp0Instalar.bat"
  exit /b
)
start "" "%PYW%" "painel\app.py"
