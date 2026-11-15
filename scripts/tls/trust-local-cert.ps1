# Makes Windows (and Chrome/Edge) trust FileGrid's gateway certificate, so https://localhost
# opens without the "Your connection is not private" page. Run it yourself, once:
#     powershell -ExecutionPolicy Bypass -File scripts\tls\trust-local-cert.ps1
# It adds ONE certificate (the public part of the gateway's own self-signed cert, valid for
# localhost only) to your user's trusted roots. Undo it with:  ... -Remove
# The private key never leaves the nginx container and is not touched.
param([switch]$Remove)
Set-Location -LiteralPath (Join-Path $PSScriptRoot "..\..")

$tmp = Join-Path $env:TEMP "filegrid-gateway.crt"
if ($Remove) {
    Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Subject -like "*O=FileGrid*" } | ForEach-Object {
        Write-Host "Removing $($_.Thumbprint)"
        Remove-Item -Path $_.PSPath
    }
    Write-Host "Done."
    exit 0
}

docker compose cp nginx:/etc/nginx/certs/tls.crt $tmp | Out-Null
if (-not (Test-Path $tmp)) { Write-Host "Could not read the certificate: is FileGrid running (./start.ps1)?" -ForegroundColor Red; exit 1 }
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $tmp
Write-Host "Certificate: $($cert.Subject)  expires $($cert.NotAfter.ToString('yyyy-MM-dd'))  sha1 $($cert.Thumbprint)"
if ($cert.Subject -notlike "*O=FileGrid*") { Write-Host "Not the FileGrid certificate, refusing." -ForegroundColor Red; exit 1 }
Import-Certificate -FilePath $tmp -CertStoreLocation Cert:\CurrentUser\Root | Out-Null   # Windows asks you to confirm
Remove-Item $tmp
Write-Host "Trusted for your user. Restart Chrome/Edge, then open https://localhost" -ForegroundColor Green
Write-Host "(If the certificate is ever regenerated, run this again; -Remove takes it out.)"
