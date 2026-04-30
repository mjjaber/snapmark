@echo off
title SnapMark — Build Standalone EXE
echo.
echo  SnapMark — Building standalone .exe...
echo  ──────────────────────────────────────
echo.

:: Install/upgrade PyInstaller
pip install --upgrade pyinstaller PyQt6 keyboard mss

echo.
echo  Compiling...
echo.

pyinstaller ^
  --onefile ^
  --windowed ^
  --name "SnapMark" ^
  --add-data "." ^
  screenshot_app.py

echo.
if exist "dist\SnapMark.exe" (
    echo  ──────────────────────────────────────
    echo  [OK] Build successful!
    echo.
    echo  Your executable is at:
    echo    dist\SnapMark.exe
    echo.
    echo  You can move SnapMark.exe anywhere and run it directly.
    echo  No Python installation needed on the target machine.
) else (
    echo  [ERROR] Build failed. Check the output above for details.
)
echo.
pause
