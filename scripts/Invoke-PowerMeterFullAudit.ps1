#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$ApplySafeFixes,
    [switch]$RunDisposableIntegration,
    [switch]$StrictFullAudit,
    [switch]$SkipBrowser,
    [switch]$SkipContainers,
    [switch]$SkipFirmware
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot "pyproject.toml") -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $repositoryRoot "frontend/package.json") -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $repositoryRoot "compose.yaml") -PathType Leaf)) {
    throw "The PowerMeter V2 repository root could not be resolved from $PSScriptRoot."
}

$startedAt = [DateTimeOffset]::UtcNow
$runId = $startedAt.ToString("yyyyMMddTHHmmssZ") + "-" + ([Guid]::NewGuid().ToString("N").Substring(0, 8))
$artifactRoot = Join-Path $repositoryRoot "artifacts/audit/$runId"
[void][System.IO.Directory]::CreateDirectory($artifactRoot)

$results = [System.Collections.Generic.List[object]]::new()
$composeProject = "pmv2audit" + $runId.ToLowerInvariant().Replace("t", "").Replace("z", "").Replace("-", "")
$composeApiImage = "pmv2-audit-api-$($runId.ToLowerInvariant()):audit"
$composeFrontendImage = "pmv2-audit-frontend-$($runId.ToLowerInvariant()):audit"
$composeStarted = $false
$dockerLocalApproved = $false
$dockerApprovalResultRecorded = $false
$approvedDockerEndpoint = $null
$composeOverridePath = $null
$disposableSecretPaths = [System.Collections.Generic.List[string]]::new()
$composeRuntimeAttempted = $false
$composeRuntimeError = $null
$composeCleanupError = $null
$composeRuntimeSeconds = 0
$composeRuntimeLogPath = $null
$initialCommit = "unavailable"
$initialBranch = "unavailable"
$initialStatus = @("unavailable")

function Resolve-AuditTool {
    param(
        [Parameter(Mandatory)][string[]]$Candidates
    )

    foreach ($candidate in $Candidates) {
        if ([System.IO.Path]::IsPathRooted($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
        $command = Get-Command $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) {
            return $command.Source
        }
    }
    return $null
}

function Test-SameAuditPath {
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )

    try {
        $leftPath = [System.IO.Path]::GetFullPath($Left).TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $rightPath = [System.IO.Path]::GetFullPath($Right).TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    }
    catch {
        return $false
    }
    return $leftPath.Equals($rightPath, [StringComparison]::OrdinalIgnoreCase)
}

function Test-AuditPathWithin {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )

    try {
        $fullPath = [System.IO.Path]::GetFullPath($Path)
        $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    }
    catch {
        return $false
    }
    $prefix = $fullRoot + [System.IO.Path]::DirectorySeparatorChar
    return $fullPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Get-EspIdfActivationEnvironment {
    param(
        [Parameter(Mandatory)][string]$ActivationScript,
        [Parameter(Mandatory)][string]$IdfRoot,
        [Parameter(Mandatory)][string]$IdfToolsRoot,
        [Parameter(Mandatory)][string]$PythonPath,
        [Parameter(Mandatory)][string]$RequiredVersion
    )

    $activationOutput = @(& $ActivationScript -e 2>&1 6>&1)
    $values = @{}
    foreach ($item in $activationOutput) {
        if ($item -is [System.Management.Automation.ErrorRecord]) {
            throw "The EIM activation script failed while reporting its environment."
        }
        $line = $item.ToString()
        if ($line -notmatch '^([A-Z][A-Z0-9_.]*)=(.*)$') {
            continue
        }
        $name = $Matches[1]
        if ($values.ContainsKey($name)) {
            throw "The EIM activation script reported duplicate '$name' values."
        }
        $values[$name] = $Matches[2]
    }

    foreach ($requiredName in @(
        "PATH", "IDF_PATH", "IDF_TOOLS_PATH", "IDF_PYTHON_ENV_PATH", "IDF_VERSION"
    )) {
        if (-not $values.ContainsKey($requiredName) -or
            [string]::IsNullOrWhiteSpace($values[$requiredName])) {
            throw "The EIM activation script did not report required '$requiredName' state."
        }
    }
    $pythonEnvironment = Split-Path -Parent (Split-Path -Parent $PythonPath)
    if (-not (Test-SameAuditPath -Left $values["IDF_PATH"] -Right $IdfRoot) -or
        -not (Test-SameAuditPath -Left $values["IDF_TOOLS_PATH"] -Right $IdfToolsRoot) -or
        -not (Test-SameAuditPath -Left $values["IDF_PYTHON_ENV_PATH"] -Right $pythonEnvironment) -or
        $values["IDF_VERSION"] -ne $RequiredVersion.TrimStart("v")) {
        throw "The EIM activation environment does not match the selected $RequiredVersion installation."
    }

    $allowedNames = @(
        "PATH", "IDF_PATH", "IDF_TOOLS_PATH", "IDF_PYTHON_ENV_PATH", "IDF_VERSION",
        "ESP_IDF_VERSION", "IDF_COMPONENT_LOCAL_STORAGE_URL", "ESP_ROM_ELF_DIR",
        "OPENOCD_SCRIPTS", "IDF_CCACHE_ENABLE", "ESP_CLANG_LIBS_PATH"
    )
    $environment = @{}
    foreach ($name in $allowedNames) {
        if ($values.ContainsKey($name)) {
            $environment[$name] = $values[$name]
        }
    }
    $currentPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
    if ($currentPath) {
        $environment["PATH"] = $environment["PATH"].TrimEnd(";") +
            [System.IO.Path]::PathSeparator + $currentPath
    }
    return $environment
}

function Resolve-EspIdfEimToolchain {
    param(
        [string]$RequiredVersion = "v6.0.2",
        [string[]]$MetadataCandidates = @()
    )

    $candidatePaths = [System.Collections.Generic.List[string]]::new()
    foreach ($candidate in $MetadataCandidates) {
        if ($candidate) { $candidatePaths.Add($candidate) }
    }
    if ($candidatePaths.Count -eq 0) {
        $configuredToolsRoot = [Environment]::GetEnvironmentVariable("IDF_TOOLS_PATH", "Process")
        if ($configuredToolsRoot) {
            $candidatePaths.Add((Join-Path $configuredToolsRoot "eim_idf.json"))
        }
        if ($IsWindows) {
            $candidatePaths.Add("C:/Espressif/tools/eim_idf.json")
            $userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
            if ($userProfile) {
                $candidatePaths.Add((Join-Path $userProfile ".espressif/eim_idf.json"))
            }
        }
    }

    foreach ($metadataPath in @($candidatePaths | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { continue }
        try {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding utf8 | ConvertFrom-Json
            $matching = @($metadata.idfInstalled | Where-Object { $_.name -eq $RequiredVersion })
            if ($matching.Count -eq 0) { continue }
            $selected = @($matching | Where-Object { $_.id -eq $metadata.idfSelectedId })
            if ($selected.Count -eq 1) {
                $installation = $selected[0]
            }
            elseif ($matching.Count -eq 1) {
                $installation = $matching[0]
            }
            else {
                continue
            }

            $idfRoot = [System.IO.Path]::GetFullPath([string]$installation.path)
            $idfToolsRoot = [System.IO.Path]::GetFullPath([string]$installation.idfToolsPath)
            $pythonPath = [System.IO.Path]::GetFullPath([string]$installation.python)
            $activationScript = [System.IO.Path]::GetFullPath([string]$installation.activationScript)
            $idfScript = Join-Path $idfRoot "tools/idf.py"
            $expectedActivationName = "Microsoft.$RequiredVersion.PowerShell_profile.ps1"
            if (-not (Test-SameAuditPath -Left (Split-Path -Parent $metadataPath) -Right $idfToolsRoot) -or
                -not (Test-AuditPathWithin -Path $pythonPath -Root $idfToolsRoot) -or
                -not (Test-AuditPathWithin -Path $activationScript -Root $idfToolsRoot) -or
                [System.IO.Path]::GetFileName($activationScript) -ne $expectedActivationName) {
                throw "EIM metadata paths do not match the selected tools root."
            }
            foreach ($requiredFile in @($pythonPath, $activationScript, $idfScript)) {
                if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
                    throw "EIM metadata references a missing file."
                }
            }

            $environment = Get-EspIdfActivationEnvironment `
                -ActivationScript $activationScript `
                -IdfRoot $idfRoot `
                -IdfToolsRoot $idfToolsRoot `
                -PythonPath $pythonPath `
                -RequiredVersion $RequiredVersion
            return [pscustomobject]@{
                FilePath = $pythonPath
                PrefixArguments = @($idfScript)
                Environment = $environment
                Source = "EIM metadata: $([System.IO.Path]::GetFullPath($metadataPath))"
            }
        }
        catch {
            continue
        }
    }
    return $null
}

function Resolve-EspIdfAuditToolchain {
    $eimToolchain = Resolve-EspIdfEimToolchain -RequiredVersion "v6.0.2"
    if ($eimToolchain) { return $eimToolchain }

    $idfExecutable = Resolve-AuditTool -Candidates @("idf.py")
    if ($idfExecutable) {
        return [pscustomobject]@{
            FilePath = $idfExecutable
            PrefixArguments = @()
            Environment = @{}
            Source = "executable on PATH"
        }
    }
    return $null
}

function Invoke-WithTemporaryEnvironment {
    param(
        [Parameter(Mandatory)][scriptblock]$Action,
        [hashtable]$Variables = @{}
    )

    $saved = @{}
    foreach ($name in $Variables.Keys) {
        if ($name -notmatch '^[A-Z][A-Z0-9_.]*$' -or $name -match '^PM_') {
            throw "Temporary environment name '$name' is not permitted."
        }
        $processEnvironment = [Environment]::GetEnvironmentVariables("Process")
        $matchingName = @($processEnvironment.Keys | Where-Object {
            $_.ToString().Equals($name, [StringComparison]::OrdinalIgnoreCase)
        } | Select-Object -First 1)
        $saved[$name] = [pscustomobject]@{
            Exists = $matchingName.Count -eq 1
            Value = if ($matchingName.Count -eq 1) { $processEnvironment[$matchingName[0]] } else { $null }
        }
        [Environment]::SetEnvironmentVariable($name, [string]$Variables[$name], "Process")
    }
    try {
        & $Action
    }
    finally {
        foreach ($name in $saved.Keys) {
            $prior = $saved[$name]
            if ($prior.Exists) {
                [Environment]::SetEnvironmentVariable($name, $prior.Value, "Process")
            }
            else {
                [Environment]::SetEnvironmentVariable($name, $null, "Process")
                Microsoft.PowerShell.Management\Remove-Item `
                    -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            }
        }
    }
}

function ConvertTo-LogName {
    param([Parameter(Mandatory)][string]$Name)

    $safe = $Name.ToLowerInvariant() -replace '[^a-z0-9]+', '-'
    return $safe.Trim('-') + ".log"
}

function Add-AuditResult {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet("PASS", "FAIL", "WARNING", "SKIPPED", "PARTIAL")][string]$Status,
        [Parameter(Mandatory)][string]$Summary,
        [string]$LogPath,
        [double]$Seconds = 0
    )

    $results.Add([pscustomobject]@{
        Name = $Name
        Status = $Status
        Summary = $Summary
        Log = if ($LogPath) { [System.IO.Path]::GetRelativePath($artifactRoot, $LogPath) } else { "" }
        Seconds = [Math]::Round($Seconds, 2)
    })
    $color = switch ($Status) {
        "PASS" { "Green" }
        "FAIL" { "Red" }
        "PARTIAL" { "Red" }
        "WARNING" { "Yellow" }
        default { "DarkYellow" }
    }
    Write-Host ("[{0}] {1}: {2}" -f $Status, $Name, $Summary) -ForegroundColor $color
}

function Add-SkippedCriticalGate {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Summary
    )

    $status = if ($StrictFullAudit) { "PARTIAL" } else { "SKIPPED" }
    Add-AuditResult -Name $Name -Status $status -Summary $Summary
}

function Invoke-WithIsolatedPowerMeterEnvironment {
    param(
        [Parameter(Mandatory)][scriptblock]$Action,
        [hashtable]$OwnedVariables = @{ PM_ENV = "test" }
    )

    $saved = @{}
    foreach ($entry in @(Get-ChildItem Env:)) {
        if ($entry.Name -match '^PM_') {
            $saved[$entry.Name] = $entry.Value
            [Environment]::SetEnvironmentVariable($entry.Name, $null, "Process")
            Microsoft.PowerShell.Management\Remove-Item -LiteralPath "Env:$($entry.Name)" -ErrorAction SilentlyContinue
        }
    }
    foreach ($name in $OwnedVariables.Keys) {
        if ($name -notmatch '^PM_[A-Z][A-Z0-9_]*$') {
            throw "Runner-owned environment name '$name' is outside the PM_* namespace."
        }
        [Environment]::SetEnvironmentVariable($name, $OwnedVariables[$name], "Process")
    }
    try {
        & $Action
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env:)) {
            if ($entry.Name -match '^PM_') {
                [Environment]::SetEnvironmentVariable($entry.Name, $null, "Process")
                Microsoft.PowerShell.Management\Remove-Item -LiteralPath "Env:$($entry.Name)" -ErrorAction SilentlyContinue
            }
        }
        foreach ($name in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
        }
    }
}

function Invoke-WithAuditEnvironment {
    param(
        [Parameter(Mandatory)][scriptblock]$AuditAction,
        [hashtable]$Variables = @{}
    )

    Invoke-WithIsolatedPowerMeterEnvironment -Action {
        Invoke-WithTemporaryEnvironment -Variables $Variables -Action $AuditAction
    }
}

function Invoke-IsolatedNativeCapture {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    Invoke-WithIsolatedPowerMeterEnvironment -Action {
        $commandOutput = @(& $FilePath @Arguments 2>&1)
        [pscustomobject]@{ Output = $commandOutput; ExitCode = $LASTEXITCODE }
    }
}

function Assert-LocalDockerEndpoint {
    param([Parameter(Mandatory)][string]$DockerPath)

    $script:approvedDockerEndpoint = $null
    $dockerHostOverride = [Environment]::GetEnvironmentVariable("DOCKER_HOST", "Process")
    $contextName = ""
    if ($dockerHostOverride) {
        $endpoint = $dockerHostOverride.Trim()
        $source = "DOCKER_HOST"
    }
    else {
        $contextResult = Invoke-IsolatedNativeCapture -FilePath $DockerPath -Arguments @("context", "show")
        $contextOutput = @($contextResult.Output)
        if ($contextResult.ExitCode -ne 0 -or $contextOutput.Count -ne 1) {
            throw "Docker context could not be resolved; no Docker operation was attempted."
        }
        $contextName = $contextOutput[0].ToString().Trim()
        if (-not $contextName) {
            throw "Docker returned an empty context name; no Docker operation was attempted."
        }
        $endpointResult = Invoke-IsolatedNativeCapture -FilePath $DockerPath -Arguments @(
            "context", "inspect", $contextName, "--format", "{{.Endpoints.docker.Host}}"
        )
        $endpointOutput = @($endpointResult.Output)
        if ($endpointResult.ExitCode -ne 0 -or $endpointOutput.Count -ne 1) {
            throw "Docker endpoint for context '$contextName' could not be inspected; no Docker operation was attempted."
        }
        $endpoint = $endpointOutput[0].ToString().Trim()
        $source = "context:$contextName"
    }

    $isLocalNamedPipe = $endpoint -match '(?i)^npipe:////\./pipe/(?:docker_engine|dockerDesktopLinuxEngine)$'
    $isLocalUnixSocket = $endpoint -match '(?i)^unix:///.+\.sock$'
    if (-not $isLocalNamedPipe -and -not $isLocalUnixSocket) {
        throw "Refusing Docker endpoint '$endpoint' from $source. Only a local named pipe or local Unix socket is permitted."
    }

    $infoResult = Invoke-IsolatedNativeCapture -FilePath $DockerPath -Arguments @(
        "--host", $endpoint, "info", "--format", "{{.Name}}|{{.OperatingSystem}}"
    )
    $infoOutput = @($infoResult.Output)
    if ($infoResult.ExitCode -ne 0 -or $infoOutput.Count -ne 1 -or -not $infoOutput[0].ToString().Trim()) {
        throw "The approved local Docker endpoint is not reachable; no mutating Docker operation was attempted."
    }
    $script:approvedDockerEndpoint = $endpoint
    "source=$source"
    "endpoint=$endpoint"
    "daemon=$($infoOutput[0].ToString().Trim())"
}

function New-DisposableComposeInputs {
    $inputRoot = Join-Path $artifactRoot "disposable-compose"
    $secretRoot = Join-Path $inputRoot "secrets"
    [void][System.IO.Directory]::CreateDirectory($secretRoot)
    $passwordNames = @(
        "postgres_bootstrap_password",
        "postgres_migrator_password",
        "postgres_api_password",
        "postgres_worker_password",
        "postgres_backup_password",
        "postgres_restore_password"
    )
    $base64Names = @("session_secret", "field_encryption_key", "ota_manifest_key", "backup_key")
    $allPaths = @{}
    foreach ($name in $passwordNames + $base64Names) {
        $bytes = [byte[]]::new(32)
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        $value = if ($name -in $passwordNames) {
            [Convert]::ToHexString($bytes).ToLowerInvariant()
        }
        else {
            [Convert]::ToBase64String($bytes)
        }
        $path = Join-Path $secretRoot $name
        [System.IO.File]::WriteAllText($path, $value, [System.Text.UTF8Encoding]::new($false))
        $disposableSecretPaths.Add($path)
        $allPaths[$name] = $path
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    foreach ($name in @("tls_cert.pem", "tls_key.pem")) {
        $path = Join-Path $secretRoot $name
        [System.IO.File]::WriteAllText(
            $path,
            "disposable-audit-input-$([Guid]::NewGuid().ToString('N'))",
            [System.Text.UTF8Encoding]::new($false)
        )
        $disposableSecretPaths.Add($path)
        $allPaths[$name] = $path
    }

    $secretMap = [ordered]@{
        postgres_bootstrap_password = $allPaths["postgres_bootstrap_password"]
        postgres_migrator_password = $allPaths["postgres_migrator_password"]
        postgres_api_password = $allPaths["postgres_api_password"]
        postgres_worker_password = $allPaths["postgres_worker_password"]
        postgres_backup_password = $allPaths["postgres_backup_password"]
        postgres_restore_password = $allPaths["postgres_restore_password"]
        session_secret = $allPaths["session_secret"]
        field_encryption_key = $allPaths["field_encryption_key"]
        ota_manifest_key = $allPaths["ota_manifest_key"]
        backup_key = $allPaths["backup_key"]
        tls_cert = $allPaths["tls_cert.pem"]
        tls_key = $allPaths["tls_key.pem"]
    }
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("secrets:")
    foreach ($entry in $secretMap.GetEnumerator()) {
        $yamlPath = $entry.Value.Replace("\", "/").Replace("'", "''")
        $lines.Add("  $($entry.Key):")
        $lines.Add("    file: '$yamlPath'")
    }
    $script:composeOverridePath = Join-Path $inputRoot "compose.audit-secrets.yaml"
    $lines | Set-Content -LiteralPath $composeOverridePath -Encoding utf8NoBOM
    $disposableSecretPaths.Add($composeOverridePath)
    return $composeOverridePath
}

function Remove-DisposableComposeInputs {
    $inputRoot = [System.IO.Path]::GetFullPath((Join-Path $artifactRoot "disposable-compose"))
    $safePrefix = $inputRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    foreach ($path in @($disposableSecretPaths)) {
        $fullPath = [System.IO.Path]::GetFullPath($path)
        if (-not $fullPath.StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a disposable input outside the run artifact directory."
        }
        if ([System.IO.File]::Exists($fullPath)) {
            [System.IO.File]::Delete($fullPath)
        }
    }
    $secretRoot = Join-Path $inputRoot "secrets"
    if ([System.IO.Directory]::Exists($secretRoot) -and
        [System.IO.Directory]::GetFileSystemEntries($secretRoot).Count -eq 0) {
        [System.IO.Directory]::Delete($secretRoot)
    }
    if ([System.IO.Directory]::Exists($inputRoot) -and
        [System.IO.Directory]::GetFileSystemEntries($inputRoot).Count -eq 0) {
        [System.IO.Directory]::Delete($inputRoot)
    }
    $disposableSecretPaths.Clear()
    $script:composeOverridePath = $null
}

function Invoke-AuditCommand {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $repositoryRoot,
        [hashtable]$EnvironmentVariables = @{},
        [switch]$Optional
    )

    $logPath = Join-Path $artifactRoot (ConvertTo-LogName $Name)
    if (-not $FilePath) {
        $status = if ($Optional) { "SKIPPED" } else { "FAIL" }
        "Required executable was not found." | Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        Add-AuditResult -Name $Name -Status $status -Summary "required executable was not found" -LogPath $logPath
        return
    }

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $displayArguments = $Arguments | ForEach-Object {
        if ($_ -match '\s') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }
    $header = @(
        "working_directory=$WorkingDirectory",
        "command=$FilePath $($displayArguments -join ' ')",
        "started_at=$([DateTimeOffset]::UtcNow.ToString('O'))",
        ""
    )
    $header | Set-Content -LiteralPath $logPath -Encoding utf8NoBOM

    $locationPushed = $false
    try {
        Push-Location -LiteralPath $WorkingDirectory
        $locationPushed = $true
        $execution = Invoke-WithAuditEnvironment -Variables $EnvironmentVariables -AuditAction {
                $commandOutput = @(& $FilePath @Arguments 2>&1)
                [pscustomobject]@{
                    Output = $commandOutput
                    ExitCode = $LASTEXITCODE
                }
        }
        $exitCode = $execution.ExitCode
        @($execution.Output) | ForEach-Object { $_.ToString() } |
            Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
    }
    catch {
        $exitCode = 1
        $_.Exception.Message | Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
    }
    finally {
        if ($locationPushed) { Pop-Location }
        $watch.Stop()
    }

    "`nexit_code=$exitCode" | Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
    if ($exitCode -eq 0) {
        Add-AuditResult -Name $Name -Status "PASS" -Summary "completed successfully" -LogPath $logPath -Seconds $watch.Elapsed.TotalSeconds
    }
    else {
        Add-AuditResult -Name $Name -Status "FAIL" -Summary "exited with code $exitCode" -LogPath $logPath -Seconds $watch.Elapsed.TotalSeconds
    }
}

function Invoke-AuditScriptBlock {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Action,
        [hashtable]$EnvironmentVariables = @{},
        [ValidateSet("FAIL", "WARNING")][string]$FailureStatus = "FAIL"
    )

    $logPath = Join-Path $artifactRoot (ConvertTo-LogName $Name)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $output = @(Invoke-WithAuditEnvironment `
            -Variables $EnvironmentVariables -AuditAction $Action)
        $output | ForEach-Object { $_.ToString() } |
            Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        $watch.Stop()
        Add-AuditResult -Name $Name -Status "PASS" -Summary "completed successfully" -LogPath $logPath -Seconds $watch.Elapsed.TotalSeconds
    }
    catch {
        $watch.Stop()
        @($_.Exception.Message, $_.ScriptStackTrace) |
            Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        Add-AuditResult -Name $Name -Status $FailureStatus -Summary $_.Exception.Message -LogPath $logPath -Seconds $watch.Elapsed.TotalSeconds
    }
}

function Assert-RunnerOwnedPytestBaseTemp {
    param([Parameter(Mandatory)][string]$Path)

    $resolvedArtifactRoot = [System.IO.Path]::GetFullPath($artifactRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (-not [System.IO.Directory]::Exists($resolvedArtifactRoot)) {
        throw "The audit artifact root does not exist; refusing pytest temporary-directory cleanup."
    }
    $artifactInfo = [System.IO.DirectoryInfo]::new($resolvedArtifactRoot)
    if (($artifactInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The audit artifact root is a reparse point; refusing pytest temporary-directory cleanup."
    }

    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $parentPath = [System.IO.Path]::GetDirectoryName($resolvedPath)
    $leafName = [System.IO.Path]::GetFileName($resolvedPath)
    if (-not $parentPath.Equals($resolvedArtifactRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $leafName -notmatch '^pytest-basetemp-[0-9a-f]{32}$') {
        throw "Refusing pytest temporary-directory cleanup outside the exact runner-owned path."
    }
    return $resolvedPath
}

function New-RunnerOwnedPytestBaseTemp {
    $candidate = Join-Path $artifactRoot "pytest-basetemp-$([Guid]::NewGuid().ToString('N'))"
    $resolvedPath = Assert-RunnerOwnedPytestBaseTemp -Path $candidate
    if ([System.IO.Directory]::Exists($resolvedPath) -or [System.IO.File]::Exists($resolvedPath)) {
        throw "The generated pytest temporary directory already exists; refusing to reuse it."
    }
    return $resolvedPath
}

function Remove-RunnerOwnedPytestBaseTemp {
    param(
        [Parameter(Mandatory)][string]$Path,
        [scriptblock]$DeleteAction
    )

    $resolvedPath = Assert-RunnerOwnedPytestBaseTemp -Path $Path
    if ([System.IO.File]::Exists($resolvedPath)) {
        throw "The runner-owned pytest path became a file; refusing cleanup."
    }
    if (-not [System.IO.Directory]::Exists($resolvedPath)) {
        "not_present=$resolvedPath"
        return
    }
    $pathInfo = [System.IO.DirectoryInfo]::new($resolvedPath)
    if (($pathInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The runner-owned pytest path became a reparse point; refusing cleanup."
    }

    if ($DeleteAction) {
        & $DeleteAction $resolvedPath
    }
    else {
        [System.IO.Directory]::Delete($resolvedPath, $true)
    }
    if ([System.IO.Directory]::Exists($resolvedPath) -or [System.IO.File]::Exists($resolvedPath)) {
        throw "The runner-owned pytest temporary directory still exists after cleanup."
    }
    "removed=$resolvedPath"
}

function Invoke-PythonTestAudit {
    param(
        [string]$PythonPath,
        [scriptblock]$CleanupAction
    )

    $pytestBaseTemp = New-RunnerOwnedPytestBaseTemp
    try {
        Invoke-AuditCommand -Name "Python unit and integration tests" -FilePath $PythonPath `
            -Arguments @("-m", "pytest", "-ra", "--basetemp", $pytestBaseTemp)
    }
    finally {
        $requestedCleanupAction = $CleanupAction
        Invoke-AuditScriptBlock -Name "Python test temporary-directory cleanup" -Action {
            if ($requestedCleanupAction) {
                Remove-RunnerOwnedPytestBaseTemp `
                    -Path $pytestBaseTemp -DeleteAction $requestedCleanupAction
            }
            else {
                Remove-RunnerOwnedPytestBaseTemp -Path $pytestBaseTemp
            }
        }
    }
}

function Get-PinnedGatewayGoImage {
    return "golang:1.26.6-alpine3.23@sha256:5978cc992ad5ef96a7469713c8af849c1433824761ce3be2c56381403cd8d9a3"
}

function Invoke-LocalDockerApprovalGate {
    param([Parameter(Mandatory)][string]$DockerPath)

    if ($script:dockerApprovalResultRecorded) { return }
    $script:dockerApprovalResultRecorded = $true
    Invoke-AuditScriptBlock -Name "Local Docker endpoint guard" -Action {
        Assert-LocalDockerEndpoint -DockerPath $DockerPath
        $script:dockerLocalApproved = $true
    }
}

function New-GatewayGoDockerArguments {
    param(
        [Parameter(Mandatory)][string]$GatewayRoot,
        [Parameter(Mandatory)][string[]]$GoArguments
    )

    if (-not $script:dockerLocalApproved -or
        [string]::IsNullOrWhiteSpace($script:approvedDockerEndpoint)) {
        throw "A fail-closed local Docker endpoint approval is required before the Go fallback."
    }
    $resolvedGatewayRoot = [System.IO.Path]::GetFullPath($GatewayRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedGatewayRoot "go.mod") -PathType Leaf)) {
        throw "The gateway source root is invalid; no Docker fallback was attempted."
    }
    $mount = "type=bind,source=$resolvedGatewayRoot,destination=/workspace,readonly"
    return @(
        "--host", $script:approvedDockerEndpoint,
        "run", "--rm", "--pull=always",
        "--mount", $mount,
        "--workdir", "/workspace",
        "--env", "GOFLAGS=-mod=readonly",
        "--env", "GOTOOLCHAIN=local",
        (Get-PinnedGatewayGoImage),
        "go"
    ) + $GoArguments
}

function Invoke-GatewayGoChecks {
    param(
        [string]$GoPath,
        [string]$DockerPath,
        [Parameter(Mandatory)][string]$GatewayRoot,
        [switch]$ContainersDisabled
    )

    $checks = @(
        [pscustomobject]@{ Name = "Gateway Go module verification"; Arguments = @("mod", "verify") },
        [pscustomobject]@{ Name = "Gateway Go dependency graph"; Arguments = @("list", "-mod=readonly", "-m", "all") },
        [pscustomobject]@{ Name = "Gateway Go tests"; Arguments = @("test", "./...") },
        [pscustomobject]@{ Name = "Gateway Go vet"; Arguments = @("vet", "./...") }
    )
    if ($GoPath) {
        foreach ($check in $checks) {
            Invoke-AuditCommand -Name $check.Name -FilePath $GoPath `
                -Arguments $check.Arguments -WorkingDirectory $GatewayRoot
        }
        return
    }

    if ($ContainersDisabled) {
        $reason = "host Go is unavailable and -SkipContainers disables the pinned local-Docker fallback"
    }
    elseif (-not $DockerPath) {
        $reason = "host Go is unavailable and Docker was not found for the pinned fallback"
    }
    else {
        Invoke-LocalDockerApprovalGate -DockerPath $DockerPath
        if ($script:dockerLocalApproved) {
            foreach ($check in $checks) {
                $dockerArguments = @(New-GatewayGoDockerArguments `
                    -GatewayRoot $GatewayRoot -GoArguments $check.Arguments)
                Invoke-AuditCommand -Name $check.Name -FilePath $DockerPath `
                    -Arguments $dockerArguments -WorkingDirectory $GatewayRoot
            }
            return
        }
        $reason = "host Go is unavailable and the Docker endpoint was not approved as local"
    }

    foreach ($check in $checks) {
        Add-AuditResult -Name $check.Name -Status "FAIL" -Summary $reason
    }
}

function Get-TrackedAuditFiles {
    $rootCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @(
        "-C", $repositoryRoot, "ls-files", "--cached", "--others", "--exclude-standard"
    )
    if ($rootCapture.ExitCode -ne 0) { throw "Git failed to inventory the server repository." }
    $rootFiles = @($rootCapture.Output)
    foreach ($path in $rootFiles) {
        [pscustomobject]@{ Scope = "server"; RelativePath = $path; FullPath = Join-Path $repositoryRoot $path }
    }

    $firmwareRoot = Join-Path $repositoryRoot "power-monitor-sensor-headless"
    if (Test-Path -LiteralPath (Join-Path $firmwareRoot ".git")) {
        $firmwareCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @(
            "-C", $firmwareRoot, "ls-files", "--cached", "--others", "--exclude-standard"
        )
        if ($firmwareCapture.ExitCode -ne 0) { throw "Git failed to inventory the firmware repository." }
        $firmwareFiles = @($firmwareCapture.Output)
        foreach ($path in $firmwareFiles) {
            [pscustomobject]@{
                Scope = "firmware"
                RelativePath = "power-monitor-sensor-headless/$path"
                FullPath = Join-Path $firmwareRoot $path
            }
        }
    }
}

function Test-IsTextAuditFile {
    param([Parameter(Mandatory)][string]$Path)

    $binaryExtensions = @(
        ".7z", ".bin", ".bmp", ".db", ".dll", ".elf", ".exe", ".gif", ".gz",
        ".ico", ".jpeg", ".jpg", ".map", ".pdf", ".png", ".pyo", ".pyc",
        ".sqlite", ".tar", ".tgz", ".webp", ".woff", ".woff2", ".zip"
    )
    return [System.IO.Path]::GetExtension($Path).ToLowerInvariant() -notin $binaryExtensions
}

function Assert-AuditEntryReadable {
    param([Parameter(Mandatory)]$Entry)

    if (-not [System.IO.File]::Exists($Entry.FullPath)) {
        throw "First-party inventory entry is missing or unreadable: $($Entry.RelativePath)"
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Entry.FullPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
        )
    }
    catch {
        throw "First-party inventory entry is missing or unreadable: $($Entry.RelativePath)"
    }
    finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Invoke-TrackedPatternScan {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][hashtable]$Rules,
        [scriptblock]$Include = { param($entry) $true },
        [scriptblock]$IgnoreFinding = { param($entry, $lineNumber, $line, $ruleName) $false },
        [ValidateSet("FAIL", "WARNING")][string]$FindingStatus = "FAIL"
    )

    $logPath = Join-Path $artifactRoot (ConvertTo-LogName $Name)
    $findings = [System.Collections.Generic.List[string]]::new()
    foreach ($entry in @(Get-TrackedAuditFiles)) {
        if (-not (& $Include $entry) -or -not (Test-IsTextAuditFile -Path $entry.FullPath)) {
            continue
        }
        Assert-AuditEntryReadable -Entry $entry
        $lineNumber = 0
        foreach ($line in [System.IO.File]::ReadLines($entry.FullPath)) {
            $lineNumber++
            foreach ($rule in $Rules.GetEnumerator()) {
                if ($line -match $rule.Value -and
                    -not (& $IgnoreFinding $entry $lineNumber $line $rule.Key)) {
                    $findings.Add("$($entry.RelativePath):${lineNumber}:$($rule.Key)")
                }
            }
        }
    }

    if ($findings.Count -eq 0) {
        "No findings." | Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        Add-AuditResult -Name $Name -Status "PASS" -Summary "no findings" -LogPath $logPath
    }
    else {
        $findings | Sort-Object -Unique | Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        Add-AuditResult -Name $Name -Status $FindingStatus -Summary "$($findings.Count) finding(s); values were not logged" -LogPath $logPath
    }
}

function Write-AuditInventory {
    $inventoryPath = Join-Path $artifactRoot "first-party-inventory.tsv"
    $entries = @(Get-TrackedAuditFiles | Sort-Object Scope, RelativePath)
    foreach ($entry in $entries) { Assert-AuditEntryReadable -Entry $entry }
    @("scope`tclassification`tpath") + @($entries | ForEach-Object {
        $classification = if ($_.RelativePath -match '(?i)(package-lock\.json$|openapi\.json$|snapshots/.+\.png$)') {
            "generated-reviewed-input"
        }
        else {
            "first-party-reviewed"
        }
        "$($_.Scope)`t$classification`t$($_.RelativePath)"
    }) | Set-Content -LiteralPath $inventoryPath -Encoding utf8NoBOM
    Add-AuditResult -Name "First-party file inventory" -Status "PASS" -Summary "$($entries.Count) tracked files inventoried" -LogPath $inventoryPath
}

function Invoke-EnvironmentDocumentationScan {
    $logPath = Join-Path $artifactRoot "environment-variable-documentation.log"
    $codeNames = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $documentedNames = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($entry in @(Get-TrackedAuditFiles)) {
        if ($entry.Scope -ne "server" -or -not (Test-IsTextAuditFile $entry.FullPath)) {
            continue
        }
        Assert-AuditEntryReadable -Entry $entry
        $text = [System.IO.File]::ReadAllText($entry.FullPath)
        foreach ($match in [regex]::Matches($text, '\bPM_[A-Z][A-Z0-9_]*\b')) {
            [void]$codeNames.Add($match.Value)
        }
        if ($entry.RelativePath -match '^(\.env\.example|README\.md|docs/|deploy/)') {
            foreach ($match in [regex]::Matches($text, '\bPM_[A-Z][A-Z0-9_]*\b')) {
                [void]$documentedNames.Add($match.Value)
            }
        }
    }
    $missing = @($codeNames | Where-Object { -not $documentedNames.Contains($_) } | Sort-Object)
    @(
        "referenced=$($codeNames.Count)",
        "documented=$($documentedNames.Count)",
        "missing=$($missing.Count)",
        "",
        $missing
    ) | Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
    if ($missing.Count -eq 0) {
        Add-AuditResult -Name "Environment-variable documentation" -Status "PASS" -Summary "all $($codeNames.Count) PM_* names are documented" -LogPath $logPath
    }
    else {
        Add-AuditResult -Name "Environment-variable documentation" -Status "WARNING" -Summary "$($missing.Count) referenced PM_* name(s) need review" -LogPath $logPath
    }
}

function Invoke-DuplicateIdScan {
    $logPath = Join-Path $artifactRoot "duplicate-ui-identifiers.log"
    $occurrences = @{}
    $frontendRoot = Join-Path $repositoryRoot "frontend/src"
    foreach ($file in Get-ChildItem -LiteralPath $frontendRoot -Recurse -File -Include *.tsx,*.jsx,*.html) {
        $lineNumber = 0
        foreach ($line in [System.IO.File]::ReadLines($file.FullName)) {
            $lineNumber++
            foreach ($match in [regex]::Matches($line, '\bid\s*=\s*["'']([^"'']+)["'']')) {
                $id = $match.Groups[1].Value
                if (-not $occurrences.ContainsKey($id)) { $occurrences[$id] = @() }
                $occurrences[$id] += "$([System.IO.Path]::GetRelativePath($repositoryRoot, $file.FullName)):$lineNumber"
            }
        }
    }
    $duplicates = @($occurrences.GetEnumerator() | Where-Object { $_.Value.Count -gt 1 } | Sort-Object Name)
    if ($duplicates.Count -eq 0) {
        "No duplicate literal IDs." | Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        Add-AuditResult -Name "Duplicate UI identifiers" -Status "PASS" -Summary "no duplicate literal IDs" -LogPath $logPath
    }
    else {
        $duplicates | ForEach-Object { "$($_.Name)`t$($_.Value -join ', ')" } |
            Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        Add-AuditResult -Name "Duplicate UI identifiers" -Status "WARNING" -Summary "$($duplicates.Count) duplicate literal ID(s) require rendered-DOM review" -LogPath $logPath
    }
}

function Invoke-TrueNasPathScan {
    $obsoleteTrueNasRootPattern = [regex]::Escape('/mnt/Apps/' + 'Power') + '(?!MeterV2)'
    Invoke-TrackedPatternScan -Name "TrueNAS obsolete-path scan" -Rules @{
        "obsolete-legacy-root" = $obsoleteTrueNasRootPattern
        "trailing-space-after-root" = '/mnt/Apps/PowerMeterV2[ \t]+(?=$|["''`])'
    }
}

function Invoke-DisposableComposeCleanup {
    param([Parameter(Mandatory)][string]$LogPath)

    if (-not $composeStarted) {
        return
    }
    Assert-LocalDockerEndpoint -DockerPath $docker | Out-Null
    $cleanupComposeArgs = @(
        "--host", $approvedDockerEndpoint, "compose", "-p", $composeProject,
        "-f", (Join-Path $repositoryRoot "compose.yaml"),
        "-f", (Join-Path $repositoryRoot "compose.dev.yaml")
    )
    if ($composeOverridePath) { $cleanupComposeArgs += @("-f", $composeOverridePath) }

    $cleanupLocationPushed = $false
    try {
        Push-Location -LiteralPath $repositoryRoot
        $cleanupLocationPushed = $true
        $cleanup = Invoke-WithIsolatedPowerMeterEnvironment -OwnedVariables @{
            PM_ENV = "test"
            PM_API_IMAGE = $composeApiImage
            PM_FRONTEND_IMAGE = $composeFrontendImage
        } -Action {
            $downOutput = @(& $docker @cleanupComposeArgs down --volumes --remove-orphans 2>&1)
            $downExitCode = $LASTEXITCODE
            $imageOutput = [System.Collections.Generic.List[string]]::new()
            $imageFailure = $null
            if ($downExitCode -eq 0) {
                foreach ($imageName in @($composeApiImage, $composeFrontendImage)) {
                    $listOutput = @(
                        & $docker --host $approvedDockerEndpoint image ls --quiet --no-trunc `
                            --filter "reference=$imageName" 2>&1
                    )
                    $listExitCode = $LASTEXITCODE
                    foreach ($line in $listOutput) { $imageOutput.Add($line.ToString()) }
                    if ($listExitCode -ne 0) {
                        $imageFailure = "docker image ls failed for runner-owned image '$imageName' with code $listExitCode"
                        break
                    }
                    if (@($listOutput | Where-Object { $_.ToString().Trim() }).Count -gt 0) {
                        $removeOutput = @(
                            & $docker --host $approvedDockerEndpoint image rm $imageName 2>&1
                        )
                        $removeExitCode = $LASTEXITCODE
                        foreach ($line in $removeOutput) { $imageOutput.Add($line.ToString()) }
                        if ($removeExitCode -ne 0) {
                            $imageFailure = "docker image rm failed for runner-owned image '$imageName' with code $removeExitCode"
                            break
                        }
                    }
                }
            }
            [pscustomobject]@{
                DownOutput = $downOutput
                DownExitCode = $downExitCode
                ImageOutput = @($imageOutput)
                ImageFailure = $imageFailure
            }
        }
        @($cleanup.DownOutput) + @($cleanup.ImageOutput) |
            ForEach-Object { $_.ToString() } | Add-Content -LiteralPath $LogPath -Encoding utf8NoBOM
        if ($cleanup.DownExitCode -ne 0) {
            throw "docker compose down failed with code $($cleanup.DownExitCode); runner-owned resources may remain"
        }
        if ($cleanup.ImageFailure) {
            throw "$($cleanup.ImageFailure); runner-owned resources may remain"
        }
        $script:composeStarted = $false
    }
    finally {
        if ($cleanupLocationPushed) { Pop-Location }
    }
}

function Add-ComposeRuntimeFinalResult {
    if (-not $composeRuntimeAttempted) {
        return
    }
    $cleanupIncomplete = $composeStarted -or $disposableSecretPaths.Count -gt 0
    $summaryParts = [System.Collections.Generic.List[string]]::new()
    if ($composeRuntimeError) { $summaryParts.Add($composeRuntimeError) }
    if ($cleanupIncomplete) {
        $detail = if ($composeCleanupError) { $composeCleanupError } else { "cleanup did not complete" }
        $summaryParts.Add("runner-owned disposable cleanup remains incomplete: $detail")
    }
    if ($summaryParts.Count -gt 0) {
        Add-AuditResult -Name "Local Compose runtime" -Status "FAIL" `
            -Summary ($summaryParts -join "; ") -LogPath $composeRuntimeLogPath -Seconds $composeRuntimeSeconds
    }
    else {
        Add-AuditResult -Name "Local Compose runtime" -Status "PASS" `
            -Summary "API, database, PDF sandbox, worker, and frontend became healthy; disposable resources were removed" `
            -LogPath $composeRuntimeLogPath -Seconds $composeRuntimeSeconds
    }
}

function Invoke-ComposeRuntimeAudit {
    if (-not $dockerLocalApproved) {
        throw "Disposable Compose was blocked because the local Docker endpoint guard did not pass."
    }
    if (-not $composeOverridePath -or
        -not (Test-Path -LiteralPath $composeOverridePath -PathType Leaf)) {
        throw "Disposable Compose was blocked because runner-owned secret inputs are unavailable."
    }
    Assert-LocalDockerEndpoint -DockerPath $docker | Out-Null
    $logPath = Join-Path $artifactRoot "local-compose-runtime.log"
    $script:composeRuntimeAttempted = $true
    $script:composeRuntimeLogPath = $logPath
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $composeArgs = @(
        "--host", $approvedDockerEndpoint, "compose", "-p", $composeProject,
        "-f", "compose.yaml", "-f", "compose.dev.yaml",
        "-f", $composeOverridePath
    )
    $locationPushed = $false
    try {
        Push-Location -LiteralPath $repositoryRoot
        $locationPushed = $true
        $script:composeStarted = $true
        $execution = Invoke-WithIsolatedPowerMeterEnvironment -OwnedVariables @{
            PM_ENV = "test"
            PM_API_IMAGE = $composeApiImage
            PM_FRONTEND_IMAGE = $composeFrontendImage
        } -Action {
            $commandOutput = @(& $docker @composeArgs up --build --wait --wait-timeout 240 postgres migrate api worker frontend 2>&1)
            [pscustomobject]@{ Output = $commandOutput; ExitCode = $LASTEXITCODE }
        }
        $exitCode = $execution.ExitCode
        @($execution.Output) | ForEach-Object { $_.ToString() } | Set-Content -LiteralPath $logPath -Encoding utf8NoBOM
        if ($exitCode -ne 0) { throw "docker compose up exited with code $exitCode" }

        $api = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health/ready" -TimeoutSec 10
        if ($api.status -ne "ready" -or $api.database -ne "ready" -or $api.pdf_sandbox -ne "enforced") {
            throw "API readiness did not report ready/database ready/PDF sandbox enforced."
        }
        $frontend = Invoke-WebRequest -Uri "http://127.0.0.1:5173/healthz" -TimeoutSec 10 -UseBasicParsing
        if ($frontend.StatusCode -ne 200) { throw "Frontend health returned HTTP $($frontend.StatusCode)." }
        "api_status=$($api.status) database=$($api.database) pdf_sandbox=$($api.pdf_sandbox)" |
            Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
        "frontend_status=$($frontend.StatusCode)" | Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
    }
    catch {
        $_.Exception.Message | Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
        $script:composeRuntimeError = $_.Exception.Message
    }
    finally {
        if ($locationPushed) { Pop-Location }
        try {
            Invoke-DisposableComposeCleanup -LogPath $logPath
            $script:composeCleanupError = $null
        }
        catch {
            $script:composeCleanupError = $_.Exception.Message
            $_.Exception.Message | Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
        }
        if (-not $composeStarted) {
            try {
                Remove-DisposableComposeInputs
                $script:composeCleanupError = $null
            }
            catch {
                $script:composeCleanupError = $_.Exception.Message
                $_.Exception.Message | Add-Content -LiteralPath $logPath -Encoding utf8NoBOM
            }
        }
        $watch.Stop()
        $script:composeRuntimeSeconds = $watch.Elapsed.TotalSeconds
    }
}

function Write-FinalAuditReport {
    $completedAt = [DateTimeOffset]::UtcNow
    $reportPath = Join-Path $artifactRoot "FULL_AUDIT_REPORT.md"
    $endingCommit = "unavailable"
    $endingBranch = "unavailable"
    $endingStatus = @("unavailable")
    if ($git) {
        try {
            $commitCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @("-C", $repositoryRoot, "rev-parse", "HEAD")
            $branchCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @("-C", $repositoryRoot, "branch", "--show-current")
            $statusCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @("-C", $repositoryRoot, "status", "--short")
            if ($commitCapture.ExitCode -ne 0 -or $branchCapture.ExitCode -ne 0 -or $statusCapture.ExitCode -ne 0) {
                throw "one or more final Git state commands failed"
            }
            $endingCommit = (@($commitCapture.Output) -join "`n").Trim()
            $endingBranch = (@($branchCapture.Output) -join "`n").Trim()
            $endingStatus = @($statusCapture.Output)
        }
        catch {
            $endingStatus = @("unable to capture final Git state: $($_.Exception.Message)")
        }
    }
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("# PowerMeter V2 full audit run")
    $lines.Add("")
    $lines.Add("- Run ID: ``$runId``")
    $lines.Add("- Started UTC: ``$($startedAt.ToString('O'))``")
    $lines.Add("- Completed UTC: ``$($completedAt.ToString('O'))``")
    $lines.Add("- Repository: ``$repositoryRoot``")
    $lines.Add("- Starting branch: ``$initialBranch``")
    $lines.Add("- Starting commit: ``$initialCommit``")
    $lines.Add("- Ending branch: ``$endingBranch``")
    $lines.Add("- Ending commit: ``$endingCommit``")
    $lines.Add("- Safe fixes requested: ``$ApplySafeFixes``")
    $lines.Add("- Disposable integration requested: ``$RunDisposableIntegration``")
    $lines.Add("- Strict full audit requested: ``$StrictFullAudit``")
    $lines.Add("")
    $lines.Add("## Results")
    $lines.Add("")
    $lines.Add("| Status | Check | Seconds | Evidence | Summary |")
    $lines.Add("| --- | --- | ---: | --- | --- |")
    foreach ($result in $results) {
        $summary = $result.Summary.Replace("|", "\\|").Replace("`r", " ").Replace("`n", " ")
        $evidence = if ($result.Log) { "``$($result.Log)``" } else { "" }
        $lines.Add("| $($result.Status) | $($result.Name) | $($result.Seconds) | $evidence | $summary |")
    }
    $lines.Add("")
    $lines.Add("## Starting Git status")
    $lines.Add("")
    $lines.Add("``````text")
    if ($initialStatus.Count -eq 0) { $lines.Add("clean") } else { foreach ($item in $initialStatus) { $lines.Add($item) } }
    $lines.Add("``````")
    $lines.Add("")
    $lines.Add("## Final Git status")
    $lines.Add("")
    $lines.Add("``````text")
    if ($endingStatus.Count -eq 0) { $lines.Add("clean") } else { foreach ($item in $endingStatus) { $lines.Add($item) } }
    $lines.Add("``````")
    $lines.Add("")
    $lines.Add("Generated artifacts are ignored by Git. No secret value is included in pattern-scan logs.")
    $lines | Set-Content -LiteralPath $reportPath -Encoding utf8NoBOM
    return $reportPath
}

$git = $null
$python = $null
$npm = $null
$docker = $null
$bash = $null
$go = $null
$idfToolchain = $null
$report = $null
try {
    $git = Resolve-AuditTool -Candidates @("git")
    if (-not $git) { throw "Git is required to inventory the repository safely." }
    $python = Resolve-AuditTool -Candidates @((Join-Path $repositoryRoot ".venv/Scripts/python.exe"), "python", "py")
    $npm = Resolve-AuditTool -Candidates @("npm.cmd", "npm")
    $docker = Resolve-AuditTool -Candidates @("docker.exe", "docker")
    $bash = Resolve-AuditTool -Candidates @("C:/Program Files/Git/bin/bash.exe", "bash")
    $go = Resolve-AuditTool -Candidates @("go.exe", "go")
    if (-not $SkipFirmware) {
        $idfToolchain = Resolve-EspIdfAuditToolchain
    }

    $commitCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @("-C", $repositoryRoot, "rev-parse", "HEAD")
    $branchCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @("-C", $repositoryRoot, "branch", "--show-current")
    $statusCapture = Invoke-IsolatedNativeCapture -FilePath $git -Arguments @("-C", $repositoryRoot, "status", "--short")
    if ($commitCapture.ExitCode -ne 0 -or $branchCapture.ExitCode -ne 0 -or $statusCapture.ExitCode -ne 0) {
        throw "Unable to capture the starting Git state."
    }
    $initialCommit = (@($commitCapture.Output) -join "`n").Trim()
    $initialBranch = (@($branchCapture.Output) -join "`n").Trim()
    $initialStatus = @($statusCapture.Output)
    Write-AuditInventory
    Invoke-AuditCommand -Name "Starting Git status" -FilePath $git -Arguments @("status", "--short", "--branch")

    if ($ApplySafeFixes) {
        Invoke-AuditCommand -Name "Safe Python lint fixes" -FilePath $python -Arguments @(
            "-m", "ruff", "check", "--fix", "backend/app", "backend/tests", "worker", "tests", "scripts",
            "deploy/truenas/initialize_host.py"
        )
        Invoke-AuditCommand -Name "Safe Python formatting" -FilePath $python -Arguments @(
            "-m", "ruff", "format", "backend/app", "backend/tests", "worker", "tests", "scripts",
            "deploy/truenas/initialize_host.py"
        )
        Invoke-AuditCommand -Name "Safe frontend ESLint fixes" -FilePath $npm -Arguments @(
            "--prefix", "frontend", "run", "lint", "--", "--fix"
        )
    }

    Invoke-AuditCommand -Name "Python package consistency" -FilePath $python -Arguments @("-m", "pip", "check")
    Invoke-AuditCommand -Name "Python format check" -FilePath $python -Arguments @(
        "-m", "ruff", "format", "--check", "backend/app", "backend/tests", "worker", "tests", "scripts",
        "deploy/truenas/initialize_host.py"
    )
    Invoke-AuditCommand -Name "Python lint" -FilePath $python -Arguments @(
        "-m", "ruff", "check", "backend", "worker", "tests", "scripts",
        "deploy/truenas/initialize_host.py"
    )
    Invoke-AuditCommand -Name "Python strict type check" -FilePath $python -Arguments @("-m", "mypy", "backend", "worker")
    Invoke-AuditCommand -Name "Initializer Linux type check" -FilePath $python -Arguments @(
        "-m", "mypy", "--platform", "linux", "deploy/truenas/initialize_host.py"
    )
    Invoke-PythonTestAudit -PythonPath $python
    Invoke-AuditCommand -Name "Release and deployment validation" -FilePath $python -Arguments @("scripts/validate_release.py")
    Invoke-AuditCommand -Name "Shared contract validation" -FilePath $python -Arguments @("scripts/validate_contracts.py")
    Invoke-AuditCommand -Name "Python dependency audit" -FilePath $python -Arguments @("-m", "pip_audit", "--desc=off")

    Invoke-GatewayGoChecks -GoPath $go -DockerPath $docker `
        -GatewayRoot (Join-Path $repositoryRoot "gateway") -ContainersDisabled:$SkipContainers

    Invoke-AuditCommand -Name "Frontend locked dependency install" -FilePath $npm -Arguments @("--prefix", "frontend", "ci", "--ignore-scripts")
    Invoke-AuditCommand -Name "Frontend lint" -FilePath $npm -Arguments @("--prefix", "frontend", "run", "lint")
    Invoke-AuditCommand -Name "Frontend type check" -FilePath $npm -Arguments @("--prefix", "frontend", "run", "typecheck")
    Invoke-AuditCommand -Name "Frontend unit tests" -FilePath $npm -Arguments @("--prefix", "frontend", "run", "test")
    Invoke-AuditCommand -Name "Frontend production build" -FilePath $npm -Arguments @("--prefix", "frontend", "run", "build")
    Invoke-AuditCommand -Name "Frontend dependency audit" -FilePath $npm -Arguments @("--prefix", "frontend", "audit", "--audit-level=high")
    if ($SkipBrowser) {
        Add-SkippedCriticalGate -Name "Browser and accessibility tests" -Summary "-SkipBrowser was supplied"
    }
    else {
        Invoke-AuditCommand -Name "Browser and accessibility tests" -FilePath $npm -Arguments @("--prefix", "frontend", "run", "test:e2e")
    }

    if ($bash) {
        Invoke-AuditCommand -Name "GitHub workflow validation" -FilePath $bash -Arguments @("scripts/verify_workflows.sh")
        Invoke-AuditCommand -Name "Firmware GitHub workflow validation" -FilePath $bash -Arguments @(
            (Join-Path $repositoryRoot "scripts/verify_workflows.sh")
        ) -WorkingDirectory (Join-Path $repositoryRoot "power-monitor-sensor-headless")
        Invoke-AuditCommand -Name "Shell syntax" -FilePath $bash -Arguments @(
            "-n", "backup/backup.sh", "backup/common.sh", "backup/entrypoint.sh",
            "backup/healthcheck.sh", "backup/restore.sh", "deploy/postgres/init-roles.sh",
            "deploy/truenas/prepare-host.sh", "scripts/release_deployment_smoke.sh",
            "scripts/verify_workflows.sh"
        )
    }
    else {
        Add-AuditResult -Name "GitHub workflow validation" -Status "FAIL" -Summary "a POSIX Bash runtime was not found"
        Add-AuditResult -Name "Firmware GitHub workflow validation" -Status "FAIL" -Summary "a POSIX Bash runtime was not found"
        Add-AuditResult -Name "Shell syntax" -Status "FAIL" -Summary "a POSIX Bash runtime was not found"
    }
    Invoke-AuditScriptBlock -Name "PowerShell syntax" -Action {
        $scriptCount = 0
        foreach ($entry in @(Get-TrackedAuditFiles)) {
            if ($entry.RelativePath -notmatch '(?i)\.ps1$') {
                continue
            }
            Assert-AuditEntryReadable -Entry $entry
            $scriptCount++
            $tokens = $null
            $parseErrors = $null
            [System.Management.Automation.Language.Parser]::ParseFile(
                $entry.FullPath,
                [ref]$tokens,
                [ref]$parseErrors
            ) | Out-Null
            if ($parseErrors.Count -gt 0) {
                throw "PowerShell parse failure in $($entry.RelativePath): $($parseErrors[0].Message)"
            }
        }
        "$scriptCount PowerShell script(s) parsed successfully."
    }

    if ($SkipContainers) {
        Add-SkippedCriticalGate -Name "Container and Compose checks" -Summary "-SkipContainers was supplied"
    }
    elseif (-not $docker) {
        Add-AuditResult -Name "Container and Compose checks" -Status "FAIL" -Summary "Docker was not found"
    }
    else {
        Invoke-LocalDockerApprovalGate -DockerPath $docker
        if ($dockerLocalApproved) {
            Invoke-AuditCommand -Name "Local Compose validation" -FilePath $docker -Arguments @(
                "--host", $approvedDockerEndpoint, "compose",
                "-f", "compose.yaml", "-f", "compose.dev.yaml", "config", "--quiet"
            )
            Invoke-AuditCommand -Name "TrueNAS Compose validation" -FilePath $docker -Arguments @(
                "--host", $approvedDockerEndpoint, "compose",
                "-f", "deploy/truenas/power-monitor-v2.yaml", "config", "--quiet"
            )
            if ($RunDisposableIntegration) {
                Invoke-AuditCommand -Name "Backend container image build" -FilePath $docker -Arguments @(
                    "--host", $approvedDockerEndpoint, "build", "--progress=plain",
                    "--file", "backend/Dockerfile", "--output", "type=cacheonly", "."
                )
                Invoke-AuditCommand -Name "Frontend container image build" -FilePath $docker -Arguments @(
                    "--host", $approvedDockerEndpoint, "build", "--progress=plain",
                    "--file", "frontend/Dockerfile", "--output", "type=cacheonly", "frontend"
                )
                Invoke-AuditCommand -Name "Gateway container image build" -FilePath $docker -Arguments @(
                    "--host", $approvedDockerEndpoint, "build", "--progress=plain",
                    "--file", "gateway/Dockerfile", "--output", "type=cacheonly", "."
                )
                Invoke-AuditCommand -Name "Backup container image build" -FilePath $docker -Arguments @(
                    "--host", $approvedDockerEndpoint, "build", "--progress=plain",
                    "--file", "backup/Dockerfile", "--output", "type=cacheonly", "."
                )
                New-DisposableComposeInputs | Out-Null
                Invoke-ComposeRuntimeAudit
            }
            else {
                Add-SkippedCriticalGate -Name "Disposable container image builds" `
                    -Summary "requires explicit -RunDisposableIntegration; no image build was attempted"
                Add-SkippedCriticalGate -Name "Local Compose runtime" `
                    -Summary "requires explicit -RunDisposableIntegration; no service or migration was started"
            }
        }
    }

    if ($SkipFirmware) {
        Add-SkippedCriticalGate -Name "Firmware host tests" -Summary "-SkipFirmware was supplied"
        Add-SkippedCriticalGate -Name "Firmware host dependency audit" -Summary "-SkipFirmware was supplied"
        Add-SkippedCriticalGate -Name "Firmware ESP-IDF version" -Summary "-SkipFirmware was supplied"
        Add-SkippedCriticalGate -Name "Firmware ESP-IDF build" -Summary "-SkipFirmware was supplied"
        Add-SkippedCriticalGate -Name "Firmware dependency audit" -Summary "-SkipFirmware was supplied"
    }
    else {
        Invoke-AuditCommand -Name "Firmware host dependency audit" -FilePath $python -Arguments @(
            "-m", "pip_audit", "--requirement", "test/host/requirements.txt", "--desc=off"
        ) -WorkingDirectory (Join-Path $repositoryRoot "power-monitor-sensor-headless")
        Invoke-AuditCommand -Name "Firmware host tests" -FilePath $python -Arguments @("tools/run_host_tests.py") `
            -WorkingDirectory (Join-Path $repositoryRoot "power-monitor-sensor-headless")
        if ($idfToolchain) {
            $idfArguments = @($idfToolchain.PrefixArguments)
            Invoke-AuditScriptBlock -Name "Firmware ESP-IDF version" `
                -EnvironmentVariables $idfToolchain.Environment -Action {
                $versionOutput = @(& $idfToolchain.FilePath @idfArguments --version 2>&1)
                if ($LASTEXITCODE -ne 0) { throw "idf.py --version exited with code $LASTEXITCODE" }
                $versionText = $versionOutput -join "`n"
                if ($versionText -notmatch '(?i)ESP-IDF\s+v6\.0\.2(?:\s|$)') {
                    throw "ESP-IDF v6.0.2 is required; reported version did not match."
                }
                "source=$($idfToolchain.Source)"
                $versionText
            }
            $firmwareBuild = Join-Path $artifactRoot "firmware-build"
            $firmwareBuildArguments = @($idfToolchain.PrefixArguments) + @(
                "-B", $firmwareBuild,
                "-D", "SDKCONFIG=$firmwareBuild/sdkconfig",
                "-D", "SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.release-candidate",
                "set-target", "esp32s3", "build"
            )
            Invoke-AuditCommand -Name "Firmware ESP-IDF build" -FilePath $idfToolchain.FilePath `
                -Arguments $firmwareBuildArguments `
                -WorkingDirectory (Join-Path $repositoryRoot "power-monitor-sensor-headless") `
                -EnvironmentVariables $idfToolchain.Environment
            Invoke-AuditCommand -Name "Firmware dependency audit" -FilePath $python -Arguments @(
                "tools/audit_dependencies.py", "--output", (Join-Path $artifactRoot "firmware-dependency-audit.json")
            ) -WorkingDirectory (Join-Path $repositoryRoot "power-monitor-sensor-headless") `
                -EnvironmentVariables $idfToolchain.Environment
        }
        else {
            Add-AuditResult -Name "Firmware ESP-IDF version" -Status "FAIL" -Summary "a validated ESP-IDF v6.0.2 executable or EIM installation is unavailable; use -SkipFirmware only for a partial audit"
            Add-AuditResult -Name "Firmware ESP-IDF build" -Status "FAIL" -Summary "the pinned ESP-IDF toolchain is unavailable; the firmware build was not attempted"
            Add-AuditResult -Name "Firmware dependency audit" -Status "FAIL" -Summary "the pinned ESP-IDF toolchain and managed dependency tree are unavailable"
        }
    }

    Invoke-TrueNasPathScan
    Invoke-TrackedPatternScan -Name "Floating production image scan" -Rules @{
        "latest-image-tag" = '(?i)(?:^|\s)(?:image:\s*|FROM\s+)\S+:latest(?:\s|$)'
    } -Include {
        param($entry)
        $entry.RelativePath -match '(?i)(Dockerfile|\.ya?ml)$'
    }
    $secretScannerDefinitionPaths = @(
        "scripts/Invoke-PowerMeterFullAudit.ps1",
        "tests/test_full_audit_runner.py"
    )
    Invoke-TrackedPatternScan -Name "High-confidence committed-secret scan" -Rules @{
        "private-key-material" = '-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----'
        "github-token" = '(?:github_pat_[A-Za-z0-9_]{40,}|gh[pousr]_[A-Za-z0-9]{30,})'
        "aws-access-key" = '(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])'
        "credential-in-database-url" = '(?i)(?:postgres(?:ql)?|mysql)://[^:/\s]+:[^@\s/]+@'
    } -Include {
        param($entry)
        $entry.RelativePath -notin $secretScannerDefinitionPaths
    } -IgnoreFinding {
        param($entry, $lineNumber, $line, $ruleName)
        $ruleName -eq "private-key-material" -and
        (
            (
                $entry.RelativePath -in @(
                    "deploy/truenas/initialize_host.py",
                    "deploy/truenas/Stage-PowerMeterTrueNAS.ps1"
                ) -and
                $line -match '(?:re\.search|\s-match\s)'
            ) -or
            $entry.RelativePath -in @(
                "tests/test_release_tools.py",
                "tests/test_host_initializer.py"
            )
        )
    }
    Invoke-AuditScriptBlock -Name "Tracked secret-file extension scan" -Action {
        $unsafe = @(Get-TrackedAuditFiles | Where-Object {
            $leafName = [System.IO.Path]::GetFileName($_.RelativePath)
            $environmentFile = $leafName -match '(?i)^\.env(?:\..+)?$' -and
                $leafName -notmatch '(?i)^\.env(?:\..+)?\.example$' -and
                $leafName -ne '.env.example'
            $environmentFile -or
            $_.RelativePath -match '(?i)(?:^|/)\.?secrets?(?:/|$)' -or
            $_.RelativePath -match '(?i)\.(?:key|pem|crt|p12|pfx)$'
        })
        if ($unsafe.Count -gt 0) {
            throw "Tracked secret/certificate-shaped paths: $($unsafe.RelativePath -join ', ')"
        }
        "No tracked secret/certificate-shaped paths."
    }
    Invoke-TrackedPatternScan -Name "Temporary bypass and disabled-test scan" -Rules @{
        "temporary-marker" = '(?i)\b(?:TODO|FIXME|HACK|XXX)\b'
        "typescript-suppression" = '@ts-(?:ignore|expect-error)'
        "eslint-disable" = 'eslint-disable'
        "python-type-ignore" = '#\s*type:\s*ignore'
        "pytest-skip" = 'pytest\.(?:mark\.skip|skip\()'
        "javascript-test-skip" = '(?:test|it|describe)\.skip\s*\('
        "empty-python-handler" = '(?i)except[^:]*:\s*pass\s*$'
    } -Include {
        param($entry)
        $entry.RelativePath -notin @(
            "scripts/Invoke-PowerMeterFullAudit.ps1",
            "tests/test_full_audit_runner.py"
        )
    } -FindingStatus "WARNING"
    Invoke-AuditScriptBlock -Name "Promise rejection and empty-catch safeguards" -Action {
        $eslintConfig = [System.IO.File]::ReadAllText(
            (Join-Path $repositoryRoot "frontend/eslint.config.js")
        )
        if (-not $eslintConfig.Contains("'@typescript-eslint/no-floating-promises': 'error'")) {
            throw "Frontend ESLint must fail on unhandled/floating promises."
        }
        if ($eslintConfig -notmatch 'recommendedTypeChecked') {
            throw "Frontend ESLint must retain type-aware rules."
        }
        $emptyCatches = [System.Collections.Generic.List[string]]::new()
        foreach ($entry in @(Get-TrackedAuditFiles)) {
            if ($entry.RelativePath -notmatch '(?i)\.(?:js|jsx|ts|tsx)$') {
                continue
            }
            Assert-AuditEntryReadable -Entry $entry
            $text = [System.IO.File]::ReadAllText($entry.FullPath)
            if ($text -match '(?s)catch\s*(?:\([^)]*\))?\s*\{\s*\}') {
                $emptyCatches.Add($entry.RelativePath)
            }
        }
        if ($emptyCatches.Count -gt 0) {
            throw "Empty JavaScript/TypeScript catch block(s): $($emptyCatches -join ', ')"
        }
        "Type-aware no-floating-promises enforcement is enabled; no empty JS/TS catch blocks found."
    }
    Invoke-TrackedPatternScan -Name "Production debug logging scan" -Rules @{
        "browser-console-debug" = 'console\.(?:log|debug)\s*\('
        "python-print" = '(?<![A-Za-z0-9_])print\s*\('
    } -Include {
        param($entry)
        $entry.RelativePath -match '^(backend/app|worker/app|frontend/src)/'
    } -FindingStatus "WARNING"
    Invoke-TrackedPatternScan -Name "Hardcoded loopback URL scan" -Rules @{
        "loopback-url" = '(?i)https?://(?:localhost|127\.0\.0\.1)(?::\d+)?'
    } -Include {
        param($entry)
        $entry.RelativePath -match '^(backend/app|worker/app|frontend/src|deploy|gateway|scripts|\.github)/'
    } -FindingStatus "WARNING"
    Invoke-DuplicateIdScan
    Invoke-EnvironmentDocumentationScan
    Invoke-AuditCommand -Name "Ending Git diff integrity" -FilePath $git -Arguments @("diff", "--check")
}
catch {
    Add-AuditResult -Name "Audit runner fatal error" -Status "FAIL" -Summary $_.Exception.Message
}
finally {
    if ($composeStarted -and $docker) {
        try {
            Invoke-DisposableComposeCleanup -LogPath $composeRuntimeLogPath
            $script:composeCleanupError = $null
        }
        catch {
            $script:composeCleanupError = $_.Exception.Message
            Write-Warning "Failed to clean the disposable Compose project ${composeProject}: $($_.Exception.Message)"
        }
    }
    if (-not $composeStarted -and $disposableSecretPaths.Count -gt 0) {
        try {
            Remove-DisposableComposeInputs
            $script:composeCleanupError = $null
        }
        catch {
            $script:composeCleanupError = $_.Exception.Message
            Write-Warning "Failed to clean runner-owned disposable inputs: $($_.Exception.Message)"
        }
    }
    Add-ComposeRuntimeFinalResult
    try {
        $report = Write-FinalAuditReport
    }
    catch {
        Write-Warning "The final audit report could not be written: $($_.Exception.Message)"
    }
}

if (-not $report) { exit 2 }
$failed = @($results | Where-Object Status -in @("FAIL", "PARTIAL"))
Write-Host "Audit report: $report"
if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) audit check(s) failed or were partial. Review the timestamped report and evidence logs." -ForegroundColor Red
    exit 1
}
exit 0
