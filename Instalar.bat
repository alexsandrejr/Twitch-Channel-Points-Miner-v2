@echo off
rem Instala o Python portatil, as bibliotecas e os atalhos. Pode ser executado de novo para reparar.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0painel\instalar.ps1"
if errorlevel 1 pause
