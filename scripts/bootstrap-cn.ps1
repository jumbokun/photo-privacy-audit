param(
  [string]$Python = "python",
  [string]$IndexUrl = $(if ($env:PHOTO_AUDIT_PIP_INDEX_URL) { $env:PHOTO_AUDIT_PIP_INDEX_URL } else { "https://pypi.tuna.tsinghua.edu.cn/simple" })
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
  & $Python -m venv .venv
}
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if ($env:PHOTO_AUDIT_WHEELHOUSE) {
  & $VenvPython -m pip install --no-index --find-links $env:PHOTO_AUDIT_WHEELHOUSE -r requirements-dev.txt
} else {
  & $VenvPython -m pip install --index-url $IndexUrl -r requirements-dev.txt
}
& $VenvPython audit.py doctor
