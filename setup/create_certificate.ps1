[CmdletBinding()]
param(
    [string]$Subject = "CN=Entra Zendesk Sync",
    [int]$ValidityYears = 2,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "certificates")
)

$ErrorActionPreference = "Stop"

Write-Host "Entra Zendesk Sync certificate setup"
Write-Host "======================================"
Write-Host ""

if ($ValidityYears -lt 1) {
    throw "ValidityYears must be at least 1."
}

if (-not (Test-Path $OutputDirectory)) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}

$cerPath = Join-Path $OutputDirectory "entra-zendesk-sync.cer"
$pfxPath = Join-Path $OutputDirectory "entra-zendesk-sync.pfx"

if ((Test-Path $cerPath) -or (Test-Path $pfxPath)) {
    throw "Certificate output already exists in '$OutputDirectory'. Move or remove the existing files before running this script again."
}

Write-Host "Creating self-signed certificate..." -ForegroundColor Cyan
$cert = New-SelfSignedCertificate `
    -Subject $Subject `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -KeyExportPolicy Exportable `
    -NotAfter (Get-Date).AddYears($ValidityYears)

Write-Host "Created certificate:" -ForegroundColor Green
Write-Host "  Subject:    $($cert.Subject)"
Write-Host "  Thumbprint: $($cert.Thumbprint)"
Write-Host "  Expires:    $($cert.NotAfter)"
Write-Host ""

Write-Host "Exporting public certificate..." -ForegroundColor Cyan
Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
Write-Host "  Public certificate: $cerPath" -ForegroundColor Green
Write-Host ""

Write-Host "Create a password for the exported PFX." -ForegroundColor Cyan
Write-Host "You will need this password to use or import the PFX later."
$pfxPassword = Read-Host "PFX password" -AsSecureString

Write-Host "Exporting certificate and private key..." -ForegroundColor Cyan
Export-PfxCertificate `
    -Cert $cert `
    -FilePath $pfxPath `
    -Password $pfxPassword | Out-Null

Write-Host "  Private certificate bundle: $pfxPath" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT STEPS" -ForegroundColor Yellow
Write-Host "1. Upload ONLY the .cer file to Entra App Registration > Certificates & secrets > Certificates."
Write-Host "2. Keep the .pfx file and its password private. Never upload the .pfx to GitHub or Entra."
Write-Host "3. Store the PFX securely until it is installed on the machine that will run the sync."
Write-Host "4. Record the certificate thumbprint shown above for troubleshooting and rotation."
Write-Host ""
Write-Host "Certificate setup complete." -ForegroundColor Green
