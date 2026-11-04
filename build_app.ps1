$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    throw "Project venv not found. Create it first with: python -m venv venv"
}

$python = Resolve-Path ".\venv\Scripts\python.exe"
& $python -m pip install pyinstaller
if (-not $?) { throw "PyInstaller installation failed." }

if (Test-Path ".\build") { Remove-Item ".\build" -Recurse -Force }
if (Test-Path ".\dist\JetonReservation") { Remove-Item ".\dist\JetonReservation" -Recurse -Force }

& $python -m PyInstaller --noconfirm --clean ".\JetonReservation.spec"
if (-not $?) { throw "Application build failed." }

$app = Join-Path $root "dist\JetonReservation"

Write-Output "Application created at: $app"
