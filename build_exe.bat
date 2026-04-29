@echo off
REM ============================================================
REM  Build ABB Crush Tester Data Reader as a standalone .exe
REM ============================================================
REM  Run this on a machine that has Python 3.8+ installed.
REM  It will:
REM    1. Install PyInstaller and matplotlib (if not already)
REM    2. Bundle crush_reader.py into a single .exe
REM    3. Place the result in the "dist" subfolder
REM
REM  Then copy dist\CrushReader.exe to the lab computer.
REM ============================================================

echo.
echo === ABB Crush Tester Data Reader - Build Script ===
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Make sure Python is installed and on your PATH.
    echo Download from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
pip install pyinstaller matplotlib --upgrade --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

echo [2/3] Building executable (this takes 1-2 minutes)...
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name CrushReader ^
    --noconfirm ^
    --clean ^
    crush_reader.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed. See messages above.
    pause
    exit /b 1
)

echo [3/3] Done!
echo.
echo ============================================================
echo   Your executable is ready:
echo   %CD%\dist\CrushReader.exe
echo.
echo   Copy this single file to the lab computer and run it.
echo   No Python or other software needed on the lab machine.
echo ============================================================
echo.

REM Open the dist folder
explorer dist

pause
