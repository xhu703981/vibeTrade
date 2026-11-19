# Start two Surfpool mainnet forks on Windows: main 8899 and scratch 8999.
# Same flags as scripts/start_surfpool.sh. Does not print RPC secrets.
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null

function Import-DotEnv {
    $path = Join-Path $Root '.env'
    if (-not (Test-Path $path)) { return }
    Get-Content -Path $path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq '' -or $line.StartsWith('#')) { return }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { return }
        $name = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($name) { Set-Item -Path "Env:$name" -Value $value }
    }
}

function Test-ForkHealth([int]$Port) {
    try {
        $body = '{"jsonrpc":"2.0","id":1,"method":"getHealth"}'
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port" -Method POST -ContentType 'application/json' -Body $body -UseBasicParsing -TimeoutSec 2
        return ($r.Content -match 'result')
    } catch { return $false }
}

function Find-Surfpool {
    if ($env:SURFPOOL_BIN -and (Test-Path $env:SURFPOOL_BIN)) { return $env:SURFPOOL_BIN }
    $local = Join-Path $Root '.tools\surfpool.exe'
    if (Test-Path $local) { return $local }
    $cmd = Get-Command surfpool -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Surfpool was not found. From PowerShell run:  .\scripts\dev.ps1 -Setup"
}

Import-DotEnv
$rpc = $env:SOLANA_RPC_URL
if (-not $rpc) { throw "SOLANA_RPC_URL is not set (add it to .env)" }
if ($rpc -match '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
    $rpc = "https://mainnet.helius-rpc.com/?api-key=$rpc"
}
$env:SURFPOOL_DATASOURCE_RPC_URL = $rpc
if ($null -eq $env:SURFPOOL_MEM_FLAGS) {
    $env:SURFPOOL_MEM_FLAGS = '--disable-instruction-profiling --max-profiles 16'
}

$bin = Find-Surfpool

function Start-One($Name, [int]$Port, [int]$Ws) {
    if (Test-ForkHealth $Port) {
        Write-Host "[$Name] already running on :$Port"
        return
    }
    $dir = Join-Path $Root ".surfpool\$Name"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $out = Join-Path $Root "logs\$Name.out"
    $err = Join-Path $Root "logs\$Name.err"
    $argList = @(
        'start',
        '--port', "$Port",
        '--ws-port', "$Ws",
        '--no-tui', '--no-studio', '--no-deploy', '--yes',
        '--airdrop-amount', '0',
        '--slot-time', '400',
        '--surfnet-id', $Name,
        '--log-path', (Join-Path $Root "logs\$Name")
    )
    if ($env:SURFPOOL_MEM_FLAGS -and $env:SURFPOOL_MEM_FLAGS.Trim()) {
        $argList += ($env:SURFPOOL_MEM_FLAGS -split '\s+' | Where-Object { $_ })
    }
    $nulIn = Join-Path $Root 'logs\dev.stdin.nul'
    if (-not (Test-Path $nulIn)) { New-Item -ItemType File -Path $nulIn -Force | Out-Null }
    $proc = Start-Process -FilePath $bin -ArgumentList $argList -WorkingDirectory $dir -RedirectStandardInput $nulIn -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $dir 'pid') -Value $proc.Id
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-ForkHealth $Port) {
            Write-Host "[$Name] ready at http://127.0.0.1:$Port (pid $($proc.Id))"
            $ok = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ok) { throw "[$Name] startup timed out, see logs\$Name.out and logs\$Name.err" }
}

Start-One 'main' 8899 8900
Start-One 'scratch' 8999 9000
