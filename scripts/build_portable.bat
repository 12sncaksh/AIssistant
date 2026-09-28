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
%PYTHON% -m PyInstaller --noconfirm --clean scripts\Aissistant.spec
if errorlevel 1 goto build_failed
if not exist "dist\Aissistant\Aissistant.exe" goto build_failed

echo [4/6] Copying application, Live2D and wake-word resources...
if not exist "dist\Aissistant\assets" mkdir "dist\Aissistant\assets"
copy /y "assets\Aissistant.ico" "dist\Aissistant\assets\Aissistant.ico" >nul
if errorlevel 1 goto packaged_icon_missing
if not exist "dist\Aissistant\assets\Aissistant.ico" goto packaged_icon_missing
if exist "dist\Aissistant\assets\web_resources" rmdir /s /q "dist\Aissistant\assets\web_resources"
if exist "dist\Aissistant\generated\kws" rmdir /s /q "dist\Aissistant\generated\kws"
xcopy /e /i /y "assets\web_resources" "dist\Aissistant\assets\web_resources" >nul
xcopy /e /i /y "generated\kws" "dist\Aissistant\generated\kws" >nul
rem assets\web_resources is the local HTTP root: drop any leftover chat-history DB so it is never shipped
del /s /q "dist\Aissistant\assets\web_resources\chat_history.db" >nul 2>nul
if exist "dist\Aissistant\assets\web_resources\chat_history.db" goto packaged_db_leak
if exist "dist\Aissistant\assets\web_resources\dist\chat_history.db" goto packaged_db_leak
if not exist "dist\Aissistant\assets\web_resources\dist\pet.html" goto packaged_assets_missing
if not exist "dist\Aissistant\generated\kws\encoder-epoch-12-avg-2-chunk-16-left-64.onnx" goto packaged_assets_missing

echo [5/6] Adding user documentation and configuration templates...
if not exist "dist\Aissistant\config\templates" mkdir "dist\Aissistant\config\templates"
copy /y "docs\README_PORTABLE.md" "dist\Aissistant\README.md" >nul
copy /y "config\templates\auth.example.json" "dist\Aissistant\config\templates\auth.example.json" >nul
copy /y "config\templates\config.example.json" "dist\Aissistant\config\templates\config.example.json" >nul
copy /y "config\templates\apps.example.json" "dist\Aissistant\config\templates\apps.example.json" >nul
copy /y "config\templates\mcp.example.json" "dist\Aissistant\config\templates\mcp.example.json" >nul

echo [6/6] Creating ZIP package...
where tar.exe >nul 2>nul
if errorlevel 1 goto tar_missing
if exist Aissistant_v1.102.5_test_portable.zip del /q Aissistant_v1.102.5_test_portable.zip
tar.exe -a -c -f "Aissistant_v1.102.5_test_portable.zip" -C "dist" "Aissistant"
if errorlevel 1 goto package_failed
if not exist "Aissistant_v1.102.5_test_portable.zip" goto package_failed
rem verify the archive can be opened and is free of local chat history
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem; try { $z=[System.IO.Compression.ZipFile]::OpenRead('Aissistant_v1.102.5_test_portable.zip'); $names=@($z.Entries.FullName); $z.Dispose() } catch { Write-Host 'ZIP check failed: archive cannot be opened.'; Write-Host $_.Exception.Message; exit 1 }; if ($names.Count -lt 1000) { Write-Host 'ZIP check failed: too few entries.'; exit 1 }; if (-not ($names -match '^Aissistant[\\/]Aissistant\.exe$')) { Write-Host 'ZIP check failed: Aissistant/Aissistant.exe is missing.'; exit 1 }; $leak=@($names -like '*chat_history.db'); if ($leak.Count -gt 0) { Write-Host 'ZIP check failed: chat history database was packaged.'; exit 1 }; Write-Host ('ZIP check OK: ' + $names.Count + ' entries, no chat history.')"
if errorlevel 1 goto package_failed

echo.
echo Build complete: Aissistant_v1.102.5_test_portable.zip
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
echo Application icon was not included in the package: assets\Aissistant.ico
pause
exit /b 1
