param([string]$Binary = '', [string]$InputFile = '', [string]$ConfigFile = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Binary) {
    $Binary = Join-Path $projectRoot 'build/paper_trader.exe'
    if (-not (Test-Path -LiteralPath $Binary)) { $Binary = Join-Path $projectRoot 'build/Release/paper_trader.exe' }
}
if (-not (Test-Path -LiteralPath $Binary)) { throw 'Build the project first; see README.md.' }
if (-not $InputFile) { $InputFile = Join-Path $projectRoot 'data/sample_ticks.csv' }
if (-not $ConfigFile) { $ConfigFile = Join-Path $projectRoot 'config/paper.conf' }
& $Binary --mode paper --input $InputFile --config $ConfigFile --audit (Join-Path $projectRoot 'reports/audit.csv') --summary (Join-Path $projectRoot 'reports/summary.json')
if ($LASTEXITCODE -ne 0) { throw "Paper replay stopped with exit code $LASTEXITCODE; review reports/summary.json." }
