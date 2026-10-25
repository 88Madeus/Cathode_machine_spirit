@echo off
cd /d "%~dp0"
python -c "import pyvisa, pyvisa_py, matplotlib, openpyxl" 2>nul || python -m pip install -r requirements.txt
python pr8_cathode_app.py
if errorlevel 1 pause
