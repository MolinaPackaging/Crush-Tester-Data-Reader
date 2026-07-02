@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Build the FTP Diagnostic tool as a standalone .exe
REM ============================================================
REM  Builds inside a clean, throwaway virtual environment that
REM  contains ONLY pyinstaller, so the build is reproducible and
REM  immune to whatever unrelated packages live in your base env.
REM
REM  Result: dist\FTPDiagnostic.exe -> copy to the lab computer.
REM  No Python required on the lab machine.
REM ============================================================

echo.
echo === ABB Crush Tester FTP Diagnostic - Build Script ===
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
    echo        build_diagnostic.bat
    echo.
    echo Otherwise install Python from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)
echo Using Python: !PYCMD!
echo.

REM --- Clean, isolated build environment (shared with build_exe) ------
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
"%VPY%" -m pip install pyinstaller --upgrade --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

echo [3/4] Building executable (this takes about a minute)...
"%VPY%" -m PyInstaller ^
    --onefile ^
    --console ^
    --name FTPDiagnostic ^
    --noconfirm ^
    --clean ^
    ftp_diagnostic.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed. See messages above.
    pause
    exit /b 1
)

echo [4/4] Done!
echo.
echo ============================================================
echo   Your executable is ready:
echo   %CD%\dist\FTPDiagnostic.exe
echo.
echo   Copy this single file to the lab computer and run it
echo   from a Command Prompt:
echo.
echo     FTPDiagnostic.exe                  (interactive menu)
echo     FTPDiagnostic.exe --quick          (one probe per strategy)
echo     FTPDiagnostic.exe --run-all        (full matrix)
echo.
echo   Logs:     ftp_diagnostic_logs\       (TSV per run)
echo   Payloads: ftp_diagnostic_payloads\   (one file per probe)
echo ============================================================
echo.

REM Open the dist folder
explorer dist

pause
