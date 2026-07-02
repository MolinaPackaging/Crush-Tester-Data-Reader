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
REM  Result: dist\CrushReader_v<version>.exe  -> copy to the lab computer.
REM  The version in the name comes from __version__ in crush_reader.py.
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

REM --- Make the base interpreter's DLLs visible to PyInstaller ------------
REM  With Anaconda/conda Python, binary dependencies of C extensions
REM  (ffi-8.dll for _ctypes, and friends) live in <base>\Library\bin,
REM  which is only on PATH inside an "Anaconda Prompt". If the build runs
REM  from a plain window, PyInstaller's dependency scan can't find those
REM  DLLs, SILENTLY leaves them out, and the exe dies on launch with
REM  "DLL load failed while importing _ctypes". Putting the folder on
REM  PATH here makes the build correct no matter how it was started.
set "PYBASE="
for /f "delims=" %%B in ('%VPY% -c "import sys;print(sys.base_prefix)"') do set "PYBASE=%%B"
if defined PYBASE if exist "%PYBASE%\Library\bin" (
    echo Using base interpreter DLLs from: %PYBASE%\Library\bin
    set "PATH=%PYBASE%\Library\bin;%PATH%"
)

REM --- Read the app version so the exe name says which build it is --------
REM  Pulls the X.Y.Z out of the  __version__ = "X.Y.Z"  line, so the exe is
REM  named e.g. CrushReader_v4.0.0.exe and can't be confused with older ones.
set "VERSION="
for /f tokens^=2^ delims^=^" %%V in ('findstr /b /c:"__version__" crush_reader.py') do set "VERSION=%%V"
if defined VERSION (
    set "EXENAME=CrushReader_v%VERSION%"
) else (
    echo WARNING: Could not read __version__ from crush_reader.py.
    set "EXENAME=CrushReader"
)
echo Building %EXENAME%.exe
echo.

echo [3/4] Building executable (this takes 1-2 minutes)...
REM  Build in a LOCAL temp folder so OneDrive can't lock the work files.
set "BUILDTMP=%TEMP%\CrushReaderBuild"
if exist "%BUILDTMP%" rmdir /s /q "%BUILDTMP%" 2>nul
mkdir "%BUILDTMP%" 2>nul
"%VPY%" -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name %EXENAME% ^
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

REM --- Sanity check: the ffi DLL must be inside the bundle ----------------
REM  Every Python build ships an ffi DLL next to _ctypes (libffi-8.dll for
REM  python.org, ffi-8.dll in conda's Library\bin). If it's absent from the
REM  archive, the exe WILL crash on startup, so fail loudly now instead of
REM  shipping a broken file to the lab computer.
%VPY% -m PyInstaller.utils.cliutils.archive_viewer -l "%BUILDTMP%\dist\%EXENAME%.exe" | findstr /i "ffi" >nul
if errorlevel 1 (
    echo.
    echo ERROR: The built exe is missing the ffi DLL that _ctypes needs.
    echo   It would crash on launch with "DLL load failed while importing
    echo   _ctypes". This usually means the Python DLL folder was not on
    echo   PATH during the build. Not copying the broken exe to dist\.
    pause
    exit /b 1
)

REM Copy the finished exe out of temp into the project's dist\ folder.
if not exist dist mkdir dist
copy /y "%BUILDTMP%\dist\%EXENAME%.exe" "dist\%EXENAME%.exe" >nul
if errorlevel 1 (
    echo.
    echo ERROR: Built the exe but could not copy it into dist\.
    echo   The finished file is here: %BUILDTMP%\dist\%EXENAME%.exe
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
echo   %CD%\dist\%EXENAME%.exe
echo.
echo   Copy this single file to the lab computer and run it.
echo   No Python or other software needed on the lab machine.
echo ============================================================
echo.

REM Open the dist folder
explorer dist

pause
