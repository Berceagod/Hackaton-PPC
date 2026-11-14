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
# AES-256 master key for personal data at rest (ADR-0011). Back up .env: without this key
# encrypted documents cannot be read. Never commit it.
Ensure FILEGRID_MASTER_KEY (Rand)
# UI accounts get random passwords (no password lives in the repository, ADR-0012).
# Installations created before this change used the literal demo passwords: replace them.
$envLines = Get-Content -LiteralPath .env | Where-Object { $_ -ne "DISPECER_PASSWORD=dispecer" -and $_ -ne "ADMIN_PASSWORD=admin" }
Set-Content -LiteralPath .env -Value $envLines -Encoding ascii
Ensure DISPECER_PASSWORD ((Rand).Substring(0, 16))
Ensure ADMIN_PASSWORD ((Rand).Substring(0, 16))

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

Write-Host ">> Waiting for FileGrid to come up"
for ($i = 0; $i -lt 90; $i++) {
    # /api/v1/livez is public and reveals nothing; the storage report needs a login (slice D)
    $body = cmd /c "curl.exe -sk --max-time 3 https://localhost/api/v1/livez 2>NUL"
    if ($body -match '"status"\s*:\s*"alive"') {
        $pwD = ((Select-String -Path .env -Pattern '^DISPECER_PASSWORD=').Line -split '=', 2)[1]
        $pwA = ((Select-String -Path .env -Pattern '^ADMIN_PASSWORD=').Line -split '=', 2)[1]
        Write-Host ""
        Write-Host "FileGrid is up:  https://localhost   (self-signed certificate)"
        Write-Host "Login:           dispecer@ppc.ro / $pwD"
        Write-Host "                 admin@ppc.ro    / $pwA"
        Write-Host "                 (generated for this machine, stored only in .env)"
        $deps = cmd /c "docker compose exec -T backend python -c ""import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/health', timeout=10).read().decode())"" 2>NUL"
        if ($deps -match '"status"\s*:\s*"degraded"') {
            Write-Host "Note: a storage is still starting or down: $deps"
        }
        exit 0
    }
    Start-Sleep -Seconds 2
}

Write-Host "Timed out waiting for FileGrid. Check: docker compose ps ; docker compose logs nginx ; docker compose logs backend" -ForegroundColor Red
exit 1
