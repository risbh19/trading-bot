param(
    [string]$Symbol = 'RELIANCE.NS',
    [string]$PythonExe = '',
    [double]$Duration = 0
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
    & $PythonExe -m live.run --symbol $Symbol --duration $Duration
    if ($LASTEXITCODE -ne 0) { throw "Paper session stopped with exit code $LASTEXITCODE. Review the session summary." }
} finally { Pop-Location }
