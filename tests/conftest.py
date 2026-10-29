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

import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
import yaml
from moto import mock_aws
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode
from typer.testing import CliRunner

# Typer forces terminal rendering when GITHUB_ACTIONS is set, emitting ANSI attribute
# codes that NO_COLOR does not suppress and breaking exact-output CLI assertions. The
# escape hatch is read when typer.rich_utils is first imported, which happens lazily on
# first help render - after this module loads.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

# Enable pytester fixture for plugin testing
pytest_plugins = ["pytester"]


def pytest_configure(config: pytest.Config) -> None:
    """Force loadscope distribution for xdist.

    Integration tests use module-scoped fixtures that deploy real AWS infrastructure.
    Without loadscope, xdist distributes tests from the same module to different workers,
    causing each worker to try deploying the same stack (AlreadyExistsException).
    """
    if hasattr(config.option, "dist"):
        config.option.dist = "loadscope"


class YAMLSnapshotExtension(SingleFileSnapshotExtension):
    _write_mode = WriteMode.TEXT
    file_extension = "yaml"


class CFNSnapshotExtension(YAMLSnapshotExtension):
    """YAML snapshot that validates CFN syntax on comparison."""

    def serialize(self, data: str, **kwargs: object) -> str:
        assert_valid_cfn_yaml(data)
        return data


class PythonSnapshotExtension(SingleFileSnapshotExtension):
    _write_mode = WriteMode.TEXT
    file_extension = "py"


@pytest.fixture
def snapshot_yaml(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(YAMLSnapshotExtension)


@pytest.fixture
def snapshot_cfn(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Snapshot that validates CFN YAML syntax."""
    return snapshot.use_extension(CFNSnapshotExtension)


@pytest.fixture
def snapshot_py(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(PythonSnapshotExtension)


# Register CFN intrinsic functions for YAML parsing
_CFN_TAGS = [
    "!Ref",
    "!GetAtt",
    "!Sub",
    "!Join",
    "!If",
    "!Equals",
    "!Not",
    "!And",
    "!Or",
    "!FindInMap",
    "!Select",
    "!Split",
    "!Base64",
    "!Cidr",
    "!GetAZs",
    "!ImportValue",
    "!Condition",
]
for _tag in _CFN_TAGS:
    yaml.add_constructor(_tag, lambda loader, node: None, Loader=yaml.SafeLoader)


def assert_valid_cfn_yaml(template: str) -> None:
    """Assert template is syntactically valid YAML with CFN intrinsic functions."""
    yaml.safe_load(template)


@pytest.fixture
def cats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary cats.yaml path and set CATS_CONFIG env var."""
    config_file = tmp_path / "cats.yaml"
    monkeypatch.setenv("CATS_CONFIG", str(config_file))
    return config_file


@pytest.fixture
def isolated_yaml(tmp_path: Path) -> Path:
    """Temp path for yaml file without setting CATS_CONFIG env var.

    Use this when testing config validation to avoid polluting the
    environment that the CATS pytest plugin reads from.
    """
    return tmp_path / "cats.yaml"


@pytest.fixture
def mock_console() -> MagicMock:
    """Mock Rich console that reports as non-terminal.

    Progress classes use console.is_terminal to decide whether to create
    Rich Live displays. In tests, we want is_terminal=False to skip
    live-updating terminal output that would block or produce control sequences.
    """
    console = MagicMock()
    console.is_terminal = False
    return console


@pytest.fixture
def cli_runner() -> CliRunner:
    """Typer CLI test runner with consistent terminal settings.

    - COLUMNS=200: Wide terminal to prevent Rich from wrapping output
    - NO_COLOR=1: Disable ANSI color codes for predictable output matching
    - AWS_SHARED_CREDENTIALS_FILE unset: a boto session reads it, so a shell that points it at
      a nonexistent file (CI's hostile shell does) would break session building in a CLI test.
      `cats run write-profile` reads AWS_CONFIG_FILE instead; a test that exercises the writer
      neutralizes that itself (see write_profile_runner in test_cli_run.py).
    """
    return CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "AWS_SHARED_CREDENTIALS_FILE": None})


@pytest.fixture
def mock_aws_infra() -> Generator[dict[str, str], None, None]:
    """Create VPCs and subnets in moto for tests needing AWS infrastructure."""
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        ec2 = session.client("ec2")
        ids: dict[str, str] = {}
        ids["VPC"] = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        ids["VPC2"] = ec2.create_vpc(CidrBlock="10.1.0.0/16")["Vpc"]["VpcId"]
        ids["SUBNET_A"] = ec2.create_subnet(VpcId=ids["VPC"], CidrBlock="10.0.1.0/24", AvailabilityZone="us-east-1a")["Subnet"]["SubnetId"]
        ids["SUBNET_B"] = ec2.create_subnet(VpcId=ids["VPC"], CidrBlock="10.0.2.0/24", AvailabilityZone="us-east-1b")["Subnet"]["SubnetId"]
        ids["SUBNET2_A"] = ec2.create_subnet(VpcId=ids["VPC2"], CidrBlock="10.1.1.0/24", AvailabilityZone="us-east-1c")["Subnet"]["SubnetId"]
        ids["SG"] = ec2.create_security_group(GroupName="test-sg", Description="test", VpcId=ids["VPC"])["GroupId"]
        yield ids
