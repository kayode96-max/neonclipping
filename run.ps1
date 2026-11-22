# Start NeonClipping from this folder. Install first with .\install.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "No .venv in this folder. Run .\install.ps1 first."
    exit 1
}
if (-not $env:NEONCLIPPING_PORT) { $env:NEONCLIPPING_PORT = '8770' }
Start-Process "http://127.0.0.1:$env:NEONCLIPPING_PORT"
& $py app.py
