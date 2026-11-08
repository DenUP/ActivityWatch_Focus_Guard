@echo off
cd /d "%~dp0"
py -3 focus_guard.py
if errorlevel 1 pause
