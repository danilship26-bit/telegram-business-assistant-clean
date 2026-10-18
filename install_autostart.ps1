$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script in an elevated PowerShell window (Run as administrator).'
}

$rebootNeeded = $false
if (-not (Test-Path -LiteralPath (Join-Path $env:WINDIR 'System32\msvcp140.dll'))) {
    $redist = Join-Path $env:TEMP 'telegram-assistant-vc_redist.x64.exe'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri 'https://aka.ms/vc14/vc_redist.x64.exe' -OutFile $redist
    $signature = Get-AuthenticodeSignature -LiteralPath $redist
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft') {
        throw 'Microsoft Visual C++ runtime installer signature is invalid.'
    }
    $install = Start-Process -FilePath $redist -ArgumentList @('/install','/quiet','/norestart') -WindowStyle Hidden -Wait -PassThru
    if ($install.ExitCode -notin @(0, 3010, 1638)) { throw "Visual C++ runtime installation failed: $($install.ExitCode)" }
    if ($install.ExitCode -eq 3010) { $rebootNeeded = $true }
}

$dataDir = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$secret = Read-Host 'Enter Telegram bot token for the server (hidden)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if (-not $token) { throw 'Token is empty.' }
    $plain = [Text.Encoding]::UTF8.GetBytes($token)
    Add-Type -AssemblyName System.Security
    $cipher = [Security.Cryptography.ProtectedData]::Protect($plain, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    [IO.File]::WriteAllBytes((Join-Path $dataDir 'token.dat'), $cipher)
    [Array]::Clear($plain, 0, $plain.Length)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}

& icacls.exe $dataDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the data directory permissions.' }

$script = Join-Path $PSScriptRoot 'start_portable.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 30 -StartWhenAvailable
Register-ScheduledTask -TaskName 'TelegramPersonalAssistant' -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Force | Out-Null
if ($rebootNeeded) {
    Write-Warning 'Restart Windows Server to finish installing the runtime; the bot task will start after reboot.'
} else {
    Start-ScheduledTask -TaskName 'TelegramPersonalAssistant'
    Write-Host 'Installed and started TelegramPersonalAssistant. It will also start after reboot.'
}
