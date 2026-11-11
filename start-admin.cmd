@echo off
cd /d "%~dp0"
powershell -Command "Start-Process 'pythonw' -ArgumentList '\"%~dp0main.py\"' -Verb RunAs"