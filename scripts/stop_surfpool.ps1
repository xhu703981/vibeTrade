# Stop the two local Surfpool forks started by start_surfpool.ps1 / start_surfpool.sh
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
foreach ($name in @('main', 'scratch')) {
    $pidFile = Join-Path $Root ".surfpool\$name\pid"
    if (Test-Path $pidFile) {
        $procId = (Get-Content $pidFile | Select-Object -First 1).Trim()
        if ($procId) {
            Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue
            Write-Host "stopped $name"
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}
