@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Running tests...
py -3 -m unittest -v
if errorlevel 1 goto error

echo [2/4] Installing the Windows packager...
py -3 -m pip install --disable-pip-version-check "pyinstaller==6.22.3" "pystray==0.19.5" "Pillow>=10,<13"
if errorlevel 1 goto error

echo [3/4] Building standalone Windows EXE...
py -3 -m PyInstaller --onefile --windowed --clean --hidden-import=pystray._win32 --name ActivityWatchFocusGuard focus_guard.py
if errorlevel 1 goto error

echo [4/4] Packing Chrome and Edge extension...
powershell -NoProfile -Command "Compress-Archive -Path 'browser-extension\*' -DestinationPath 'dist\FocusGuardBrowserExtension.zip' -Force"
if errorlevel 1 goto error

powershell -NoProfile -Command "$names = @('ActivityWatchFocusGuard.exe', 'FocusGuardBrowserExtension.zip'); $lines = foreach ($name in $names) { $hash = (Get-FileHash ('dist\' + $name) -Algorithm SHA256).Hash.ToLowerInvariant(); $hash + '  ' + $name }; Set-Content -Path 'dist\SHA256SUMS.txt' -Value $lines -Encoding Ascii"
if errorlevel 1 goto error

echo.
echo Build succeeded. Upload these THREE files to GitHub Releases:
echo   dist\ActivityWatchFocusGuard.exe
echo   dist\FocusGuardBrowserExtension.zip
echo   dist\SHA256SUMS.txt
pause
exit /b 0

:error
echo.
echo Build failed. Read the error above; nothing was published.
pause
exit /b 1
