@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Running tests...
py -3 -m unittest -v
if errorlevel 1 goto error

echo [2/3] Installing the Windows packager...
py -3 -m pip install --disable-pip-version-check "pyinstaller==6.22.3" "pystray==0.19.5" "Pillow>=10,<13"
if errorlevel 1 goto error

echo [3/3] Building standalone Windows EXE...
py -3 -m PyInstaller --onefile --windowed --clean --add-data "web;web" --hidden-import=pystray._win32 --name ActivityWatchFocusGuard focus_guard.py
if errorlevel 1 goto error

powershell -NoProfile -Command "$name = 'ActivityWatchFocusGuard.exe'; $hash = (Get-FileHash ('dist\' + $name) -Algorithm SHA256).Hash.ToLowerInvariant(); Set-Content -Path 'dist\SHA256SUMS.txt' -Value ($hash + '  ' + $name) -Encoding Ascii"
if errorlevel 1 goto error

echo.
echo Build succeeded. Upload these TWO files to GitHub Releases:
echo   dist\ActivityWatchFocusGuard.exe
echo   dist\SHA256SUMS.txt
pause
exit /b 0

:error
echo.
echo Build failed. Read the error above; nothing was published.
pause
exit /b 1
