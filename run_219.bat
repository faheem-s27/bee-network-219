@echo off
rem Double-click to launch the 219 bus sign with no console window.
rem It kicks off everything: live feed polling, the delay model, the timetable
rem (auto-refreshing), accuracy logging, and self-calibration.
cd /d "%~dp0"
start "" pythonw "bus_sign.py"
