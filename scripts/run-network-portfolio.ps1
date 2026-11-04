param(
    [string]$ConfigFile = '',
    [string]$PythonExe = '',
    [double]$Duration = 0,
    [string]$Resume = '',
    [ValidateRange(0, 10)][int]$MaxFeedRetries = 3
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ConfigFile) { $ConfigFile = Join-Path $projectRoot 'config/network-portfolio.json' }
if (-not $PythonExe) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $bundledPython) { $PythonExe = $bundledPython }
    else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand -and $pythonCommand.Source -notlike '*WindowsApps*') { $PythonExe = $pythonCommand.Source }
    }
}
if (-not $PythonExe) { throw 'Python 3.10+ is required. Pass its executable using -PythonExe.' }
Push-Location -LiteralPath $projectRoot
try {
    $portfolioArgs = @('-m', 'live.portfolio_run', '--config', $ConfigFile,
                        '--duration', [string]$Duration, '--max-feed-retries', [string]$MaxFeedRetries)
    if ($Resume) { $portfolioArgs += @('--resume', $Resume) }
    & $PythonExe @portfolioArgs
    if ($LASTEXITCODE -ne 0) { throw "Paper portfolio stopped with exit code $LASTEXITCODE. Review its summary." }
} finally { Pop-Location }
