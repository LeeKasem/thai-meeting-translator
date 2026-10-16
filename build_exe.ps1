# Builds dist\ThaiMeetingTranslator.exe (single file, no console window).
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Needs PyInstaller in the venv:  .venv\Scripts\python -m pip install pyinstaller
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& .venv\Scripts\python.exe -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name ThaiMeetingTranslator `
    --collect-all azure.cognitiveservices.speech `
    --collect-all pyaudiowpatch `
    --add-data "translator\assets;translator\assets" `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built dist\ThaiMeetingTranslator.exe. Put your .env next to it."
