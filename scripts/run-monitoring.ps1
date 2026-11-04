param(
    [Parameter(Mandatory = $true)][string]$Session,
    [string]$PythonExe = '',
    [ValidateRange(1, 65535)][int]$Port = 9400,
    [ValidateRange(5, 300)][double]$MaxAge = 20
)
$ErrorActionPreference = 'Stop'
if ([double]::IsNaN($MaxAge) -or [double]::IsInfinity($MaxAge)) {
    throw 'MaxAge must be finite and between 5 and 300 seconds.'
}
# Resolve before changing directory so a caller's relative report path is preserved.
$sessionPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Session)
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
$pythonCommand = Get-Command $PythonExe -ErrorAction Stop
if (-not $pythonCommand.Source) { throw 'PythonExe must identify a Python executable.' }
$PythonExe = $pythonCommand.Source
Push-Location -LiteralPath $projectRoot
try {
    $monitorArgs = @('-m', 'live.monitoring', '--session', $sessionPath,
                     '--port', [string]$Port, '--max-age',
                     $MaxAge.ToString([System.Globalization.CultureInfo]::InvariantCulture))
    & $PythonExe @monitorArgs
    if ($LASTEXITCODE -ne 0) { throw "Paper monitoring stopped with exit code $LASTEXITCODE." }
} finally { Pop-Location }
