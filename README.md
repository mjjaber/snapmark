# SnapMark 📸

Lightweight Windows screenshot + annotation tool.  
Runs in the system tray. Press **Ctrl+F1** anywhere to capture.

---

## Quick Start

### 1. Install Python (one time)
Download Python 3.10+ from [python.org](https://www.python.org/downloads/)  
✅ Check **"Add Python to PATH"** during install.

### 2. Install dependencies
Double-click **`install.bat`**

### 3. Run
```
python screenshot_app.py
```
SnapMark will appear in your system tray (bottom-right corner).

---

## How to use

| Action | How |
|--------|-----|
| Take a screenshot | **Ctrl+F1** (anywhere on screen) |
| Select area | Click & drag over what you want |
| Cancel selection | **Esc** |
| Draw an arrow | Click **↗ Arrow**, then drag on the screenshot |
| Draw a rectangle | Click **□ Rectangle**, then drag |
| Change color | Click the color swatch |
| Change stroke size | Adjust the Size spinner |
| Undo last shape | **↩ Undo** or **Ctrl+Z** |
| Copy to clipboard | **📋 Copy** or **Ctrl+C** |
| Save as PNG/JPG | **💾 Save as…** or **Ctrl+S** |

---

## Build a standalone .exe (optional)

Want to send SnapMark to someone without Python? Run:
```
build_exe.bat
```
This produces `dist\SnapMark.exe` — a single file that runs on any Windows 10/11 machine with no dependencies.

---

## Tech stack

| | |
|--|--|
| Language | Python 3 |
| UI framework | PyQt6 (Qt 6) |
| Screen capture | mss |
| Global hotkey | keyboard |
| Packaging | PyInstaller |
