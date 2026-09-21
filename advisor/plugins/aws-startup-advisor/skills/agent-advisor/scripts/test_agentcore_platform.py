"""Exercise the deploy helper with service responses, without AWS calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
# Runs the authored template against test-owned fake CLIs.
import subprocess  # nosec B404
import sys
from unittest.mock import Mock

import pytest

import set_agentcore_platform as platform


@pytest.fixture
def runtime():
    return {
        "agentRuntimeId": "poc-1234567890",
        "agentRuntimeVersion": "1",
        "status": "READY",
        "platformVersion": "V1",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "example/image"}},
        "roleArn": "arn:aws:iam::123456789012:role/Poc",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "description": "POC",
        "authorizerConfiguration": {"customJWTAuthorizer": {"allowedClients": ["client"]}},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User"]},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"maxLifetime": 28800},
        "environmentVariables": {"PRIVATE_VALUE": "must-not-be-printed"},
        "filesystemConfigurations": [],
        "metadataConfiguration": {"requireMMDSV2": True},
        "workloadIdentityDetails": {"workloadIdentityArn": "do-not-forward"},
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(platform.time, "sleep", lambda _: None)


def test_upgrade_preserves_configuration_and_waits_for_new_revision(runtime):
    upgraded = {**runtime, "platformVersion": "V2", "agentRuntimeVersion": "2"}
    client = Mock()
    client.get_agent_runtime.side_effect = [
        {**runtime, "status": "CREATING"}, runtime,
        runtime, {**upgraded, "status": "UPDATING"}, upgraded,
    ]
    client.update_agent_runtime.return_value = {"agentRuntimeVersion": "2", "status": "UPDATING"}
    before = deepcopy(runtime)
    result = platform.set_platform(client, runtime["agentRuntimeId"], "V2")
    request = client.update_agent_runtime.call_args.kwargs
    for key in platform.UPDATE_FIELDS:
        assert request[key] == before[key]
    assert set(request) == set(platform.UPDATE_FIELDS) | {"agentRuntimeId", "platformVersion"}
    assert request["platformVersion"] == "V2"
    assert result == {
        "agentRuntimeId": runtime["agentRuntimeId"], "agentRuntimeVersion": "2",
        "platformVersion": "V2", "status": "READY",
    }
    assert "must-not-be-printed" not in str(result)
    assert runtime == before


@pytest.mark.parametrize("version", ["V1", "V2"])
def test_matching_platform_only_reads(runtime, version):
    runtime["platformVersion"] = version
    client = Mock()
    client.get_agent_runtime.return_value = runtime
    assert platform.set_platform(client, runtime["agentRuntimeId"], version)["platformVersion"] == version
    client.update_agent_runtime.assert_not_called()


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED", "DELETING"])
def test_failure_never_updates_or_falls_back(runtime, status):
    client = Mock()
    client.get_agent_runtime.return_value = {**runtime, "status": status}
    with pytest.raises(RuntimeError, match=status):
        platform.set_platform(client, runtime["agentRuntimeId"], "V2")
    client.update_agent_runtime.assert_not_called()


def test_failed_upgrade_never_retries_v1(runtime):
    client = Mock()
    client.get_agent_runtime.side_effect = [runtime, {**runtime, "status": "UPDATE_FAILED"}]
    client.update_agent_runtime.return_value = {"agentRuntimeVersion": "2"}
    with pytest.raises(RuntimeError, match="UPDATE_FAILED"):
        platform.set_platform(client, runtime["agentRuntimeId"], "V2")
    assert client.update_agent_runtime.call_count == 1
    assert client.update_agent_runtime.call_args.kwargs["platformVersion"] == "V2"


def test_ready_with_wrong_platform_is_not_success(runtime):
    client = Mock()
    client.get_agent_runtime.side_effect = [runtime, {**runtime, "agentRuntimeVersion": "2"}]
    client.update_agent_runtime.return_value = {"agentRuntimeVersion": "2"}
    with pytest.raises(RuntimeError, match="does not match"):
        platform.set_platform(client, runtime["agentRuntimeId"], "V2")


@pytest.mark.parametrize("extra", [
    {"capacityProviderConfiguration": {"capacityProviderArn": "instances"}},
    {"platformVersion": None},
])
def test_instances_and_unknown_platform_cannot_be_updated(runtime, extra):
    client = Mock()
    client.get_agent_runtime.return_value = {**runtime, **extra}
    with pytest.raises((ValueError, RuntimeError)):
        platform.set_platform(client, runtime["agentRuntimeId"], "V2")
    client.update_agent_runtime.assert_not_called()


def test_timeout_is_bounded_without_mutation(runtime, monkeypatch):
    client = Mock()
    client.get_agent_runtime.return_value = {**runtime, "status": "UPDATING"}
    ticks = iter([0, 0, 901])
    monkeypatch.setattr(platform.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        platform.set_platform(client, runtime["agentRuntimeId"], "V2")
    client.update_agent_runtime.assert_not_called()


@pytest.mark.parametrize("failure,run_id,override,expected_name", [
    ("none", "0921-1530", None, "poc_agent_0921_1530"),
    ("none", "01a0b59b-a54c-7963-8759-48e49b10df0f", None,
     "poc_agent_01a0b59b_a54c_7963_8759_48e49b10df0f"),
    ("none", "0921-1530", "CustomAgent_42", "CustomAgent_42"),
    ("none", "0921-1530", "A" * 48, "A" * 48),
    ("name", "0921-1530", "invalid-name", None),
    ("name", "0921-1530", "9invalid", None),
    ("name", "0921-1530", "A" * 49, None),
    ("sdk", "0921-1530", None, "poc_agent_0921_1530"),
    ("update", "0921-1530", None, "poc_agent_0921_1530"),
    ("region", "0921-1530", None, "poc_agent_0921_1530"),
    ("declined", "0921-1530", None, "poc_agent_0921_1530"),
], ids=[
    "default-timestamp", "default-uuid", "custom-name", "max-length",
    "invalid-hyphen", "invalid-first-character", "over-length",
    "sdk", "update", "region", "declined",
])
def test_exact_deploy_template_orders_preflight_and_platform_verification(
    tmp_path, failure, run_id, override, expected_name
):
    """Run the authored shell with fake CLIs, including its real config-reading heredoc."""
    poc = Path(__file__).parent.parent / "references/phases/poc/poc.md"
    section = poc.read_text().split("### 3d.", 1)[1].split("### 3e.", 1)[0]
    shell = re.search(r"```bash\n(.*?)\n```", section, re.S).group(1)
    shell = shell.replace("<verified-target-region>", "us-west-2").replace("<run_id>", run_id)
    script = tmp_path / "deploy.sh"
    script.write_text(shell)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # JSON is a YAML subset. This fixture avoids a test dependency on PyYAML.
    (tmp_path / "yaml.py").write_text("import json\nsafe_load = json.loads\n")
    config = {"default_agent": "wrong_default", "agents": {
        "wrong_default": {"bedrock_agentcore": {"agent_id": "wrong-runtime"}},
        expected_name or "unused": {"bedrock_agentcore": {"agent_id": "right-runtime"}},
    }}
    (tmp_path / ".bedrock_agentcore.yaml").write_text(json.dumps(config))
    # Enforce the official starter toolkit's validate_agent_name contract.
    fake_cli = """import json, os, pathlib, re, subprocess, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\\n")
if tool == "aws":
    print("123456789012")
elif tool == "agentcore":
    option = "--name" if args[0] == "configure" else "--agent"
    name = args[args.index(option) + 1]
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", name):
        print("Invalid agent name: only letters, numbers, and underscores are allowed.", file=sys.stderr)
        sys.exit(2)
elif tool == "uv":
    if "--check-sdk" in args:
        sys.exit(1 if os.environ["FAILURE"] == "sdk" else 0)
    if "pyyaml" in args:
        code = sys.stdin.read()
        command = [sys.executable, "-c", code, args[-1]]
        sys.exit(subprocess.run(command).returncode)
    if os.environ["FAILURE"] == "update":
        sys.exit(1)
    print(json.dumps({
        "agentRuntimeId": args[args.index("--runtime-id") + 1],
        "agentRuntimeVersion": "2",
        "platformVersion": args[args.index("--platform-version") + 1],
        "status": "READY",
    }))
"""
    for tool in ("aws", "agentcore", "uv"):
        executable = binaries / tool
        executable.write_text(f"#!{sys.executable}\n{fake_cli}")
        executable.chmod(0o755)
    evidence_path = tmp_path / "runtime-verification.json"
    evidence_path.write_text(json.dumps({"platformVersion": "V1", "status": "READY"}))
    log = tmp_path / "calls.jsonl"
    env = {**os.environ, "PATH": f"{binaries}:/usr/bin:/bin",
           "PYTHONPATH": str(tmp_path), "CALL_LOG": str(log), "FAILURE": failure,
           "AWS_REGION": "eu-central-1" if failure == "region" else "us-west-2"}
    env.pop("AGENT_NAME", None)
    if override is not None:
        env["AGENT_NAME"] = override
    # Fixed bash executable and test-owned script; no shell interpolation.
    result = subprocess.run(  # nosec B603
        ["/bin/bash", str(script)], cwd=tmp_path, env=env,
        input="no\n" if failure == "declined" else "deploy\n",
        text=True, capture_output=True, timeout=10,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    writes = [call for call in calls if call[0] == "agentcore"]
    if failure in ("name", "sdk", "region", "declined"):
        assert result.returncode != 0
        assert writes == []
        if failure != "declined":
            assert not evidence_path.exists()
    else:
        if failure == "none":
            assert result.returncode == 0, result.stderr
        preflight = next(i for i, call in enumerate(calls) if "--check-sdk" in call)
        assert preflight < next(i for i, call in enumerate(calls) if call[0] == "agentcore")
        assert writes[0][-2:] == ["--region", "us-west-2"]
        assert writes[0][writes[0].index("--name") + 1] == expected_name
        assert writes[1][1:4] == ["launch", "--agent", expected_name]
        update = calls[-1]
        assert update[update.index("--runtime-id") + 1] == "right-runtime"
        assert update[update.index("--platform-version") + 1] == "V2"
        assert update[update.index("--region") + 1] == "us-west-2"
        if failure == "update":
            assert result.returncode != 0
            assert "Platform verified." not in result.stdout
            assert not evidence_path.exists()
        else:
            assert result.returncode == 0, result.stderr
            assert f"agentcore destroy --agent {expected_name}" in result.stdout
            evidence = json.loads((tmp_path / "runtime-verification.json").read_text())
            assert evidence["platformVersion"] == "V2"
            assert evidence["agentRuntimeId"] == "right-runtime"
