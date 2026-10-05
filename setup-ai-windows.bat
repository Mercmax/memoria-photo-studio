@echo off
cd /d "%~dp0"
py -3.11 setup_ai.py
if errorlevel 1 echo Setup failed. Install Python 3.11 and update the NVIDIA driver.
pause
