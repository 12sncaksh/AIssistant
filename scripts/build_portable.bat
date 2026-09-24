@echo off
setlocal
cd /d "%~dp0.."

echo [1/6] Checking required wake-word assets...
if not exist "generated\kws\encoder-epoch-12-avg-2-chunk-16-left-64.onnx" goto missing_assets
if not exist "generated\kws\decoder-epoch-12-avg-2-chunk-16-left-64.onnx" goto missing_assets
if not exist "generated\kws\joiner-epoch-12-avg-2-chunk-16-left-64.onnx" goto missing_assets
if not exist "generated\kws\tokens.txt" goto missing_assets
if not exist "generated\kws\keywords.txt" goto missing_assets

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

echo [2/6] Installing build dependencies into the selected Python...
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 goto build_failed
%PYTHON% -m pip install pyinstaller
if errorlevel 1 goto build_failed

echo [3/6] Building the portable application...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
%PYTHON% -m PyInstaller --noconfirm --clean scripts\Aissist.spec
if errorlevel 1 goto build_failed
if not exist "dist\Aissist\Aissist.exe" goto build_failed

echo [4/6] Copying application, Live2D and wake-word resources...
if not exist "dist\Aissist\assets" mkdir "dist\Aissist\assets"
copy /y "assets\Aissist.ico" "dist\Aissist\assets\Aissist.ico" >nul
if errorlevel 1 goto packaged_icon_missing
if not exist "dist\Aissist\assets\Aissist.ico" goto packaged_icon_missing
if exist "dist\Aissist\assets\web_resources" rmdir /s /q "dist\Aissist\assets\web_resources"
if exist "dist\Aissist\generated\kws" rmdir /s /q "dist\Aissist\generated\kws"
xcopy /e /i /y "assets\web_resources" "dist\Aissist\assets\web_resources" >nul
xcopy /e /i /y "generated\kws" "dist\Aissist\generated\kws" >nul
rem assets\web_resources is the local HTTP root: drop any leftover chat-history DB so it is never shipped
del /s /q "dist\Aissist\assets\web_resources\chat_history.db" >nul 2>nul
if exist "dist\Aissist\assets\web_resources\chat_history.db" goto packaged_db_leak
if exist "dist\Aissist\assets\web_resources\dist\chat_history.db" goto packaged_db_leak
if not exist "dist\Aissist\assets\web_resources\dist\pet.html" goto packaged_assets_missing
if not exist "dist\Aissist\generated\kws\encoder-epoch-12-avg-2-chunk-16-left-64.onnx" goto packaged_assets_missing

echo [5/6] Adding user documentation and configuration templates...
if not exist "dist\Aissist\config\templates" mkdir "dist\Aissist\config\templates"
copy /y "docs\README_PORTABLE.md" "dist\Aissist\README.md" >nul
copy /y "config\templates\auth.example.json" "dist\Aissist\config\templates\auth.example.json" >nul
copy /y "config\templates\config.example.json" "dist\Aissist\config\templates\config.example.json" >nul
copy /y "config\templates\apps.example.json" "dist\Aissist\config\templates\apps.example.json" >nul
copy /y "config\templates\mcp.example.json" "dist\Aissist\config\templates\mcp.example.json" >nul

echo [6/6] Creating ZIP package...
where tar.exe >nul 2>nul
if errorlevel 1 goto tar_missing
if exist Aissist_v1.101.5_test_portable.zip del /q Aissist_v1.101.5_test_portable.zip
tar.exe -a -c -f "Aissist_v1.101.5_test_portable.zip" -C "dist" "Aissist"
if errorlevel 1 goto package_failed
if not exist "Aissist_v1.101.5_test_portable.zip" goto package_failed
rem verify the archive can be opened and is free of local chat history
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem; try { $z=[System.IO.Compression.ZipFile]::OpenRead('Aissist_v1.101.5_test_portable.zip'); $names=@($z.Entries.FullName); $z.Dispose() } catch { Write-Host 'ZIP check failed: archive cannot be opened.'; Write-Host $_.Exception.Message; exit 1 }; if ($names.Count -lt 1000) { Write-Host 'ZIP check failed: too few entries.'; exit 1 }; if (-not ($names -match '^Aissist[\\/]Aissist\.exe$')) { Write-Host 'ZIP check failed: Aissist/Aissist.exe is missing.'; exit 1 }; $leak=@($names -like '*chat_history.db'); if ($leak.Count -gt 0) { Write-Host 'ZIP check failed: chat history database was packaged.'; exit 1 }; Write-Host ('ZIP check OK: ' + $names.Count + ' entries, no chat history.')"
if errorlevel 1 goto package_failed

echo.
echo Build complete: Aissist_v1.101.5_test_portable.zip
pause
exit /b 0

:missing_assets
echo Missing generated\kws assets. The wake-word model files are required for this build.
pause
exit /b 1

:build_failed
echo Build failed. Check the error above.
pause
exit /b 1

:package_failed
echo ZIP verification failed. The package is missing, corrupt, or still contains chat history.
pause
exit /b 1

:tar_missing
echo tar.exe was not found. Windows 10 1803 or newer is required to build the package.
pause
exit /b 1

:packaged_assets_missing
echo Live2D resources were not included in the package: assets\web_resources\dist\pet.html
pause
exit /b 1

:packaged_db_leak
echo Chat history database was not removed from the package: assets\web_resources\chat_history.db
pause
exit /b 1

:packaged_icon_missing
echo Application icon was not included in the package: assets\Aissist.ico
pause
exit /b 1
