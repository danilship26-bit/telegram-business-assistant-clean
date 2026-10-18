$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot 'runtime\python.exe'
$model = Join-Path $PSScriptRoot 'local_model\Qwen3-0.6B-Q4_K_M.gguf'
$whisper = Join-Path $PSScriptRoot 'local_model\whisper-small'
$server = Join-Path $PSScriptRoot 'local_model\llama\llama-server.exe'
$dataDir = Join-Path $PSScriptRoot 'data'
foreach ($path in @($python, $model, $server, (Join-Path $whisper 'model.bin'))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing package file: $path" }
}
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

if (-not $env:TELEGRAM_BOT_TOKEN) {
    $secretPath = Join-Path $dataDir 'token.dat'
    if (Test-Path -LiteralPath $secretPath) {
        Add-Type -AssemblyName System.Security
        $cipher = [IO.File]::ReadAllBytes($secretPath)
        $plain = [Security.Cryptography.ProtectedData]::Unprotect($cipher, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
        try { $env:TELEGRAM_BOT_TOKEN = [Text.Encoding]::UTF8.GetString($plain) }
        finally { [Array]::Clear($plain, 0, $plain.Length) }
    } else {
        $secret = Read-Host 'Enter Telegram bot token (hidden)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
        try { $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
}
if (-not $env:TELEGRAM_BOT_TOKEN) { throw 'Telegram bot token is empty.' }
$env:WHISPER_MODEL = $whisper
if (-not $env:WHISPER_THREADS) { $env:WHISPER_THREADS = '2' }
if (-not $env:LLAMA_THREADS) { $env:LLAMA_THREADS = '2' }
$env:PYTHONUTF8 = '1'

$portOpen = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient('127.0.0.1', 18765)
    $portOpen = $true
    $client.Close()
} catch [System.Net.Sockets.SocketException] { }
if ($portOpen) { throw 'Port 18765 is already in use. Stop the other model server.' }

$serverArgs = @('-m', ('"' + $model + '"'), '--alias', 'local-qwen3-0.6b', '--host', '127.0.0.1', '--port', '18765', '-c', '4096', '-t', $env:LLAMA_THREADS, '-ngl', '0', '--reasoning', 'off')
$modelProcess = Start-Process -FilePath $server -ArgumentList $serverArgs -WorkingDirectory (Split-Path $server) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDir 'llama-server.log') -RedirectStandardError (Join-Path $dataDir 'llama-server-error.log') -PassThru
try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        if ($modelProcess.HasExited) { throw 'Local model exited. Check data\llama-server-error.log.' }
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:18765/health' -TimeoutSec 2 -ErrorAction Stop
            if ($health.status -eq 'ok') { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Local model did not become ready. Check data\llama-server-error.log.' }
    Write-Host 'Local model ready. Starting Telegram bot.'
    # Windows PowerShell treats native stderr log lines as errors under Stop.
    $ErrorActionPreference = 'Continue'
    while ($true) {
        & $python (Join-Path $PSScriptRoot 'bot.py') 2>> (Join-Path $dataDir 'bot-error.log')
        if ($LASTEXITCODE -eq 0) { break }
        Write-Warning 'Bot exited with an error; retrying in 15 seconds. Check data\bot-error.log.'
        Start-Sleep -Seconds 15
    }
} finally {
    if (-not $modelProcess.HasExited) { Stop-Process -Id $modelProcess.Id -Force }
}
