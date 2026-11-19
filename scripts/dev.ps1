# One-command local start on Windows (no Ubuntu / WSL required).
#   .\scripts\dev.ps1              rehearsal: replay discovery, cached research, no real money
#   .\scripts\dev.ps1 -Mode live   live discovery + real x402 research (asks for "yes")
#   .\scripts\dev.ps1 -Mode mainnet  MAINNET REAL FUNDS: live discovery + x402 research, and approved cards send real
#                                   Solana mainnet swaps via Jupiter (asks you to type MAINNET; hard $5/trade ceiling)
#   .\scripts\dev.ps1 -Setup       install Python venv, npm deps, and the Windows Surfpool binary
#   .\scripts\dev.ps1 -Lan         also open the UI to phones on the same Wi-Fi (http://<this PC's IP>:5173)
#   .\scripts\dev.ps1 -Lan -Https  same, over self-signed HTTPS: phones only give the microphone to https:// pages
#
# Without -Mode mainnet, trading only happens on the local Surfpool fork. Backend and frontend keep running after
# this window is closed; stop them with .\scripts\stop_dev.ps1
# (Surfpool stays up until .\scripts\stop_surfpool.ps1).
param(
    [ValidateSet('rehearsal', 'live', 'mainnet')]
    [string]$Mode = 'rehearsal',
    [switch]$Setup,
    [switch]$Lan,
    [switch]$Https
)

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

function Test-LocalPortInUse([int]$Port) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { return $true }
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $false
    } catch {
        return $true
    } finally {
        if ($listener) { try { $listener.Stop() } catch {} }
    }
}

function Select-FreePort([int]$Preferred, [int[]]$Fallbacks) {
    if (-not (Test-LocalPortInUse $Preferred)) { return $Preferred }
    foreach ($p in $Fallbacks) {
        if (-not (Test-LocalPortInUse $p)) { return $p }
    }
    throw "No free port near $Preferred (tried $Preferred, $($Fallbacks -join ', '))."
}

function Get-Python {
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    foreach ($cmd in @('py', 'python', 'python3')) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($exe) { return $exe.Source }
    }
    return $null
}

# The Windows Surfpool build links OpenSSL 3 (libssl-3-x64.dll / libcrypto-3-x64.dll) but does not ship it.
# Without them it exits at once with 0xC0000135. Git for Windows ships a compatible pair.
function Copy-OpenSslDlls([string]$tools) {
    if ((Test-Path (Join-Path $tools 'libssl-3-x64.dll')) -and (Test-Path (Join-Path $tools 'libcrypto-3-x64.dll'))) { return }
    $candidates = @("$env:ProgramFiles\Git\mingw64\bin", "${env:ProgramFiles(x86)}\Git\mingw64\bin", "$env:LOCALAPPDATA\Programs\Git\mingw64\bin")
    foreach ($dir in $candidates) {
        if ($dir -and (Test-Path (Join-Path $dir 'libssl-3-x64.dll')) -and (Test-Path (Join-Path $dir 'libcrypto-3-x64.dll'))) {
            Copy-Item (Join-Path $dir 'libssl-3-x64.dll'), (Join-Path $dir 'libcrypto-3-x64.dll') $tools -Force
            Write-Host "Copied OpenSSL 3 DLLs from $dir"
            return
        }
    }
    Write-Host "WARNING: OpenSSL 3 DLLs not found. Install Git for Windows (https://git-scm.com/download/win), then re-run -Setup."
}

function Install-WindowsSurfpool {
    $tools = Join-Path $Root '.tools'
    $dest = Join-Path $tools 'surfpool.exe'
    if (Test-Path $dest) { Copy-OpenSslDlls $tools; return $dest }
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    $url = 'https://github.com/solana-foundation/surfpool/releases/download/v1.7.0/surfpool-windows-x64.tar.gz'
    $tgz = Join-Path $tools 'surfpool-windows-x64.tar.gz'
    Write-Host "Downloading Surfpool 1.7.0 for Windows..."
    Invoke-WebRequest -Uri $url -OutFile $tgz
    tar -xf $tgz -C $tools
    $found = Get-ChildItem -Path $tools -Recurse -Filter 'surfpool.exe' | Select-Object -First 1
    if (-not $found) { throw "The Surfpool archive extracted, but surfpool.exe was not inside it." }
    if ($found.FullName -ne $dest) { Copy-Item $found.FullName $dest -Force }
    Write-Host "Surfpool installed at $dest"
    Copy-OpenSslDlls $tools
    Write-Host "If Windows Defender blocked the unsigned build, allow it and re-run."
    return $dest
}

function Invoke-Setup {
    $pyCmd = Get-Python
    if (-not $pyCmd) { throw "Python 3 was not found. Install Python 3.12+ from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'." }
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        Write-Host "Creating .venv ..."
        if ((Split-Path $pyCmd -Leaf) -eq 'py.exe' -or (Split-Path $pyCmd -Leaf) -eq 'py') {
            & $pyCmd -3 -m venv (Join-Path $Root '.venv')
        } else {
            & $pyCmd -m venv (Join-Path $Root '.venv')
        }
    }
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    & $venvPy -m pip install -U pip
    & $venvPy -m pip install -r (Join-Path $Root 'requirements.txt')
    & $venvPy -m pip install "x402[httpx,evm,svm]"
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "npm was not found. Install Node.js 20+ from https://nodejs.org/ and re-run with -Setup."
    }
    Push-Location (Join-Path $Root 'frontend')
    try { npm install --no-audit --no-fund } finally { Pop-Location }
    Install-WindowsSurfpool | Out-Null
    Write-Host "Setup complete."
}

Import-DotEnv

if ($Setup) { Invoke-Setup }

if ($Mode -eq 'mainnet') {
    # explicit opt-in only: VT_NETWORK is never read from .env for the other modes (they force 'fork' below)
    $env:VT_NETWORK = 'mainnet'
    $env:VIBE_DISCOVERY = 'live'
    if (-not $env:VIBE_RESEARCH) { $env:VIBE_RESEARCH = 'live' }
    $cap = if ($env:VT_MAINNET_MAX_TRADE_USD) { $env:VT_MAINNET_MAX_TRADE_USD } else { '5' }
    Write-Host ''
    Write-Host '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!' -ForegroundColor Yellow
    Write-Host '  MAINNET REAL FUNDS' -ForegroundColor Yellow
    Write-Host "  Every card you approve sends a real Solana mainnet swap (Jupiter). On-chain trades cannot be undone." -ForegroundColor Yellow
    Write-Host "  Hard ceiling: `$$cap per trade (VT_MAINNET_MAX_TRADE_USD), on top of your mandate limits." -ForegroundColor Yellow
    Write-Host "  Research mode: $($env:VIBE_RESEARCH) (live = x402 research fees from the Base wallet)." -ForegroundColor Yellow
    Write-Host '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!' -ForegroundColor Yellow
    $ans = Read-Host 'Continue with real money? Type MAINNET'
    if ($ans -cne 'MAINNET') { Write-Host 'cancelled'; exit 1 }
} elseif ($Mode -eq 'live') {
    $env:VT_NETWORK = 'fork'
    $env:VIBE_DISCOVERY = 'live'
    $env:VIBE_RESEARCH = 'live'
    Write-Host "WARNING live mode: paid research spends real USDC from the Base research wallet (at most `$0.08 per coin, never more than the mandate research budget)."
    $ans = Read-Host 'Continue? Type yes'
    if ($ans -ne 'yes') { Write-Host 'cancelled'; exit 1 }
} else {
    $env:VT_NETWORK = 'fork'

    if (-not $env:VIBE_DISCOVERY) { $env:VIBE_DISCOVERY = 'replay' }
    if (-not $env:VIBE_RESEARCH) { $env:VIBE_RESEARCH = 'cache' }
}
if (-not $env:VIBE_UI_BUTTONS) { $env:VIBE_UI_BUTTONS = '1' }
Write-Host "mode: network=$($env:VT_NETWORK) discovery=$($env:VIBE_DISCOVERY) research=$($env:VIBE_RESEARCH) ui_buttons=$($env:VIBE_UI_BUTTONS)"

$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    throw "Python venv is missing. From PowerShell run:  .\scripts\dev.ps1 -Setup"
}
if (-not (Test-Path (Join-Path $Root 'frontend\node_modules'))) {
    throw "Frontend dependencies are missing. From PowerShell run:  .\scripts\dev.ps1 -Setup"
}

# The scratch fork (8999) is still used in mainnet mode, only to simulate a test buy/sell of research candidates with a
# throwaway wallet; it never touches the real trading wallet.
& (Join-Path $PSScriptRoot 'start_surfpool.ps1')
if ($env:VT_NETWORK -eq 'mainnet') {
    # no fork funding / cheatcodes for the real wallet: check the mainnet RPC and the real balance instead (fail closed)
    $env:PYTHONUTF8 = '1'
    & $py (Join-Path $Root 'scripts\mainnet_check.py')
    if ($LASTEXITCODE -ne 0) { Write-Host 'mainnet preflight failed: not starting (nothing was sent).'; exit 1 }
} else {
    & $py (Join-Path $Root 'scripts\fund_wallets.py') --sol 2 --usdc 50
}

$backendPort = Select-FreePort 8000 @(8010, 8020, 8030)
$frontendPort = Select-FreePort 5173 @(5174, 5175, 5176)
if ($backendPort -ne 8000 -or $frontendPort -ne 5173) {
    Write-Host "Port 8000 or 5173 is already in use on Windows (another app). Using http://127.0.0.1:$frontendPort  (backend :$backendPort)."
}
$env:VIBE_BACKEND = "http://127.0.0.1:$backendPort"
# Python in UTF-8 mode for the backend child: files and redirected logs are UTF-8 whatever the Windows code page
# (cp1252 / cp936 can't encode emoji in token names).
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$viteHost = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
$scheme = if ($Https) { 'https' } else { 'http' }
if ($Https) { $env:VIBE_HTTPS = '1' } else { Remove-Item Env:VIBE_HTTPS -ErrorAction SilentlyContinue }

$backendOut = Join-Path $Root 'logs\backend.out'
$backendErr = Join-Path $Root 'logs\backend.err'
$frontendOut = Join-Path $Root 'logs\frontend.out'
$frontendErr = Join-Path $Root 'logs\frontend.err'
$vite = Join-Path $Root 'frontend\node_modules\.bin\vite.cmd'
if (-not (Test-Path $vite)) { $vite = Join-Path $Root 'frontend\node_modules\.bin\vite' }
if (-not (Test-Path $vite)) { throw "Vite was not found under frontend\node_modules. Run:  .\scripts\dev.ps1 -Setup" }

# RedirectStandardInput from NUL: without it, Hidden children inherit this console's stdin and
# the PowerShell window can no longer accept typing after the script returns.
$nulIn = Join-Path $Root 'logs\dev.stdin.nul'
if (-not (Test-Path $nulIn)) { New-Item -ItemType File -Path $nulIn -Force | Out-Null }
$backend = Start-Process -FilePath $py -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',"$backendPort") -WorkingDirectory $Root -RedirectStandardInput $nulIn -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $Root 'logs\backend.pid') -Value $backend.Id
$frontend = Start-Process -FilePath $vite -ArgumentList @('--host',$viteHost,'--port',"$frontendPort") -WorkingDirectory (Join-Path $Root 'frontend') -RedirectStandardInput $nulIn -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $Root 'logs\frontend.pid') -Value $frontend.Id
Set-Content -Path (Join-Path $Root 'logs\dev-ports.txt') -Value "backend=$backendPort`nfrontend=$frontendPort`nurl=${scheme}://127.0.0.1:$frontendPort"

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$backendPort/state" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) { $ready = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $ready) {
    Write-Host "backend did not start, see logs\backend.out and logs\backend.err"
    exit 1
}

Write-Host "backend ready at http://127.0.0.1:$backendPort (log: logs\backend.out)"
if ($env:VT_NETWORK -eq 'mainnet') { Write-Host 'MAINNET REAL FUNDS: the UI shows an amber MAINNET badge. Transactions: logs\backend.out ([mainnet] lines) and runs\m6_trades_*.json.' -ForegroundColor Yellow }
Write-Host "frontend: open ${scheme}://127.0.0.1:$frontendPort (Chrome / Edge, allow the microphone)"
if ($Lan) {
    $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -ExpandProperty IPAddress
    foreach ($ip in $ips) { Write-Host "phone (same Wi-Fi): ${scheme}://${ip}:$frontendPort" }
    if (-not $Https) { Write-Host "Note: phones only allow the microphone on https:// pages. For voice on a phone run with -Lan -Https (text chat works either way)." }
    else { Write-Host "The certificate is self-signed: on the phone tap 'Advanced' -> 'Proceed' once. Windows may ask to allow Node.js through the firewall (Private networks)." }
}
Write-Host "This window can be closed. Stop later with .\scripts\stop_dev.ps1"
