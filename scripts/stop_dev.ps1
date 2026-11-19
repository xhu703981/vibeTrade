# Stop the Windows-native backend and frontend started by dev.ps1.
# Does not stop Surfpool (use .\scripts\stop_surfpool.ps1) and never kills other apps on 8000/5173.
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
foreach ($name in @('backend', 'frontend')) {
    $pidFile = Join-Path $Root "logs\$name.pid"
    if (Test-Path $pidFile) {
        $procId = (Get-Content $pidFile | Select-Object -First 1).Trim()
        if ($procId) {
            $proc = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                # Also stop any child processes with the same name launched under this pid tree
                Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq [int]$procId } | ForEach-Object {
                    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                }
                Write-Host "stopped $name (pid $procId)"
            } else {
                Write-Host "$name pid $procId is already gone"
            }
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

# Sweep: also stop any leftover backend/frontend from earlier starts of THIS repo (pid files can miss
# grandchildren, e.g. venv launcher -> python, vite.cmd -> cmd -> node). Matches only our own command lines:
# uvicorn backend.app:app on 127.0.0.1, and vite.js under this repo's frontend folder. Tack-UI etc. are untouched.
$frontendDir = (Join-Path $Root 'frontend').ToLower()
Get-CimInstance Win32_Process | Where-Object {
    $cl = [string]$_.CommandLine
    ($cl -match 'uvicorn\s+backend\.app:app' -and $cl -match '--host\s+127\.0\.0\.1') -or
    ($cl.ToLower().Contains($frontendDir) -and $cl -match 'vite')
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "stopped leftover pid $($_.ProcessId)"
}
