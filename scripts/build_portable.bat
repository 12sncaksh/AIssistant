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
rem docs\ is not published in this repository: prefer the end-user portable notes,
rem otherwise fall back to the repository README.md, and never fail silently.
if exist "docs\README_PORTABLE.md" (
    copy /y "docs\README_PORTABLE.md" "dist\Aissistant\README.md" >nul
) else if exist "README.md" (
    echo   docs\README_PORTABLE.md not found - packaging the repository README.md instead.
    copy /y "README.md" "dist\Aissistant\README.md" >nul
) else (
    echo   WARNING: neither docs\README_PORTABLE.md nor README.md exists.
    echo   The package will ship without a README.
)
copy /y "config\templates\auth.example.json" "dist\Aissistant\config\templates\auth.example.json" >nul
if errorlevel 1 goto templates_missing
copy /y "config\templates\config.example.json" "dist\Aissistant\config\templates\config.example.json" >nul
if errorlevel 1 goto templates_missing
copy /y "config\templates\apps.example.json" "dist\Aissistant\config\templates\apps.example.json" >nul
if errorlevel 1 goto templates_missing
copy /y "config\templates\mcp.example.json" "dist\Aissistant\config\templates\mcp.example.json" >nul
if errorlevel 1 goto templates_missing
copy /y "config\templates\live2d_emotions.example.json" "dist\Aissistant\config\templates\live2d_emotions.example.json" >nul
if errorlevel 1 goto templates_missing

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
echo Missing wake-word assets under generated\kws. This build requires:
echo   encoder-epoch-12-avg-2-chunk-16-left-64.onnx
echo   decoder-epoch-12-avg-2-chunk-16-left-64.onnx
echo   joiner-epoch-12-avg-2-chunk-16-left-64.onnx
echo   tokens.txt
echo   keywords.txt
echo They are not published in this repository. Get them from the sherpa-onnx KWS model
echo   https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
echo copy the three .onnx files plus tokens.txt into generated\kws, then build keywords.txt with
echo   sherpa-onnx-cli text2token --tokens generated\kws\tokens.txt --tokens-type ppinyin keywords_raw.txt keywords.txt
echo See README.md ("resources not shipped in this repository") for details.
pause
exit /b 1

:templates_missing
echo Failed to copy config\templates into the package. Check config\templates\*.example.json.
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
echo Packaged Live2D or wake-word resources are incomplete. Expected at least:
echo   assets\web_resources\dist\pet.html
echo   generated\kws\encoder-epoch-12-avg-2-chunk-16-left-64.onnx
echo These files are not shipped in this repository, so a fresh clone cannot be packaged
echo as-is. See README.md for the full list. Nothing failed in PyInstaller itself.
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
