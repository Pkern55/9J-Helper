# Builds dist\9J-Helper.exe. Run from the project folder:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
# Which Python: $env:PYTHON (CI sets this), then the project .venv, then the `py` launcher.
$py = if ($env:PYTHON) { $env:PYTHON }
      elseif (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" }
      elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
Write-Host "Using Python: $py"

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
