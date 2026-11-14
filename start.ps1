# FileGrid — Windows start (PowerShell 5+ / 7)
# Linux & WSL:  ./start.sh
# Windows:      .\start.ps1   or   start.cmd
#
# Docker prints build progress on stderr. PowerShell must NOT treat that as a
# terminating error, and must NOT capture it as the "exit code".

Set-Location -LiteralPath $PSScriptRoot
$ErrorActionPreference = "Continue"

function Rand {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

if (-not (Test-Path -LiteralPath .env)) {
    New-Item -ItemType File -Path .env | Out-Null
}

function Ensure([string]$Key, [string]$Value) {
    if (-not (Select-String -Path .env -Pattern ("^" + [regex]::Escape($Key) + "=") -Quiet)) {
        Write-Host ">> generating $Key"
        Add-Content -Path .env -Value "$Key=$Value" -Encoding ascii
    }
}

Ensure POSTGRES_META_PASSWORD (Rand)
Ensure POSTGRES_BLOB_PASSWORD (Rand)
Ensure MINIO_ROOT_USER ("fg" + (Rand).Substring(0, 10))
Ensure MINIO_ROOT_PASSWORD (Rand)
Ensure FTP_USER filegrid
Ensure FTP_PASSWORD (Rand)
Ensure JWT_SECRET ((Rand) + (Rand))
Ensure SIMULATOR_TOKEN (Rand)
Ensure DISPECER_PASSWORD dispecer
Ensure ADMIN_PASSWORD admin

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker is not installed. Install Docker Desktop, start it, then re-run .\start.ps1" -ForegroundColor Red
    exit 1
}

cmd /c "docker info >NUL 2>&1"
if ($LASTEXITCODE -ne 0) {
    $desktop = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $desktop) {
        Write-Host "Docker engine is not running. Start Docker Desktop, wait until it says Running, then re-run .\start.ps1" -ForegroundColor Red
        exit 1
    }
    Write-Host ">> Docker engine is not running. Starting Docker Desktop..."
    Start-Process $desktop
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        cmd /c "docker info >NUL 2>&1"
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Write-Host "   waiting for Docker engine... ($($i + 1)/60)"
    }
    if (-not $ready) {
        Write-Host "Docker Desktop did not become ready. Wait until it says Running, then re-run .\start.ps1" -ForegroundColor Red
        exit 1
    }
}

Write-Host ">> Building and starting FileGrid"
# Start-Process keeps the real docker exit code and does not paint stderr red.
$argList = @("compose", "up", "-d", "--build") + @($args)
$proc = Start-Process -FilePath "docker" -ArgumentList $argList -Wait -NoNewWindow -PassThru
if ($proc.ExitCode -ne 0) {
    Write-Host "docker compose failed (exit $($proc.ExitCode)). Try: docker compose ps ; docker compose logs" -ForegroundColor Red
    exit $proc.ExitCode
}

Write-Host ">> Waiting for the gateway to become healthy"
for ($i = 0; $i -lt 90; $i++) {
    $body = cmd /c "curl.exe -sk --max-time 3 https://localhost/api/v1/health 2>NUL"
    if ($body -match '"status"\s*:\s*"(ok|degraded)"') {
        Write-Host ""
        Write-Host "FileGrid is up:  https://localhost   (self-signed certificate)"
        Write-Host "Login:           dispecer@ppc.ro / dispecer   or   admin@ppc.ro / admin"
        if ($body -match '"status"\s*:\s*"degraded"') {
            Write-Host "Note: /health is degraded (a storage may still be starting)."
        }
        exit 0
    }
    Start-Sleep -Seconds 2
}

Write-Host "Timed out waiting for health. Check: docker compose ps ; docker compose logs nginx ; docker compose logs backend" -ForegroundColor Red
exit 1
