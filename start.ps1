$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = (Get-Command python -ErrorAction Stop).Source

$versionCode = & $python -c 'import sys; print(sys.version_info.major*100+sys.version_info.minor)'
if ($LASTEXITCODE -ne 0) { throw 'Could not check the Python version.' }
if ([int]$versionCode -lt 309) {
    throw 'Voice commands require Python 3.9 or newer.'
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install voice recognition.' }

if (-not $env:TELEGRAM_BOT_TOKEN) {
    $secret = Read-Host 'Enter the new bot token (input hidden)' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if (-not $env:TELEGRAM_BOT_TOKEN) { throw 'Token is empty.' }

$model = Join-Path $PSScriptRoot 'local_model\Qwen3-0.6B-Q4_K_M.gguf'
$server = Join-Path $PSScriptRoot 'local_model\llama\llama-server.exe'
if (-not (Test-Path -LiteralPath $model)) { throw 'Local Qwen model is missing. See README.md.' }
if (-not (Test-Path -LiteralPath $server)) { throw 'Local llama.cpp server is missing. See README.md.' }
$dataDir = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$serverLog = Join-Path $dataDir 'llama-server.log'
$serverErrorLog = Join-Path $dataDir 'llama-server-error.log'
try {
    $existing = Invoke-RestMethod -Uri 'http://127.0.0.1:18765/health' -TimeoutSec 2 -ErrorAction Stop
    throw 'Port 18765 is already in use. Stop the other local model server first.'
} catch [System.Net.WebException] {
    # The port is free or an old server is still starting.
}
$threads = if ($env:LLAMA_THREADS) { $env:LLAMA_THREADS } else { '2' }
$serverArgs = @('-m', ('"' + $model + '"'), '--alias', 'local-qwen3-0.6b', '--host', '127.0.0.1', '--port', '18765', '-c', '4096', '-t', $threads, '-ngl', '0')
$modelProcess = Start-Process -FilePath $server -ArgumentList $serverArgs -WorkingDirectory (Split-Path $server) -WindowStyle Hidden -RedirectStandardOutput $serverLog -RedirectStandardError $serverErrorLog -PassThru
try {
    Write-Host 'Loading the local language model...'
    $ready = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        if ($modelProcess.HasExited) { throw "Local model stopped. Check $serverErrorLog" }
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:18765/health' -TimeoutSec 2 -ErrorAction Stop
            if ($health.status -eq 'ok') { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "Local model did not start. Check $serverErrorLog" }
    Write-Host 'Starting the bot. Keep this window open while you need it running.'
    & $venvPython (Join-Path $PSScriptRoot 'bot.py')
    if ($LASTEXITCODE -ne 0) { throw 'Bot stopped with an error. See the Python message above.' }
} finally {
    if (-not $modelProcess.HasExited) { Stop-Process -Id $modelProcess.Id -Force }
}
