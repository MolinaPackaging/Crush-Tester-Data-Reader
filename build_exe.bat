@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Build ABB Crush Tester Data Reader as a standalone .exe
REM ============================================================
REM  Builds inside a clean, throwaway virtual environment that
REM  contains ONLY matplotlib + pyinstaller, and does the actual
REM  PyInstaller work in a LOCAL temp folder (not the OneDrive-
REM  synced project folder). OneDrive locks files mid-write, which
REM  makes PyInstaller's --clean fail with "Access is denied", so
REM  we build in %TEMP% and copy just the finished .exe back.
REM
REM  Result: dist\CrushReader.exe  -> copy to the lab computer.
REM ============================================================

echo.
echo === ABB Crush Tester Data Reader - Build Script ===
echo.

REM --- Find a working Python for THIS window --------------------------
REM  Double-clicking the script opens a fresh cmd.exe that does NOT have
REM  Anaconda/conda activated, so "python" may be missing even though it
REM  works in your terminal. Try the common launchers in order.
set "PYCMD="
for %%P in ("python" "py -3" "python3") do (
    if not defined PYCMD (
        %%~P --version >nul 2>&1 && set "PYCMD=%%~P"
    )
)
if not defined PYCMD (
    echo ERROR: Python was not found on PATH for this window.
    echo.
    echo If you use Anaconda/Miniconda, this is expected when you
    echo double-click the script - conda is not activated in a fresh
    echo window. Open "Anaconda Prompt" ^(or run "conda activate base"^)
    echo and then run this script from that terminal:
    echo.
    echo        build_exe.bat
    echo.
    echo Otherwise install Python from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)
echo Using Python: !PYCMD!
echo.

REM --- Clean, isolated build environment ------------------------------
REM  We use virtualenv rather than "python -m venv": Anaconda's base Python
REM  often ships a broken/stripped ensurepip, which makes "venv" fail while
REM  bootstrapping pip. virtualenv carries its own pip and just works.
set "VENV=.build_venv"
set "VPY=%VENV%\Scripts\python.exe"
REM Guard on pip.exe (not just python.exe) so a half-built env is rebuilt.
if not exist "%VENV%\Scripts\pip.exe" (
    echo [1/4] Creating clean build environment ^(first run only, ~30s^)...
    if exist "%VENV%" rmdir /s /q "%VENV%"
    !PYCMD! -m pip install --upgrade virtualenv --quiet
    !PYCMD! -m virtualenv "%VENV%"
    if errorlevel 1 (
        echo ERROR: Could not create the build environment "%VENV%".
        echo Try running:  !PYCMD! -m pip install virtualenv
        echo then run this script again.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Reusing clean build environment "%VENV%".
)

echo [2/4] Installing dependencies into the clean environment...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install pyinstaller matplotlib --upgrade --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

echo [3/4] Building executable (this takes 1-2 minutes)...
REM  Build in a LOCAL temp folder so OneDrive can't lock the work files.
set "BUILDTMP=%TEMP%\CrushReaderBuild"
if exist "%BUILDTMP%" rmdir /s /q "%BUILDTMP%" 2>nul
mkdir "%BUILDTMP%" 2>nul
"%VPY%" -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name CrushReader ^
    --noconfirm ^
    --clean ^
    --workpath "%BUILDTMP%\build" ^
    --distpath "%BUILDTMP%\dist" ^
    --specpath "%BUILDTMP%" ^
    --icon="%CD%\corrugated_crush_icon_assets\corrugated_crush_icon.ico" ^
    --add-data "%CD%\corrugated_crush_icon_assets\corrugated_crush_icon.ico;." ^
    --exclude-module PyQt5 ^
    --exclude-module PyQt6 ^
    --exclude-module PySide2 ^
    --exclude-module PySide6 ^
    --exclude-module pandas ^
    --exclude-module scipy ^
    --exclude-module IPython ^
    --exclude-module notebook ^
    --exclude-module pytest ^
    "%CD%\crush_reader.py"

if errorlevel 1 (
    echo.
    echo ERROR: Build failed. See messages above.
    pause
    exit /b 1
)

REM Copy the finished exe out of temp into the project's dist\ folder.
if not exist dist mkdir dist
copy /y "%BUILDTMP%\dist\CrushReader.exe" "dist\CrushReader.exe" >nul
if errorlevel 1 (
    echo.
    echo ERROR: Built the exe but could not copy it into dist\.
    echo   The finished file is here: %BUILDTMP%\dist\CrushReader.exe
    echo   ^(If OneDrive is syncing, pause it and copy that file manually.^)
    pause
    exit /b 1
)
REM Best-effort cleanup of temp + any stale project build folder.
rmdir /s /q "%BUILDTMP%" 2>nul
if exist build rmdir /s /q build 2>nul

echo [4/4] Done!
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
