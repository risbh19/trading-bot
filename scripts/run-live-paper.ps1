param(
    [string]$Symbol = 'RELIANCE.NS',
    [string]$PythonExe = '',
    [double]$Duration = 0,
    [string]$Resume = '',
    [ValidateRange(0, 10)][int]$MaxFeedRetries = 3
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
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
    $livePaperArgs = @('-m', 'live.run', '--symbol', $Symbol, '--duration', [string]$Duration,
                       '--max-feed-retries', [string]$MaxFeedRetries)
    if ($Resume) { $livePaperArgs += @('--resume', $Resume) }
    & $PythonExe @livePaperArgs
    if ($LASTEXITCODE -ne 0) { throw "Paper session stopped with exit code $LASTEXITCODE. Review the session summary." }
} finally { Pop-Location }
