from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = ROOT / "scripts" / "Invoke-PowerMeterFullAudit.ps1"


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is required only for audit-runner executable tests")
    return executable


def _quoted(path: Path) -> str:
    return str(path).replace("'", "''")


def _function_loader() -> str:
    return (
        "$tokens=$null;$parseErrors=$null;"
        "$auditAst=[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_quoted(AUDIT_SCRIPT)}',[ref]$tokens,[ref]$parseErrors);"
        "if($parseErrors.Count){throw $parseErrors[0]};"
        "$auditAst.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true)|"
        "ForEach-Object{Invoke-Expression $_.Extent.Text};"
    )


def _run_powershell(command: str, *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _last_json(stdout: str) -> object:
    return json.loads(next(line for line in reversed(stdout.splitlines()) if line.strip()))


def test_full_audit_runner_is_safe_by_default_and_covers_required_gates() -> None:
    text = AUDIT_SCRIPT.read_text(encoding="utf-8")

    assert "#requires -Version 7.0" in text
    assert "[switch]$ApplySafeFixes" in text
    assert "[switch]$RunDisposableIntegration" in text
    assert "[switch]$StrictFullAudit" in text
    assert '"artifacts/audit/$runId"' in text
    assert "first-party-inventory.tsv" in text
    assert "FULL_AUDIT_REPORT.md" in text
    assert "First-party inventory entry is missing or unreadable" in text
    for gate in (
        "Python format check",
        "Python lint",
        "Python strict type check",
        "Initializer Linux type check",
        "Python unit and integration tests",
        "Python test temporary-directory cleanup",
        "Python dependency audit",
        "Gateway Go module verification",
        "Gateway Go dependency graph",
        "Gateway Go tests",
        "Gateway Go vet",
        "Frontend lint",
        "Frontend type check",
        "Frontend unit tests",
        "Frontend production build",
        "Frontend dependency audit",
        "Browser and accessibility tests",
        "GitHub workflow validation",
        "Firmware GitHub workflow validation",
        "Shell syntax",
        "PowerShell syntax",
        "Local Docker endpoint guard",
        "Local Compose validation",
        "TrueNAS Compose validation",
        "New-DisposableComposeInputs",
        "Backend container image build",
        "Frontend container image build",
        "Gateway container image build",
        "Backup container image build",
        "Local Compose runtime",
        "Firmware host dependency audit",
        "Firmware host tests",
        "Firmware ESP-IDF version",
        "Firmware ESP-IDF build",
        "Firmware dependency audit",
        "TrueNAS obsolete-path scan",
        "Floating production image scan",
        "High-confidence committed-secret scan",
        "Temporary bypass and disabled-test scan",
        "Promise rejection and empty-catch safeguards",
        "Duplicate UI identifiers",
        "Environment-variable documentation",
    ):
        assert gate in text

    safe_fix_block, checks = text.split(
        '    Invoke-AuditCommand -Name "Python package consistency"', maxsplit=1
    )
    assert "if ($ApplySafeFixes)" in safe_fix_block
    assert "--fix" in safe_fix_block
    assert '"format"' in safe_fix_block
    assert "--fix" not in checks

    integration_condition = text.index("if ($RunDisposableIntegration)")
    runtime_calls = [
        match.start() for match in re.finditer(r"(?m)^\s*Invoke-ComposeRuntimeAudit\s*$", text)
    ]
    assert len(runtime_calls) == 1
    assert runtime_calls[0] > integration_condition
    assert "requires explicit -RunDisposableIntegration" in text
    assert "no service or migration was started" in text
    assert 'Status -in @("FAIL", "PARTIAL")' in text
    assert '"--host", $approvedDockerEndpoint' in text
    assert '"--basetemp", $pytestBaseTemp' in text
    assert "if (-not $SkipFirmware)" in text
    assert "-ContainersDisabled:$SkipContainers" in text

    for forbidden in (
        "git reset",
        "git clean",
        "git checkout --",
        "Remove-Item -Recurse",
        "docker system prune",
        "--no-verify",
        "-k https://",
    ):
        assert forbidden not in text

    obsolete_pattern = "/mnt/Apps/" + "Power(?!MeterV2)"
    assert obsolete_pattern not in text
    assert "\\.?secrets?" in text
    assert "'.env.example'" in text


def test_compose_runtime_call_is_structurally_guarded_by_explicit_switch() -> None:
    command = _function_loader() + (
        "$calls=$auditAst.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.CommandAst] -and "
        "$node.GetCommandName() -eq 'Invoke-ComposeRuntimeAudit'},$true);"
        "$guarded=$false;"
        "if($calls.Count -eq 1){$parent=$calls[0].Parent;"
        "while($null -ne $parent){"
        "if($parent -is [System.Management.Automation.Language.IfStatementAst] -and "
        "$parent.Extent.Text -match 'if\\s*\\(\\$RunDisposableIntegration\\)')"
        "{$guarded=$true;break};$parent=$parent.Parent}};"
        "[pscustomobject]@{count=$calls.Count;guarded=$guarded}|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {"count": 1, "guarded": True}


def test_environment_isolation_clears_all_inherited_pm_names_and_restores_them() -> None:
    command = _function_loader() + (
        "$original=@{PM_ENV='production';PM_DATABASE_URL='danger';"
        "PM_TEST_MIGRATOR_DATABASE_URL='danger-cleanup';PM_LIVE_API_URL='https://live.invalid';"
        "PM_ARBITRARY_INHERITED='must-not-leak'};"
        "foreach($name in $original.Keys){"
        "[Environment]::SetEnvironmentVariable($name,$original[$name],'Process')};"
        "$inside=@(Invoke-WithIsolatedPowerMeterEnvironment -Action {"
        "Get-ChildItem Env:|Where-Object Name -match '^PM_'|Sort-Object Name|"
        'ForEach-Object{"$($_.Name)=$($_.Value)"}});'
        "$after=@{};foreach($name in $original.Keys){"
        "$after[$name]=[Environment]::GetEnvironmentVariable($name,'Process')};"
        "[pscustomobject]@{inside=$inside;after=$after}|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = _last_json(completed.stdout)
    assert payload["inside"] == ["PM_ENV=test"]
    assert payload["after"] == {
        "PM_ENV": "production",
        "PM_DATABASE_URL": "danger",
        "PM_TEST_MIGRATOR_DATABASE_URL": "danger-cleanup",
        "PM_LIVE_API_URL": "https://live.invalid",
        "PM_ARBITRARY_INHERITED": "must-not-leak",
    }


def test_python_test_gate_uses_and_removes_unique_runner_owned_basetemp(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "audit-artifacts"
    fake_python = tmp_path / "fake-python.ps1"
    argument_log = tmp_path / "python-arguments.txt"
    artifact_root.mkdir()
    fake_python.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$PythonArgs)\n"
        f"Set-Content -LiteralPath '{_quoted(argument_log)}' -Value ($PythonArgs -join '|')\n"
        "$index=[Array]::IndexOf($PythonArgs,'--basetemp')\n"
        "if($index -lt 0 -or $index -ge ($PythonArgs.Count-1)){exit 41}\n"
        "$baseTemp=$PythonArgs[$index+1]\n"
        "New-Item -ItemType Directory -Path $baseTemp|Out-Null\n"
        "Set-Content -LiteralPath (Join-Path $baseTemp 'fixture.txt') -Value 'temporary'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    command = _function_loader() + (
        f"$repositoryRoot='{_quoted(ROOT)}';"
        f"$artifactRoot='{_quoted(artifact_root)}';"
        "$results=[System.Collections.Generic.List[object]]::new();"
        f"Invoke-PythonTestAudit -PythonPath '{_quoted(fake_python)}';"
        f"$arguments=(Get-Content -Raw -LiteralPath '{_quoted(argument_log)}').Trim();"
        "$parts=$arguments.Split('|');$index=[Array]::IndexOf($parts,'--basetemp');"
        "$baseTemp=$parts[$index+1];"
        "[pscustomobject]@{names=@($results.Name);statuses=@($results.Status);"
        "arguments=$arguments;baseTemp=$baseTemp;exists=(Test-Path -LiteralPath $baseTemp);"
        "cleanupLog=(Get-Content -Raw -LiteralPath "
        "(Join-Path $artifactRoot 'python-test-temporary-directory-cleanup.log')).Trim()}|"
        "ConvertTo-Json -Depth 3 -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = _last_json(completed.stdout)
    assert payload["names"] == [
        "Python unit and integration tests",
        "Python test temporary-directory cleanup",
    ]
    assert payload["statuses"] == ["PASS", "PASS"]
    assert payload["arguments"].startswith("-m|pytest|-ra|--basetemp|")
    base_temp = Path(payload["baseTemp"])
    assert base_temp.parent == artifact_root
    assert re.fullmatch(r"pytest-basetemp-[0-9a-f]{32}", base_temp.name)
    assert payload["exists"] is False
    assert payload["cleanupLog"] == f"removed={base_temp}"


def test_python_test_basetemp_cleanup_failure_is_a_blocking_audit_result(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "audit-artifacts"
    fake_python = tmp_path / "fake-python.ps1"
    argument_log = tmp_path / "python-arguments.txt"
    artifact_root.mkdir()
    fake_python.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$PythonArgs)\n"
        f"Set-Content -LiteralPath '{_quoted(argument_log)}' -Value ($PythonArgs -join '|')\n"
        "$index=[Array]::IndexOf($PythonArgs,'--basetemp')\n"
        "$baseTemp=$PythonArgs[$index+1]\n"
        "New-Item -ItemType Directory -Path $baseTemp|Out-Null\n"
        "exit 0\n",
        encoding="utf-8",
    )
    command = _function_loader() + (
        f"$repositoryRoot='{_quoted(ROOT)}';"
        f"$artifactRoot='{_quoted(artifact_root)}';"
        "$results=[System.Collections.Generic.List[object]]::new();"
        f"Invoke-PythonTestAudit -PythonPath '{_quoted(fake_python)}' "
        "-CleanupAction {param($target)throw 'simulated cleanup denial'};"
        f"$arguments=(Get-Content -Raw -LiteralPath '{_quoted(argument_log)}').Trim();"
        "$parts=$arguments.Split('|');$index=[Array]::IndexOf($parts,'--basetemp');"
        "$baseTemp=$parts[$index+1];"
        "[pscustomobject]@{statuses=@($results.Status);summary=$results[1].Summary;"
        "exists=(Test-Path -LiteralPath $baseTemp);"
        "blocking=@($results|Where-Object Status -in @('FAIL','PARTIAL')).Count}|"
        "ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {
        "statuses": ["PASS", "FAIL"],
        "summary": "simulated cleanup denial",
        "exists": True,
        "blocking": 1,
    }


def test_python_test_basetemp_cleanup_rejects_path_outside_exact_artifact_root(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "audit-artifacts"
    outside = tmp_path / "outside" / ("pytest-basetemp-" + ("a" * 32))
    artifact_root.mkdir()
    outside.mkdir(parents=True)
    (outside / "retain.txt").write_text("must remain\n", encoding="utf-8")
    command = _function_loader() + (
        f"$artifactRoot='{_quoted(artifact_root)}';"
        "$blocked=$false;try{"
        f"Remove-RunnerOwnedPytestBaseTemp -Path '{_quoted(outside)}'"
        "}catch{$blocked=$_.Exception.Message -match 'exact runner-owned path'};"
        "[pscustomobject]@{blocked=$blocked;retained=(Test-Path -LiteralPath "
        f"'{_quoted(outside / 'retain.txt')}')}}|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {"blocked": True, "retained": True}


def test_inventory_readability_gate_fails_closed_for_a_missing_entry() -> None:
    command = _function_loader() + (
        "$blocked=$false;try{Assert-AuditEntryReadable -Entry ([pscustomobject]@{"
        "RelativePath='missing-fixture';FullPath='Z:/definitely/missing/audit-fixture'})}"
        "catch{$blocked=$_.Exception.Message -match 'missing or unreadable'};"
        "$blocked|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) is True


def test_local_docker_guard_rejects_remote_override_without_invoking_docker(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "fake-docker.ps1"
    call_log = tmp_path / "docker-calls.log"
    fake_docker.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$DockerArgs)\n"
        f"Add-Content -LiteralPath '{_quoted(call_log)}' -Value ($DockerArgs -join ' ')\n"
        "Write-Output 'unexpected invocation'\n",
        encoding="utf-8",
    )
    command = _function_loader() + (
        "[Environment]::SetEnvironmentVariable('DOCKER_HOST','tcp://192.168.0.175:2375','Process');"
        "$blocked=$false;try{"
        f"Assert-LocalDockerEndpoint -DockerPath '{_quoted(fake_docker)}'|Out-Null"
        "}catch{$blocked=$true};"
        "[pscustomobject]@{blocked=$blocked;called=(Test-Path -LiteralPath "
        f"'{_quoted(call_log)}')}}|"
        "ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {"blocked": True, "called": False}


def test_disposable_compose_inputs_are_runner_owned_and_exactly_cleaned(
    tmp_path: Path,
) -> None:
    command = _function_loader() + (
        f"$artifactRoot='{_quoted(tmp_path)}';"
        "$composeOverridePath=$null;"
        "$disposableSecretPaths=[System.Collections.Generic.List[string]]::new();"
        "$override=New-DisposableComposeInputs;"
        "$secretRoot=Join-Path $artifactRoot 'disposable-compose/secrets';"
        "$files=@(Get-ChildItem -LiteralPath $secretRoot -File);"
        "$values=@($files|ForEach-Object{Get-Content -Raw -LiteralPath $_.FullName});"
        "$yaml=Get-Content -Raw -LiteralPath $override;"
        "$before=[pscustomobject]@{count=$files.Count;unique=@($values|Sort-Object -Unique).Count;"
        "overrideExists=(Test-Path -LiteralPath $override);"
        "usesRepositorySecrets=$yaml.Contains('./.secrets')};"
        "Remove-DisposableComposeInputs;"
        "$afterExists=Test-Path -LiteralPath (Join-Path $artifactRoot 'disposable-compose');"
        "[pscustomobject]@{before=$before;afterExists=$afterExists}|"
        "ConvertTo-Json -Depth 3 -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {
        "before": {
            "count": 12,
            "unique": 12,
            "overrideExists": True,
            "usesRepositorySecrets": False,
        },
        "afterExists": False,
    }


def test_disposable_compose_cleanup_retains_failure_state_until_retry_succeeds(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "fake-docker-cleanup.ps1"
    first_failure = tmp_path / "first-cleanup-failed"
    cleanup_log = tmp_path / "cleanup.log"
    fake_docker.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$DockerArgs)\n"
        "$joined=$DockerArgs -join ' '\n"
        "if($joined -eq 'context show'){Write-Output 'desktop-linux';exit 0}\n"
        "if($joined -like 'context inspect desktop-linux*'){"
        "Write-Output 'npipe:////./pipe/dockerDesktopLinuxEngine';exit 0}\n"
        "if($joined -like '--host npipe:* info --format*'){"
        "Write-Output 'desktop|Docker Desktop';exit 0}\n"
        "if($joined -like '--host npipe:* compose * down --volumes --remove-orphans'){"
        f"if(-not(Test-Path -LiteralPath '{_quoted(first_failure)}')){{"
        f"Set-Content -LiteralPath '{_quoted(first_failure)}' -Value 'failed';"
        "Write-Output 'simulated cleanup failure';exit 23};"
        "Write-Output 'cleanup retry succeeded';exit 0}\n"
        "if($joined -like '--host npipe:* image ls *'){exit 0}\n"
        "exit 9\n",
        encoding="utf-8",
    )
    command = _function_loader() + (
        "[Environment]::SetEnvironmentVariable('DOCKER_HOST',$null,'Process');"
        f"$repositoryRoot='{_quoted(ROOT)}';"
        f"$artifactRoot='{_quoted(tmp_path)}';"
        f"$docker='{_quoted(fake_docker)}';"
        "$approvedDockerEndpoint=$null;"
        "$composeProject='pmv2audit-cleanup-fixture';"
        "$composeApiImage='pmv2-audit-api-fixture:audit';"
        "$composeFrontendImage='pmv2-audit-frontend-fixture:audit';"
        "$composeOverridePath=$null;"
        "$composeStarted=$true;"
        "$firstBlocked=$false;try{"
        f"Invoke-DisposableComposeCleanup -LogPath '{_quoted(cleanup_log)}'"
        "}catch{$firstBlocked=$_.Exception.Message -match 'resources may remain'};"
        "$retainedAfterFailure=$composeStarted;"
        f"Invoke-DisposableComposeCleanup -LogPath '{_quoted(cleanup_log)}';"
        "[pscustomobject]@{firstBlocked=$firstBlocked;"
        "retainedAfterFailure=$retainedAfterFailure;clearedAfterRetry=(-not $composeStarted)}|"
        "ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {
        "firstBlocked": True,
        "retainedAfterFailure": True,
        "clearedAfterRetry": True,
    }


def test_incomplete_disposable_cleanup_makes_runtime_result_blocking(tmp_path: Path) -> None:
    command = _function_loader() + (
        f"$artifactRoot='{_quoted(tmp_path)}';"
        "$results=[System.Collections.Generic.List[object]]::new();"
        "$composeRuntimeAttempted=$true;"
        "$composeRuntimeError=$null;"
        "$composeCleanupError='simulated cleanup failure';"
        "$composeRuntimeSeconds=1;"
        "$composeRuntimeLogPath=$null;"
        "$composeStarted=$true;"
        "$disposableSecretPaths=[System.Collections.Generic.List[string]]::new();"
        "Add-ComposeRuntimeFinalResult;"
        "$failed=@($results|Where-Object Status -in @('FAIL','PARTIAL'));"
        "[pscustomobject]@{status=$results[0].Status;blocking=$failed.Count;"
        "summary=$results[0].Summary}|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = _last_json(completed.stdout)
    assert payload["status"] == "FAIL"
    assert payload["blocking"] == 1
    assert "cleanup remains incomplete" in payload["summary"]


def test_local_docker_guard_accepts_only_inspected_local_endpoint(tmp_path: Path) -> None:
    fake_docker = tmp_path / "fake-docker.ps1"
    fake_docker.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$DockerArgs)\n"
        "$joined=$DockerArgs -join ' '\n"
        "if($joined -eq 'context show'){Write-Output 'desktop-linux';exit 0}\n"
        "if($joined -like 'context inspect desktop-linux*'){"
        "Write-Output 'npipe:////./pipe/dockerDesktopLinuxEngine';exit 0}\n"
        "if($joined -like '--host npipe:* info --format*'){"
        "Write-Output 'desktop|Docker Desktop';exit 0}\n"
        "exit 9\n",
        encoding="utf-8",
    )
    command = _function_loader() + (
        "[Environment]::SetEnvironmentVariable('DOCKER_HOST',$null,'Process');"
        "[Environment]::SetEnvironmentVariable('DOCKER_CONTEXT',$null,'Process');"
        f"$evidence=@(Assert-LocalDockerEndpoint -DockerPath '{_quoted(fake_docker)}');"
        "$evidence|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == [
        "source=context:desktop-linux",
        "endpoint=npipe:////./pipe/dockerDesktopLinuxEngine",
        "daemon=desktop|Docker Desktop",
    ]


def test_eim_v602_metadata_resolves_exact_python_idf_pair_and_environment(
    tmp_path: Path,
) -> None:
    tools_root = tmp_path / "Espressif" / "tools"
    idf_root = tmp_path / "esp" / "v6.0.2" / "esp-idf"
    python_env = tools_root / "python" / "v6.0.2" / "venv"
    python_path = python_env / "Scripts" / "python.exe"
    idf_script = idf_root / "tools" / "idf.py"
    activation_script = tools_root / "Microsoft.v6.0.2.PowerShell_profile.ps1"
    metadata_path = tools_root / "eim_idf.json"
    python_path.parent.mkdir(parents=True)
    idf_script.parent.mkdir(parents=True)
    python_path.write_bytes(b"")
    idf_script.write_text("# fixture\n", encoding="utf-8")
    activation_script.write_text(
        "param([switch]$e)\n"
        "if(-not $e){throw 'report-only mode required'}\n"
        f"Write-Output 'PATH={_quoted(tools_root / 'cmake' / 'bin')}'\n"
        "Write-Output 'ESP_IDF_VERSION=6.0'\n"
        "Write-Output 'IDF_VERSION=6.0.2'\n"
        f"Write-Host 'IDF_TOOLS_PATH={_quoted(tools_root)}'\n"
        f"Write-Output 'IDF_PATH={_quoted(idf_root)}'\n"
        f"Write-Output 'IDF_PYTHON_ENV_PATH={_quoted(python_env)}'\n",
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(
            {
                "idfInstalled": [
                    {
                        "activationScript": str(activation_script),
                        "id": "fixture-v602",
                        "idfToolsPath": str(tools_root),
                        "name": "v6.0.2",
                        "path": str(idf_root),
                        "python": str(python_path),
                    }
                ],
                "idfSelectedId": "fixture-v602",
            }
        ),
        encoding="utf-8",
    )
    command = _function_loader() + (
        f"$tool=Resolve-EspIdfEimToolchain -MetadataCandidates @('{_quoted(metadata_path)}');"
        "if(-not $tool){throw 'fixture EIM toolchain did not resolve'};"
        "$before=[Environment]::GetEnvironmentVariable('IDF_PATH','Process');"
        "$inside=Invoke-WithTemporaryEnvironment -Variables $tool.Environment -Action {"
        "[Environment]::GetEnvironmentVariable('IDF_PATH','Process')};"
        "$after=[Environment]::GetEnvironmentVariable('IDF_PATH','Process');"
        "[pscustomobject]@{file=$tool.FilePath;prefix=@($tool.PrefixArguments);"
        "idfPath=$tool.Environment.IDF_PATH;toolsPath=$tool.Environment.IDF_TOOLS_PATH;"
        "version=$tool.Environment.IDF_VERSION;inside=$inside;restored=($before -eq $after)}|"
        "ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {
        "file": str(python_path),
        "prefix": [str(idf_script)],
        "idfPath": str(idf_root),
        "toolsPath": str(tools_root),
        "version": "6.0.2",
        "inside": str(idf_root),
        "restored": True,
    }


def test_eim_resolution_fails_closed_when_activation_version_disagrees(
    tmp_path: Path,
) -> None:
    tools_root = tmp_path / "tools"
    idf_root = tmp_path / "idf"
    python_env = tools_root / "python-env"
    python_path = python_env / "Scripts" / "python.exe"
    idf_script = idf_root / "tools" / "idf.py"
    activation_script = tools_root / "Microsoft.v6.0.2.PowerShell_profile.ps1"
    metadata_path = tools_root / "eim_idf.json"
    python_path.parent.mkdir(parents=True)
    idf_script.parent.mkdir(parents=True)
    python_path.write_bytes(b"")
    idf_script.write_text("# fixture\n", encoding="utf-8")
    activation_script.write_text(
        "param([switch]$e)\n"
        "Write-Output 'PATH=C:\\\\fixture'\n"
        "Write-Output 'IDF_VERSION=6.0.1'\n"
        f"Write-Output 'IDF_TOOLS_PATH={_quoted(tools_root)}'\n"
        f"Write-Output 'IDF_PATH={_quoted(idf_root)}'\n"
        f"Write-Output 'IDF_PYTHON_ENV_PATH={_quoted(python_env)}'\n",
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(
            {
                "idfInstalled": [
                    {
                        "activationScript": str(activation_script),
                        "id": "fixture-v602",
                        "idfToolsPath": str(tools_root),
                        "name": "v6.0.2",
                        "path": str(idf_root),
                        "python": str(python_path),
                    }
                ],
                "idfSelectedId": "fixture-v602",
            }
        ),
        encoding="utf-8",
    )
    command = _function_loader() + (
        f"$tool=Resolve-EspIdfEimToolchain -MetadataCandidates @('{_quoted(metadata_path)}');"
        "($null -eq $tool)|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) is True


def test_gateway_go_docker_fallback_is_pinned_read_only_and_locally_approved(
    tmp_path: Path,
) -> None:
    gateway_root = tmp_path / "gateway"
    artifact_root = tmp_path / "evidence"
    fake_docker = tmp_path / "fake-docker.ps1"
    call_log = tmp_path / "docker-calls.log"
    gateway_root.mkdir()
    artifact_root.mkdir()
    (gateway_root / "go.mod").write_text("module fixture.invalid/gateway\n", encoding="utf-8")
    fake_docker.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$DockerArgs)\n"
        "$joined=$DockerArgs -join ' '\n"
        f"Add-Content -LiteralPath '{_quoted(call_log)}' -Value $joined\n"
        "if($joined -eq 'context show'){Write-Output 'desktop-linux';exit 0}\n"
        "if($joined -like 'context inspect desktop-linux*'){"
        "Write-Output 'npipe:////./pipe/dockerDesktopLinuxEngine';exit 0}\n"
        "if($joined -like '--host npipe:* info --format*'){"
        "Write-Output 'desktop|Docker Desktop';exit 0}\n"
        "if($joined -like '--host npipe:* run *'){"
        "if($joined -notlike '*golang:1.26.6-alpine3.23@sha256:*'){exit 31};"
        "if($joined -notlike '*destination=/workspace,readonly*'){exit 32};"
        "if($joined -notlike '*GOTOOLCHAIN=local*'){exit 33};"
        "if($joined -like '*.secrets*'){exit 34};exit 0}\n"
        "exit 35\n",
        encoding="utf-8",
    )
    command = _function_loader() + (
        "[Environment]::SetEnvironmentVariable('DOCKER_HOST',$null,'Process');"
        "[Environment]::SetEnvironmentVariable('DOCKER_CONTEXT',$null,'Process');"
        f"$artifactRoot='{_quoted(artifact_root)}';"
        "$results=[System.Collections.Generic.List[object]]::new();"
        "$StrictFullAudit=$false;$dockerLocalApproved=$false;"
        "$dockerApprovalResultRecorded=$false;$approvedDockerEndpoint=$null;"
        f"Invoke-GatewayGoChecks -DockerPath '{_quoted(fake_docker)}' "
        f"-GatewayRoot '{_quoted(gateway_root)}';"
        f"$calls=@(Get-Content -LiteralPath '{_quoted(call_log)}');"
        "[pscustomobject]@{statuses=@($results.Status);calls=$calls;"
        "approved=$dockerLocalApproved;image=(Get-PinnedGatewayGoImage)}|"
        "ConvertTo-Json -Depth 3 -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = _last_json(completed.stdout)
    assert payload["statuses"] == ["PASS"] * 5
    assert payload["approved"] is True
    gateway_builder = (ROOT / "gateway" / "Dockerfile").read_text(encoding="utf-8").splitlines()[0]
    assert payload["image"] == gateway_builder.removeprefix("FROM ").split(" AS ", maxsplit=1)[0]
    assert len(payload["calls"]) == 7
    run_calls = [call for call in payload["calls"] if " run " in f" {call} "]
    assert len(run_calls) == 4
    assert all("--host npipe:////./pipe/dockerDesktopLinuxEngine" in call for call in run_calls)
    assert all("--pull=always" in call for call in run_calls)
    assert all("destination=/workspace,readonly" in call for call in run_calls)
    assert all(".secrets" not in call for call in run_calls)


def test_skip_containers_disables_gateway_docker_fallback(tmp_path: Path) -> None:
    gateway_root = tmp_path / "gateway"
    fake_docker = tmp_path / "must-not-run.ps1"
    call_log = tmp_path / "docker-was-called"
    gateway_root.mkdir()
    (gateway_root / "go.mod").write_text("module fixture.invalid/gateway\n", encoding="utf-8")
    fake_docker.write_text(
        f"Set-Content -LiteralPath '{_quoted(call_log)}' -Value 'called'\nexit 99\n",
        encoding="utf-8",
    )
    command = _function_loader() + (
        "$results=[System.Collections.Generic.List[object]]::new();"
        "$dockerLocalApproved=$false;$dockerApprovalResultRecorded=$false;"
        "$approvedDockerEndpoint=$null;"
        f"Invoke-GatewayGoChecks -DockerPath '{_quoted(fake_docker)}' "
        f"-GatewayRoot '{_quoted(gateway_root)}' -ContainersDisabled;"
        f"[pscustomobject]@{{statuses=@($results.Status);called=(Test-Path '{_quoted(call_log)}');"
        "approvalRecorded=$dockerApprovalResultRecorded}|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {
        "statuses": ["FAIL"] * 4,
        "called": False,
        "approvalRecorded": False,
    }


def test_strict_skips_become_blocking_partial_results() -> None:
    command = _function_loader() + (
        "$artifactRoot=[System.IO.Path]::GetTempPath();"
        "$results=[System.Collections.Generic.List[object]]::new();"
        "$StrictFullAudit=$true;"
        "Add-SkippedCriticalGate -Name 'critical fixture' -Summary 'fixture skip';"
        "$results[0]|Select-Object Name,Status,Summary|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _last_json(completed.stdout) == {
        "Name": "critical fixture",
        "Status": "PARTIAL",
        "Summary": "fixture skip",
    }


def test_final_report_is_written_when_runner_fails_before_first_gate(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit-repository"
    (audit_root / "scripts").mkdir(parents=True)
    (audit_root / "frontend").mkdir()
    shutil.copy2(AUDIT_SCRIPT, audit_root / "scripts" / AUDIT_SCRIPT.name)
    (audit_root / "pyproject.toml").write_text("", encoding="utf-8")
    (audit_root / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    (audit_root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = ""

    completed = subprocess.run(  # noqa: S603
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(audit_root / "scripts" / AUDIT_SCRIPT.name),
        ],
        cwd=audit_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    reports = list((audit_root / "artifacts" / "audit").glob("*/FULL_AUDIT_REPORT.md"))
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "Audit runner fatal error" in report
    assert "Git is required to inventory the repository safely" in report
    assert "Starting commit: `unavailable`" in report
    assert "Ending commit: `unavailable`" in report


def test_pattern_scanner_detects_fixture_but_ignores_its_definition_source(
    tmp_path: Path,
) -> None:
    scanner_definition = tmp_path / "scanner-definition.ps1"
    leaked_value = tmp_path / "leaked-value.txt"
    scanner_definition.write_text(
        "private key detector marker: " + "-----BEGIN " + "PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    leaked_value.write_text("AKIA" + ("A" * 16) + "\n", encoding="utf-8")
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    command = _function_loader() + (
        f"$artifactRoot='{_quoted(artifact_root)}';"
        "$results=[System.Collections.Generic.List[object]]::new();"
        f"$scannerDefinition='{_quoted(scanner_definition)}';"
        f"$leakedValue='{_quoted(leaked_value)}';"
        "function Get-TrackedAuditFiles {"
        "@([pscustomobject]@{Scope='server';RelativePath='scanner-definition.ps1';FullPath=$scannerDefinition},"
        "[pscustomobject]@{Scope='server';RelativePath='leaked-value.txt';FullPath=$leakedValue})};"
        "Invoke-TrackedPatternScan -Name 'fixture secret scan' -Rules @{"
        "'private-key-material'="
        "'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----';"
        "'aws-access-key'='(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])'} -IgnoreFinding {"
        "param($entry,$lineNumber,$line,$ruleName)"
        "$entry.RelativePath -eq 'scanner-definition.ps1'};"
        "$log=Get-Content -Raw -LiteralPath (Join-Path $artifactRoot 'fixture-secret-scan.log');"
        "[pscustomobject]@{status=$results[0].Status;log=$log}|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = _last_json(completed.stdout)
    assert payload["status"] == "FAIL"
    assert "leaked-value.txt:1:aws-access-key" in payload["log"]
    assert "scanner-definition" not in payload["log"]


def test_truenas_path_scan_detects_unquoted_end_of_line_trailing_space(
    tmp_path: Path,
) -> None:
    invalid_path = tmp_path / "invalid-path.txt"
    invalid_path.write_text("/mnt/Apps/" + "PowerMeterV2 " + "\n", encoding="utf-8")
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    command = _function_loader() + (
        f"$artifactRoot='{_quoted(artifact_root)}';"
        "$results=[System.Collections.Generic.List[object]]::new();"
        f"$invalidPath='{_quoted(invalid_path)}';"
        "function Get-TrackedAuditFiles {"
        "@([pscustomobject]@{Scope='server';RelativePath='invalid-path.txt';"
        "FullPath=$invalidPath})};"
        "Invoke-TrueNasPathScan;"
        "$log=Get-Content -Raw -LiteralPath "
        "(Join-Path $artifactRoot 'truenas-obsolete-path-scan.log');"
        "[pscustomobject]@{status=$results[0].Status;log=$log}|ConvertTo-Json -Compress"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = _last_json(completed.stdout)
    assert payload["status"] == "FAIL"
    assert "invalid-path.txt:1:trailing-space-after-root" in payload["log"]


def test_full_audit_runner_parses_in_powershell() -> None:
    escaped_path = _quoted(AUDIT_SCRIPT)
    command = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{escaped_path}',[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
    )
    completed = _run_powershell(command)

    assert completed.returncode == 0, completed.stdout + completed.stderr
