[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\.secrets'),
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$secretDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $secretDirectory)) {
    if ($PSCmdlet.ShouldProcess($secretDirectory, 'create the local secret directory')) {
        [IO.Directory]::CreateDirectory($secretDirectory) | Out-Null
    }
}
$utf8NoBom = [Text.UTF8Encoding]::new($false)
$random = [Security.Cryptography.RandomNumberGenerator]::Create()

function New-RandomBytes([int]$Count) {
    $bytes = [byte[]]::new($Count)
    $random.GetBytes($bytes)
    return $bytes
}

function ConvertTo-LowerHex([byte[]]$Bytes) {
    return -join ($Bytes | ForEach-Object { $_.ToString('x2') })
}

function Write-Secret([string]$Name, [string]$Value) {
    $path = Join-Path $secretDirectory $Name
    if ((Test-Path -LiteralPath $path) -and -not $Force) {
        throw "Refusing to overwrite existing secret: $path (use -Force only for an intentional rotation)"
    }
    if ($PSCmdlet.ShouldProcess($path, 'write a new random local-development secret')) {
        [IO.File]::WriteAllText($path, $Value, $utf8NoBom)
    }
}

try {
    foreach ($name in @(
        'postgres_bootstrap_password',
        'postgres_migrator_password',
        'postgres_api_password',
        'postgres_worker_password',
        'postgres_backup_password',
        'postgres_restore_password'
    )) {
        Write-Secret $name (ConvertTo-LowerHex (New-RandomBytes 32))
    }
    foreach ($name in @(
        'session_secret',
        'field_encryption_key',
        'ota_manifest_key',
        'backup_key'
    )) {
        Write-Secret $name ([Convert]::ToBase64String((New-RandomBytes 32)))
    }
} finally {
    $random.Dispose()
}

if ($WhatIfPreference) {
    Write-Output "Validated the PowerMeter V2 local secret generation plan for $secretDirectory."
} else {
    Write-Output "Created PowerMeter V2 local secrets in $secretDirectory; values were not displayed."
}
