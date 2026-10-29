# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared test utilities."""

import contextlib
import json
import logging
import os
import re
import traceback
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import boto3
import yaml
from botocore.config import Config
from typer.testing import CliRunner, Result

import cats.config
from cats.cfn.deploy import CATS_VERSION_TAG_KEY, get_stack_name, get_stack_status
from cats.cfn.events import StackEvent
from cats.cli import cats_cli
from cats.config import CatsConfig
from cats.constants import DeployedEC2Resources, DeployedLambdaResources
from cats.report_run import RunManifest
from cats.version import __version__

if TYPE_CHECKING:
    import pytest
    from botocore.awsrequest import AWSPreparedRequest
    from pytest_mock import MockerFixture

# --- YAML helpers ---


def write_config(path: Path, config: dict[str, Any]) -> None:
    """Write a config dict to cats.yaml and reset settings cache."""
    path.write_text(yaml.dump(config))
    cats.config.reset_settings()


def write_isolated_yaml(path: Path, content: dict[str, Any] | str) -> None:
    """Write content to a yaml file without resetting global settings."""
    if isinstance(content, str):
        path.write_text(content)
    else:
        path.write_text(yaml.dump(content))


# --- Snapshot normalization ---


def normalize_aws_ids(text: str) -> str:
    """Replace dynamic AWS IDs with stable placeholders for snapshot testing."""
    text = re.sub(r"vpc-[a-f0-9]+", "vpc-00000000000000000", text)
    text = re.sub(r"subnet-[a-f0-9]+", "subnet-00000000000000000", text)
    text = re.sub(r"sg-[a-f0-9]+", "sg-00000000000000000", text)
    text = re.sub(r"rtb-[a-f0-9]+", "rtb-00000000000000000", text)
    return text


def normalize_output(text: str) -> str:
    """Remove timestamp prefixes and wall-clock durations from log output for snapshot testing."""
    text = re.sub(r"\[\d{2}:\d{2}:\d{2}\] ", "", text)
    return re.sub(r"in \d+s", "in <n>s", text)


def normalize_paths(text: str) -> str:
    """Replace temp paths with stable placeholder for snapshot testing."""
    return re.sub(r"/[^\s]+/cats\.yaml", "/tmp/cats.yaml", text)  # noqa: S108


def normalize_config_path(text: str) -> str:
    """Replace dynamic config path with stable placeholder for snapshot testing."""
    return re.sub(r"generated from .+\.yaml\.", "generated from <config_path>.", text)


# --- Mock response factories ---


def make_cfn_event(
    event_id: str,
    logical_id: str,
    status: str,
    resource_type: str = "AWS::CloudFormation::Stack",
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a raw CFN event dict (as returned by describe_stack_events)."""
    return {
        "EventId": event_id,
        "LogicalResourceId": logical_id,
        "ResourceType": resource_type,
        "ResourceStatus": status,
        "ResourceStatusReason": reason,
        "Timestamp": datetime.now(UTC),
    }


class CfnTaggedLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation intrinsics as {"Fn::X": args} instead of discarding them.

    yaml.safe_load turns `!Ref X` and `!Sub ...` into None; assertions on rendered templates
    that need to see the intrinsic load with this instead.
    """


def _register_cfn_tag(tag: str) -> None:
    def construct(loader: yaml.SafeLoader, node: yaml.Node) -> dict[str, Any]:
        if isinstance(node, yaml.ScalarNode):
            return {tag: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {tag: loader.construct_sequence(node, deep=True)}
        return {tag: loader.construct_mapping(node, deep=True)}  # type: ignore[arg-type]

    CfnTaggedLoader.add_constructor(f"!{tag}", construct)


for _tag in ("Ref", "Sub", "FindInMap", "GetAtt", "Base64"):
    _register_cfn_tag(_tag)


def load_cfn_template(rendered: str) -> dict[str, Any]:
    """Parse a rendered template, keeping intrinsics."""
    template: dict[str, Any] = yaml.load(rendered, Loader=CfnTaggedLoader)  # noqa: S506
    return template


def make_probe_output(result: str, reason: str | None = None, http_status: int | None = None) -> dict[str, Any]:
    """The probe's three-key wire document, identical on SSM stdout and as the Lambda payload."""
    return {"result": result, "reason": reason, "http_status": http_status}


def make_ssm_response(status: str, result: str, reason: str | None = None, http_status: int | None = None) -> dict[str, Any]:
    """Create mock get_command_invocation response with the probe's JSON on stdout."""
    return {
        "Status": status,
        "StatusDetails": f"{status} details",
        "StandardOutputContent": json.dumps(make_probe_output(result, reason, http_status)),
        "StandardErrorContent": "",
    }


def make_lambda_response(result: str, reason: str | None = None, http_status: int | None = None) -> dict[str, Any]:
    """Create mock Lambda invoke response whose payload is the probe's JSON (no envelope)."""
    body = json.dumps(make_probe_output(result, reason, http_status)).encode()
    return {"Payload": MagicMock(read=lambda: body)}


def make_lambda_error_response(error_message: str, error_type: str = "Runtime.ExitError") -> dict[str, Any]:
    """Create mock Lambda invoke response for an unhandled runtime error: FunctionError is set and the payload has no `result` key."""
    body = json.dumps({"errorMessage": error_message, "errorType": error_type, "stackTrace": []}).encode()
    return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: body)}


def make_stack_tags(version: str | None = __version__) -> list[dict[str, str]]:
    """Stack tags as `cats infra deploy` writes them."""
    tags = [{"Key": "cats:managed", "Value": "true"}]
    if version is not None:
        tags.append({"Key": CATS_VERSION_TAG_KEY, "Value": version})
    return tags


def make_deployed_ec2(dns_name: str = "ip-10-0-1-5.ec2.internal") -> DeployedEC2Resources:
    """Create deployed EC2 resources for tests."""
    return DeployedEC2Resources(dns_name=dns_name, instance_id="i-1234567890abcdef0")


def make_deployed_lambda() -> DeployedLambdaResources:
    """Create deployed Lambda resources for tests."""
    return DeployedLambdaResources(probe_lambda_name="CATS-probe-lambda")


def make_stack_event(
    logical_id: str,
    status: str,
    resource_type: str = "AWS::S3::Bucket",
    status_reason: str | None = None,
) -> StackEvent:
    """Create a StackEvent for tests."""
    return StackEvent(
        timestamp=datetime.now(UTC),
        logical_id=logical_id,
        resource_type=resource_type,
        status=status,
        status_reason=status_reason,
    )


# --- Config factory ---


def make_config(
    account_name: str = "testaccount",
    account_id: str = "111111111111",
    accounts: dict[str, str] | None = None,
    network_targets: dict[str, dict[str, Any]] | None = None,
    enabled_network_handlers: list[str] | None = None,
    additional_roles: dict[str, dict[str, Any]] | None = None,
    default_test_role_managed_policies: list[str] | None = None,
    gateway_endpoints: list[str] | None = None,
    interface_endpoints: list[str] | None = None,
    interface_endpoint_security_group_ids: list[str] | None = None,
    fixture_scope: str = "session",
    allow_deploy_to_current_account: bool = False,
    cfn_stack_prefix: str = "CATS-test-infra-",
    aws_region: str = "us-east-1",
    parent_account_id: str = "999999999999",
    existing_ec2_instance_profile_name: str | None = None,
    account_network_settings: dict[str, Any] | None = None,
    account_settings: dict[str, Any] | None = None,
    reporting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build cats.yaml config dict.

    Args:
        account_name: Name for the test account
        account_id: Account ID for the test account
        accounts: Dict of account_name -> account_id (overrides account_name/account_id)
        network_targets: Dict of target_name -> {vpc_id, subnet_ids, ...}
        enabled_network_handlers: List of handler names (e.g. ["lambda", "ec2"])
        additional_roles: Dict of role_name -> {managed_policies, inline_policies} for global account settings
        default_test_role_managed_policies: Managed policies for default test role
        gateway_endpoints: List of gateway endpoint names
        interface_endpoints: List of interface endpoint names
        interface_endpoint_security_group_ids: Security groups for the interface endpoints; the key is omitted when None
        fixture_scope: Pytest fixture scope for account fixtures
        allow_deploy_to_current_account: Set deployment setting
        cfn_stack_prefix: CloudFormation stack name prefix
        aws_region: AWS region for deployment
        parent_account_id: Account ID running CATS (cats_deployment_settings.parent_account_id)
        account_network_settings: Per-account network settings override
        reporting: The ``reporting`` section, e.g. {"custom_markers": ["team"]}; omitted when None
        account_settings: Per-account account settings override
    """
    # Build accounts_under_test
    if accounts:
        accounts_under_test = {name: {"account_id": aid, "account_network_under_test": network_targets or {}} for name, aid in accounts.items()}
    else:
        account_entry: dict[str, Any] = {"account_id": account_id, "account_network_under_test": network_targets or {}}
        if account_network_settings is not None:
            account_entry["network_settings"] = account_network_settings
        if account_settings is not None:
            account_entry["account_settings"] = account_settings
        accounts_under_test = {account_name: account_entry}

    global_network_settings: dict[str, Any] = {
        "enabled_network_handlers": enabled_network_handlers or ["ec2"],
        "udp_listen_ports": [5000],
        "tcp_listen_ports": [8080],
        "ec2_instance_type": "t3.micro",
        "existing_ec2_ami_id": "ami-0123456789abcdef0",
        "gateway_endpoints": gateway_endpoints or [],
        "interface_endpoints": interface_endpoints or [],
        "existing_ec2_instance_profile_name": existing_ec2_instance_profile_name,
    }
    if interface_endpoint_security_group_ids is not None:
        global_network_settings["interface_endpoint_security_group_ids"] = interface_endpoint_security_group_ids

    return {
        "cats_deployment_settings": {
            "allow_deploy_to_current_account": allow_deploy_to_current_account,
            "parent_account_id": parent_account_id,
            "aws_region": aws_region,
            "partition": "aws",
            "cfn_stack_prefix": cfn_stack_prefix,
        },
        "global_network_settings": global_network_settings,
        "global_account_settings": {
            "additional_roles": additional_roles or {},
            "default_test_role_managed_policies": default_test_role_managed_policies or [],
            "fixture_scope": fixture_scope,
        },
        "accounts_under_test": accounts_under_test,
        **({"reporting": reporting} if reporting is not None else {}),
    }


def make_cats_config(
    account_name: str = "testaccount",
    account_id: str = "111111111111",
    accounts: dict[str, str] | None = None,
    network_targets: dict[str, dict[str, Any]] | None = None,
    enabled_network_handlers: list[str] | None = None,
    additional_roles: dict[str, dict[str, Any]] | None = None,
    default_test_role_managed_policies: list[str] | None = None,
    gateway_endpoints: list[str] | None = None,
    interface_endpoints: list[str] | None = None,
    interface_endpoint_security_group_ids: list[str] | None = None,
    fixture_scope: str = "session",
    allow_deploy_to_current_account: bool = False,
    cfn_stack_prefix: str = "CATS-test-infra-",
    aws_region: str = "us-east-1",
    parent_account_id: str = "999999999999",
    existing_ec2_instance_profile_name: str | None = None,
) -> "cats.config.CatsConfig":
    """Build validated CatsConfig. Wrapper around make_config."""

    return CatsConfig.model_validate(
        make_config(
            account_name=account_name,
            account_id=account_id,
            accounts=accounts,
            network_targets=network_targets,
            enabled_network_handlers=enabled_network_handlers,
            additional_roles=additional_roles,
            default_test_role_managed_policies=default_test_role_managed_policies,
            gateway_endpoints=gateway_endpoints,
            interface_endpoints=interface_endpoints,
            interface_endpoint_security_group_ids=interface_endpoint_security_group_ids,
            fixture_scope=fixture_scope,
            allow_deploy_to_current_account=allow_deploy_to_current_account,
            cfn_stack_prefix=cfn_stack_prefix,
            aws_region=aws_region,
            parent_account_id=parent_account_id,
            existing_ec2_instance_profile_name=existing_ec2_instance_profile_name,
        )
    )


# --- Integration test helpers ---


def _cli_error_detail(result: Result) -> str:
    """Return CLI output and exception details suitable for a teardown failure."""
    detail = result.output or ""
    if result.exception is not None:
        detail += "\n" + "".join(
            traceback.format_exception(
                type(result.exception),
                result.exception,
                result.exception.__traceback__,
            )
        )
    return detail.strip() or "unknown CLI error"


def _assert_stacks_deleted(stack_names: list[str], region: str) -> None:
    """Fail cleanup when CloudFormation still reports any requested stack."""
    session = boto3.Session(region_name=region)
    for stack_name in stack_names:
        status = get_stack_status(session, stack_name, region)
        if status is not None:
            raise RuntimeError(f"Destroy completed but stack {stack_name} remains in {status}")


def destroy_stack(stack_name: str, account_keyname: str, region: str) -> None:
    """Destroy one CATS stack and fail unless the CLI succeeds and the stack is absent."""
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(cats_cli, ["infra", "destroy", account_keyname])
    print(f"[fixture] Destroy exit code: {result.exit_code}")

    if result.exit_code != 0:
        raise RuntimeError(f"Destroy failed for {stack_name}: {_cli_error_detail(result)}")
    _assert_stacks_deleted([stack_name], region)


def destroy_stacks(stack_names: list[str], region: str) -> None:
    """Destroy all configured CATS stacks and fail unless every stack is absent."""
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(cats_cli, ["infra", "destroy", "--all"])
    print(f"[fixture] Destroy exit code: {result.exit_code}")

    if result.exit_code != 0:
        names = ", ".join(stack_names)
        raise RuntimeError(f"Destroy failed for {names}: {_cli_error_detail(result)}")
    _assert_stacks_deleted(stack_names, region)


def deploy_and_destroy(
    tmp_path_factory: "pytest.TempPathFactory",
    config: dict[str, Any],
    account_keyname: str | list[str],
    region: str,
    dir_name: str,
    deploy_all: bool = False,
) -> Generator[str | list[str], None, None]:
    """Deploy CATS stack(s), yield account keyname(s), then destroy. For module-scoped fixtures.

    Destroy and environment restoration always run, even when the deploy fails - a failed
    deploy leaves a stack behind that would otherwise poison every later run.
    """

    log = logging.getLogger(__name__)

    tmp_path = tmp_path_factory.mktemp(dir_name)
    config_path = tmp_path / "cats.yaml"
    old_config = os.environ.get("CATS_CONFIG")
    os.environ["CATS_CONFIG"] = str(config_path)
    write_config(config_path, config)

    runner = CliRunner(env={"COLUMNS": "200"})

    try:
        if deploy_all:
            log.info("Deploying with --all...")
            print("\n[fixture] Deploying with --all...")
            result = runner.invoke(cats_cli, ["infra", "deploy", "--all"])
        else:
            log.info("Deploying %s...", account_keyname)
            print(f"\n[fixture] Deploying {account_keyname}...")
            result = runner.invoke(cats_cli, ["infra", "deploy", str(account_keyname)])

        log.info("Deploy exit code: %s", result.exit_code)
        print(f"[fixture] Deploy exit code: {result.exit_code}")
        if result.exit_code != 0:
            error_detail = result.output or ""
            if result.exception is not None:
                error_detail += "\n" + "".join(traceback.format_exception(type(result.exception), result.exception, result.exception.__traceback__))
            raise RuntimeError(f"Deploy failed: {error_detail}")

        yield account_keyname

    finally:
        try:
            prefix = str(config["cats_deployment_settings"]["cfn_stack_prefix"])
            if deploy_all:
                log.info("Destroying with --all...")
                print("\n[fixture] Destroying with --all...")
                account_keynames = [account_keyname] if isinstance(account_keyname, str) else account_keyname
                destroy_stacks([get_stack_name(prefix, str(name)) for name in account_keynames], region)
            else:
                log.info("Destroying %s...", account_keyname)
                print(f"\n[fixture] Destroying {account_keyname}...")
                destroy_stack(get_stack_name(prefix, str(account_keyname)), str(account_keyname), region)
        finally:
            if old_config is None:
                os.environ.pop("CATS_CONFIG", None)
            else:
                os.environ["CATS_CONFIG"] = old_config
            cats.config.reset_settings()


def make_run_manifest(run_id: str = "20260924T105317Z") -> RunManifest:
    """A RunManifest for writer tests: the run block writers receive at session end."""
    return RunManifest(
        run_id=run_id,
        started_at="2026-09-24T10:53:17.000000Z",
        finished_at="2026-09-24T10:53:19.000000Z",
        role_session_name=f"cats-{run_id}",
        caller_identity={"account_id": "999999999999", "arn": "arn:aws:sts::999999999999:assumed-role/ci-runner/job-8812"},
        test_packs=[{"name": "acme-controls", "version": "0.99.4", "metadata": {"framework": "NIST-800-53r5"}}],
        git={"commit": "3f2a9c1e0d4b7f8a9c2e1d3f5b6a7c8d9e0f1a2b", "dirty": False},
        config_sha256="71aa0c2d6e8f1b3a5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b",
        cats_version="0.99.3",
        python_version="3.14.6",
        pytest_version="9.1.1",
        pytest_args=["-vv", "-ra"],
        config_path="cats.yaml",
        aws_region="us-east-1",
        partition="aws",
        cfn_stack_prefix="CATS-test-infra-",
        exit_status=0,
        selection={
            "keyword": "",
            "markexpr": "",
            "last_failed": False,
            "deselect": [],
            "explicit_args": [],
            "stopped_early": "",
            "provided": [],
            "config_excluded": [],
            "unmatched_exclusions": [],
        },
        counts={"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0},
        accounts=[{"account_name": "testaccount", "account_id": "111111111111"}],
        outputs=["report.json"],
        writers=[],
    )


def cats_summary(result: "pytest.RunResult") -> tuple[str, list[str]]:
    """The CATS section of a pytester run's terminal summary: its header title and the lines under it."""
    lines = result.stdout.lines
    header_index = next(index for index, line in enumerate(lines) if "CATS reports: run" in line)
    body: list[str] = []
    for line in lines[header_index + 1 :]:
        if line.startswith("="):
            break
        body.append(line)
    return lines[header_index].strip("-").strip(), body


class RequestAborted(Exception):
    """Raised from a before-send hook so no request reaches the network (or moto)."""


def capture_wire_user_agent(session: boto3.Session, client_config: Config | None = None) -> str:
    """Return the assembled User-Agent header for one STS GetCallerIdentity call, aborting before send.

    Config-level assertions cannot prove a suffix survives header assembly; this reads the
    header botocore actually built. The hook is registered first so it runs ahead of moto's
    own before-send handler when a test has ``mock_aws`` active.
    """
    client = session.client("sts", config=client_config)
    captured: list[str] = []

    def grab(request: "AWSPreparedRequest", **kwargs: object) -> None:
        header = request.headers["User-Agent"]
        captured.append(header.decode("utf-8") if isinstance(header, bytes) else header)
        raise RequestAborted()

    client.meta.events.register_first("before-send.sts.GetCallerIdentity", grab)
    with contextlib.suppress(RequestAborted):
        client.get_caller_identity()
    return captured[0]


# =============================================================================
# The deploy readiness gate's role probe (cats/cfn/readiness.py RoleReadiness)
# =============================================================================

FAKE_ASSUMED_CREDENTIALS = {"AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "secret", "SessionToken": "token"}


@dataclass
class RoleProbeMocks:
    """The clients RoleReadiness builds: STS from the environment chain, CloudFormation from the assumed credentials."""

    attributed_session: MagicMock  # the patched cats.cfn.readiness.attributed_session
    sts: MagicMock
    cfn: MagicMock


def patch_role_probe(mocker: "MockerFixture", stack_name: str) -> RoleProbeMocks:
    """Make every RoleReadiness probe succeed: AssumeRole hands out FAKE_ASSUMED_CREDENTIALS, DescribeStacks sees `stack_name`.

    attributed_session() with no arguments is the environment chain and yields the STS mock;
    called with credentials it is the assumed role and yields the CloudFormation mock. Set a
    side_effect on either mock's method to make a probe fail.
    """
    sts = MagicMock()
    sts.assume_role.return_value = {"Credentials": FAKE_ASSUMED_CREDENTIALS}
    cfn = MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": [{"StackName": stack_name}]}
    environment_session = MagicMock()
    environment_session.client.return_value = sts
    assumed_session = MagicMock()
    assumed_session.client.return_value = cfn
    patched = mocker.patch("cats.cfn.readiness.attributed_session", side_effect=lambda **kwargs: assumed_session if kwargs else environment_session)
    return RoleProbeMocks(attributed_session=patched, sts=sts, cfn=cfn)
