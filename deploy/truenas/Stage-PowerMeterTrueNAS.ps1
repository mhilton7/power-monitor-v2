<#
.SYNOPSIS
Stages the exact 13 PowerMeter V2 secret/TLS files through authenticated SMB.

.DESCRIPTION
This script never generates, rotates, overwrites, or prints secret values. It
validates the exact local input set and TLS contract, copies through a unique
same-share staging directory, verifies every copy, then renames each file into
the empty secrets-share root. A transient completion marker is written last and
removed only after the exact remote set is verified. If publication fails, the
helper removes only destination files moved by that invocation. Existing
differing or partial destinations are rejected for manual review.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SourceDirectory,

    [Parameter()]
    [string]$TrueNasAddress = '192.168.0.175',

    [Parameter()]
    [string]$ShareName = 'PowerMeterV2-Secrets',

    [Parameter()]
    [string]$HostName = 'power-monitor.home.arpa',

    [Parameter(Mandatory)]
    [System.Management.Automation.PSCredential]$Credential
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$DatabaseSecretNames = @(
    'postgres_bootstrap_password'
    'postgres_migrator_password'
    'postgres_api_password'
    'postgres_worker_password'
    'postgres_backup_password'
    'postgres_restore_password'
)
$ApplicationSecretNames = @('session_secret', 'field_encryption_key', 'ota_manifest_key')
$TlsNames = @('tls.crt', 'tls.key', 'tls-ca.crt')
$RequiredNames = @(
    $DatabaseSecretNames +
    $ApplicationSecretNames +
    @('backup_encryption_key') +
    $TlsNames
)

if ($ShareName -cne 'PowerMeterV2-Secrets') {
    throw 'The staging helper is restricted to the fixed PowerMeterV2-Secrets share.'
}
if ($TrueNasAddress -cnotmatch '^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$') {
    throw 'TrueNasAddress is not a valid host or IP literal.'
}
if ($HostName.Length -gt 253 -or $HostName.EndsWith('.')) {
    throw 'HostName is not a valid multi-label DNS name.'
}
$hostLabels = @($HostName.Split('.'))
if ($hostLabels.Count -lt 2 -or @($hostLabels | Where-Object {
    $_.Length -gt 63 -or $_ -cnotmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$'
}).Count -ne 0) {
    throw 'HostName is not a valid multi-label DNS name.'
}
$parsedAddress = $null
if ([System.Net.IPAddress]::TryParse($HostName, [ref]$parsedAddress)) {
    throw 'HostName must be a DNS name, not an IP address.'
}

function Get-ExactDirectoryFiles {
    param([Parameter(Mandatory)][string]$LiteralPath)

    $root = Get-Item -LiteralPath $LiteralPath -Force
    if (-not $root.PSIsContainer -or
        ($root.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'The source must be a real local directory.'
    }
    $items = @(Get-ChildItem -LiteralPath $LiteralPath -Force)
    $actual = @($items | ForEach-Object { $_.Name })
    $missing = @($RequiredNames | Where-Object { $actual -cnotcontains $_ })
    $unexpected = @($actual | Where-Object { $RequiredNames -cnotcontains $_ })
    if ($missing.Count -ne 0 -or $unexpected.Count -ne 0 -or $items.Count -ne 13) {
        throw 'The source directory must contain only the exact 13 required files.'
    }
    foreach ($item in $items) {
        if ($item.PSIsContainer -or
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $item.Length -le 0 -or $item.Length -gt 1MB) {
            throw "A source input is not a bounded real file: $($item.Name)"
        }
    }
}

function Read-AsciiValue {
    param([Parameter(Mandatory)][string]$LiteralPath)

    $bytes = [System.IO.File]::ReadAllBytes($LiteralPath)
    try {
        if ($bytes.Length -eq 0 -or $bytes.Length -gt 4096 -or
            @($bytes | Where-Object { $_ -eq 0 -or $_ -eq 10 -or $_ -eq 13 -or $_ -gt 127 }).Count -ne 0) {
            throw "A secret has an invalid single-value encoding: $([System.IO.Path]::GetFileName($LiteralPath))"
        }
        return [System.Text.Encoding]::ASCII.GetString($bytes)
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Convert-LowerHexToBytes {
    param([Parameter(Mandatory)][string]$Value)

    if ($Value -cnotmatch '^[0-9a-f]{64}$') {
        throw 'A database secret is not exactly 64 lowercase hexadecimal characters.'
    }
    $bytes = New-Object byte[] 32
    for ($index = 0; $index -lt 32; $index += 1) {
        $bytes[$index] = [Convert]::ToByte($Value.Substring($index * 2, 2), 16)
    }
    return $bytes
}

function Convert-CanonicalBase64 {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Name
    )

    try {
        $bytes = [Convert]::FromBase64String($Value)
    }
    catch {
        throw "A secret is not valid Base64: $Name"
    }
    if ([Convert]::ToBase64String($bytes) -cne $Value) {
        [Array]::Clear($bytes, 0, $bytes.Length)
        throw "A secret is not canonical Base64: $Name"
    }
    return $bytes
}

function Test-SecretFormats {
    param([Parameter(Mandatory)][string]$LiteralPath)

    $normalized = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($name in $DatabaseSecretNames) {
        $value = Read-AsciiValue -LiteralPath (Join-Path $LiteralPath $name)
        $bytes = Convert-LowerHexToBytes -Value $value
        try {
            if (-not $normalized.Add([Convert]::ToBase64String($bytes))) {
                throw 'Application and database secret values must all be independent.'
            }
        }
        finally {
            [Array]::Clear($bytes, 0, $bytes.Length)
            $value = $null
        }
    }
    foreach ($name in $ApplicationSecretNames) {
        $value = Read-AsciiValue -LiteralPath (Join-Path $LiteralPath $name)
        $bytes = Convert-CanonicalBase64 -Value $value -Name $name
        try {
            if ($bytes.Length -ne 32) {
                throw "An application secret does not decode to exactly 32 bytes: $name"
            }
            if (-not $normalized.Add([Convert]::ToBase64String($bytes))) {
                throw 'Application and database secret values must all be independent.'
            }
        }
        finally {
            [Array]::Clear($bytes, 0, $bytes.Length)
            $value = $null
        }
    }
    $backupName = 'backup_encryption_key'
    $backupValue = Read-AsciiValue -LiteralPath (Join-Path $LiteralPath $backupName)
    $backupBytes = $null
    try {
        try {
            $backupBytes = Convert-CanonicalBase64 -Value $backupValue -Name $backupName
        }
        catch {
            $backupBytes = $null
        }
        if ($null -ne $backupBytes -and $backupBytes.Length -ge 32) {
            if (-not $normalized.Add([Convert]::ToBase64String($backupBytes))) {
                throw 'Application and database secret values must all be independent.'
            }
        }
        elseif ($backupValue -cnotmatch '^[!-~]+( [!-~]+){5,}$') {
            throw 'backup_encryption_key must be 32+ random Base64 bytes or six Diceware words.'
        }
    }
    finally {
        if ($null -ne $backupBytes) {
            [Array]::Clear($backupBytes, 0, $backupBytes.Length)
        }
        $backupValue = $null
    }
}

function Find-OpenSsl {
    $command = Get-Command openssl.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles})) {
        $candidates += Join-Path ${env:ProgramFiles} 'Git\mingw64\bin\openssl.exe'
    }
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} 'Git\mingw64\bin\openssl.exe'
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw 'OpenSSL from Git for Windows is required for strict TLS validation.'
}

function Invoke-OpenSsl {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Failure,
        [Parameter()][switch]$Capture
    )

    $output = @(& $Executable @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw $Failure
    }
    if ($Capture) {
        return ($output -join "`n")
    }
}

function Test-TlsFiles {
    param([Parameter(Mandatory)][string]$LiteralPath)

    $openssl = Find-OpenSsl
    $certificate = Join-Path $LiteralPath 'tls.crt'
    $privateKey = Join-Path $LiteralPath 'tls.key'
    $caCertificate = Join-Path $LiteralPath 'tls-ca.crt'
    $keyText = [System.IO.File]::ReadAllText($privateKey, [System.Text.Encoding]::ASCII)
    if ($keyText -match '(?im)^-----BEGIN ENCRYPTED PRIVATE KEY-----$' -or
        $keyText -match '(?im)^(Proc-Type:\s*4,ENCRYPTED|DEK-Info:)') {
        throw 'The TLS private key must be an unencrypted PEM key.'
    }
    $keyText = $null
    Invoke-OpenSsl $openssl @('pkey', '-in', $privateKey, '-passin', 'pass:', '-check', '-noout') `
        'The TLS private key is invalid or encrypted.'
    Invoke-OpenSsl $openssl @('x509', '-in', $certificate, '-noout', '-checkhost', $HostName) `
        'The TLS certificate SAN does not cover the required hostname.'
    Invoke-OpenSsl $openssl @('x509', '-in', $certificate, '-noout', '-checkend', '604800') `
        'The TLS certificate expires in less than seven days.'

    $certificateText = [System.IO.File]::ReadAllText($certificate, [System.Text.Encoding]::ASCII)
    $blocks = @([regex]::Matches(
        $certificateText,
        '(?ms)^-----BEGIN CERTIFICATE-----\r?\n.*?^-----END CERTIFICATE-----\r?\n?'
    ))
    if ($blocks.Count -lt 1 -or (($blocks | ForEach-Object { $_.Value }) -join '').Trim() -cne $certificateText.Trim()) {
        throw 'tls.crt is not an exact PEM certificate chain.'
    }
    $chainPath = $null
    try {
        $verifyArguments = @(
            'verify', '-x509_strict', '-purpose', 'sslserver',
            '-CAfile', $caCertificate, '-verify_hostname', $HostName
        )
        if ($blocks.Count -gt 1) {
            $chainPath = Join-Path ([System.IO.Path]::GetTempPath()) `
                ('pm-tls-chain-' + [Guid]::NewGuid().ToString('N') + '.pem')
            $chainText = ($blocks | Select-Object -Skip 1 | ForEach-Object { $_.Value }) -join ''
            [System.IO.File]::WriteAllText(
                $chainPath,
                $chainText,
                (New-Object System.Text.UTF8Encoding($false))
            )
            $verifyArguments += @('-untrusted', $chainPath)
        }
        $verifyArguments += $certificate
        Invoke-OpenSsl $openssl $verifyArguments `
            'The TLS certificate chain does not verify strictly against tls-ca.crt.'

        $minimumValidEpoch = [DateTimeOffset]::UtcNow.AddSeconds(604800).ToUnixTimeSeconds().ToString(
            [Globalization.CultureInfo]::InvariantCulture
        )
        $futureVerifyArguments = @(
            'verify', '-x509_strict', '-purpose', 'sslserver',
            '-attime', $minimumValidEpoch,
            '-CAfile', $caCertificate, '-verify_hostname', $HostName
        )
        if ($blocks.Count -gt 1) {
            $futureVerifyArguments += @('-untrusted', $chainPath)
        }
        $futureVerifyArguments += $certificate
        Invoke-OpenSsl $openssl $futureVerifyArguments `
            'The TLS certificate chain expires in less than seven days.'
    }
    finally {
        if ($null -ne $chainPath) {
            Remove-Item -LiteralPath $chainPath -Force -ErrorAction SilentlyContinue
        }
        $certificateText = $null
    }
    $certificatePublicKey = Invoke-OpenSsl $openssl `
        @('x509', '-in', $certificate, '-pubkey', '-noout') `
        'Cannot read the TLS certificate public key.' -Capture
    $privatePublicKey = Invoke-OpenSsl $openssl `
        @('pkey', '-in', $privateKey, '-passin', 'pass:', '-pubout') `
        'Cannot derive the TLS private-key public key.' -Capture
    if ($certificatePublicKey -cne $privatePublicKey) {
        throw 'The TLS certificate and private key do not match.'
    }
    $certificatePublicKey = $null
    $privatePublicKey = $null
}

function Test-SameFile {
    param(
        [Parameter(Mandatory)][string]$First,
        [Parameter(Mandatory)][string]$Second
    )

    return (Get-FileHash -LiteralPath $First -Algorithm SHA256).Hash -ceq `
        (Get-FileHash -LiteralPath $Second -Algorithm SHA256).Hash
}

$source = [System.IO.Path]::GetFullPath($SourceDirectory)
if ($source.StartsWith('\\', [StringComparison]::Ordinal)) {
    throw 'SourceDirectory must be a trusted local path, not a UNC path.'
}
Get-ExactDirectoryFiles -LiteralPath $source
Test-SecretFormats -LiteralPath $source
Test-TlsFiles -LiteralPath $source

$driveName = 'PMV2' + [Guid]::NewGuid().ToString('N').Substring(0, 4)
$uncRoot = "\\$TrueNasAddress\$ShareName"
$driveParameters = @{
    Name = $driveName
    PSProvider = 'FileSystem'
    Root = $uncRoot
    Scope = 'Script'
}
if ($null -ne $Credential) {
    $driveParameters.Credential = $Credential
}
$stageRoot = $null
$remoteRoot = $null
$marker = $null
$movedNames = [System.Collections.Generic.List[string]]::new()
$stagingSucceeded = $false
try {
    [void](New-PSDrive @driveParameters)
    $remoteRoot = "${driveName}:\"
    $remoteItems = @(Get-ChildItem -LiteralPath $remoteRoot -Force)
    if (@($remoteItems | Where-Object {
        $_.PSIsContainer -or
        ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    }).Count -ne 0) {
        throw 'The SMB destination contains a directory or reparse point.'
    }
    $remoteNames = @($remoteItems | ForEach-Object { $_.Name })
    if ($remoteItems.Count -eq 13 -and
        @($RequiredNames | Where-Object { $remoteNames -cnotcontains $_ }).Count -eq 0) {
        foreach ($name in $RequiredNames) {
            if (-not (Test-SameFile (Join-Path $source $name) (Join-Path $remoteRoot $name))) {
                throw 'The destination already contains a differing secret file.'
            }
        }
        Write-Host 'The exact 13 files are already staged and byte-for-byte verified.'
        return
    }
    if ($remoteItems.Count -ne 0) {
        throw 'The SMB destination must be empty or contain the exact identical 13-file set.'
    }

    $stageRoot = Join-Path $remoteRoot ('.powermeter-stage-' + [Guid]::NewGuid().ToString('N'))
    [void][System.IO.Directory]::CreateDirectory($stageRoot)
    foreach ($name in $RequiredNames) {
        $temporary = Join-Path $stageRoot $name
        [System.IO.File]::Copy((Join-Path $source $name), $temporary, $false)
        if (-not (Test-SameFile (Join-Path $source $name) $temporary)) {
            throw 'A staged SMB copy did not verify.'
        }
    }
    foreach ($name in $RequiredNames) {
        [System.IO.File]::Move((Join-Path $stageRoot $name), (Join-Path $remoteRoot $name))
        [void]$movedNames.Add($name)
    }
    [System.IO.Directory]::Delete($stageRoot)
    $stageRoot = $null

    $marker = Join-Path $remoteRoot '.powermeter-stage-complete'
    [System.IO.File]::WriteAllText(
        $marker,
        "pm-secret-staging/1.0.0`n13 files`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    foreach ($name in $RequiredNames) {
        if (-not (Test-SameFile (Join-Path $source $name) (Join-Path $remoteRoot $name))) {
            throw 'The final SMB destination did not verify.'
        }
    }
    Remove-Item -LiteralPath $marker -Force
    $finalNames = @(Get-ChildItem -LiteralPath $remoteRoot -Force | ForEach-Object { $_.Name })
    if ($finalNames.Count -ne 13 -or
        @($RequiredNames | Where-Object { $finalNames -cnotcontains $_ }).Count -ne 0) {
        throw 'The final SMB destination is not the exact 13-file set.'
    }
    $stagingSucceeded = $true
    Write-Host 'Staging passed: exactly 13 secret/TLS files were preserved and verified.'
}
finally {
    if (-not $stagingSucceeded -and $null -ne $remoteRoot) {
        if ($null -ne $marker -and (Test-Path -LiteralPath $marker)) {
            Remove-Item -LiteralPath $marker -Force -ErrorAction Stop
        }
        foreach ($movedName in $movedNames) {
            $movedPath = Join-Path $remoteRoot $movedName
            if (Test-Path -LiteralPath $movedPath) {
                Remove-Item -LiteralPath $movedPath -Force -ErrorAction Stop
            }
            if (Test-Path -LiteralPath $movedPath) {
                throw 'Failed to clean a file published by this staging invocation.'
            }
        }
    }
    if ($null -ne $stageRoot -and (Test-Path -LiteralPath $stageRoot -PathType Container)) {
        foreach ($name in $RequiredNames) {
            Remove-Item -LiteralPath (Join-Path $stageRoot $name) -Force -ErrorAction SilentlyContinue
        }
        [System.IO.Directory]::Delete($stageRoot, $false)
    }
    Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
}
