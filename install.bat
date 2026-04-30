@echo off
title SnapMark — Install Dependencies
echo.
echo  SnapMark — Installing dependencies...
echo  ─────────────────────────────────────
echo.

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found.
    echo  Please install Python 3.10+ from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo  Python found. Installing packages...
echo.
pip install PyQt6 keyboard mss
echo.
echo  ─────────────────────────────────────
echo  [OK] All done!
echo.
echo  To run SnapMark:
echo    python screenshot_app.py
echo.
pause
