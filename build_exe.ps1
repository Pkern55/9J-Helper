# Builds dist\9J-Helper.exe. Run from the project folder:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

$pyArgs = @("-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
            "--name", "9J-Helper", "--collect-data", "pdfplumber")
if (Test-Path "client_secret.json") {
    $pyArgs += @("--add-data", "client_secret.json;.")
} else {
    Write-Warning "client_secret.json not found - the exe will look for it next to itself or in %APPDATA%\9J Helper."
}
$pyArgs += "run_9j_helper.py"
& $py @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built dist\9J-Helper.exe"
