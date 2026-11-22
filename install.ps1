# Install NeonClipping into this folder (the clone). Does not create a separate install directory
# and does not download the app. System tools: Python 3.11-3.13, ffmpeg, ffprobe.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path (Join-Path $PSScriptRoot 'app.py'))) {
    throw "Run this inside the cloned NeonClipping folder (app.py is missing)."
}

function Find-Python {
    foreach ($n in @('python', 'python3', 'py')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if (-not $c) { continue }
        if ($c.Source -match 'WindowsApps') { continue }
        try {
            $v = & $c.Source -c "import sys; print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.11' -and [version]$v -lt [version]'3.14') { return $c.Source }
        } catch {}
    }
    return $null
}

Write-Host "Installing in $PSScriptRoot"
$py = Find-Python
if (-not $py) { throw "Python 3.11, 3.12 or 3.13 is not on PATH." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    throw "ffmpeg and ffprobe are not on PATH. Install them, then run this again (winget install Gyan.FFmpeg)."
}

$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    & $py -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv" }
}
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $venvPy -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host ""
Write-Host "Installed in $PSScriptRoot"
Write-Host "Run the app:  .\run.ps1"
Write-Host "Then open http://127.0.0.1:8770"
