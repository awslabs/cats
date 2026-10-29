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

"""Contract tests: AccountUnderTest and its handlers must snapshot Settings at construction.

If future changes reintroduce `Settings()` reads inside the resources layer (outside
`AccountUnderTest.__init__`), the `get_account()` cache in `cats/resources/__init__.py`
will return objects that silently use stale config after a `reset_settings()` call.

These tests construct an AccountUnderTest against config A, then wipe Settings() and
load config B, then assert the already-constructed instance (and its handlers) still
reflect A.
"""

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

import cats.config
from cats.resources.account_under_test import AccountUnderTest
from tests.utils import make_config, make_ssm_response, make_stack_tags, write_config

_PREFIX_A = "CATS-config-A-"
_REGION_A = "eu-west-1"
_PARTITION_A = "aws"

_PREFIX_B = "CATS-config-B-"
_REGION_B = "cn-north-1"
_PARTITION_B = "aws-cn"

_ACCOUNT_NAME = "myaccount"
_ACCOUNT_ID = "123456789012"
_TARGET_NAME = "private"


def _config(prefix: str, region: str, partition: str) -> dict[str, Any]:
    """Build a config with given deployment settings. partition isn't exposed via make_config."""
    cfg = make_config(
        account_name=_ACCOUNT_NAME,
        account_id=_ACCOUNT_ID,
        network_targets={_TARGET_NAME: {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}},
        cfn_stack_prefix=prefix,
        aws_region=region,
    )
    cfg["cats_deployment_settings"]["partition"] = partition
    return cfg


def _cfn_describe_stacks_response() -> dict[str, Any]:
    return {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "cats-bucket"},
                    {"OutputKey": f"Ec2DnsName{_TARGET_NAME}", "OutputValue": "ip-10.ec2.internal"},
                    {"OutputKey": f"Ec2InstanceId{_TARGET_NAME}", "OutputValue": "i-abc"},
                    {"OutputKey": f"VpcId{_TARGET_NAME}", "OutputValue": "vpc-111"},
                    {"OutputKey": f"SubnetIds{_TARGET_NAME}", "OutputValue": "subnet-aaa"},
                    {"OutputKey": f"EnabledHandlers{_TARGET_NAME}", "OutputValue": "ec2"},
                ],
            }
        ]
    }


@pytest.fixture
def cats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Isolated cats.yaml path."""
    path = tmp_path / "cats.yaml"
    monkeypatch.setenv("CATS_CONFIG", str(path))
    cats.config.reset_settings()
    yield path
    cats.config.reset_settings()


@pytest.fixture
def mock_session_boundary(mocker: MockerFixture) -> dict[str, MagicMock]:
    """Mock get_test_session and get_role_arn at their source so SessionHandler runs real code.

    Returns the mocks plus a pre-programmed ssm client for handler-path tests.
    """
    mock_ssm = MagicMock()
    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.return_value = _cfn_describe_stacks_response()

    mock_session = MagicMock()
    mock_session.client.side_effect = lambda service: {"cloudformation": mock_cfn, "ssm": mock_ssm}[service]

    mock_get_session = mocker.patch("cats.resources.session_interact.get_test_session", return_value=mock_session)
    mock_get_arn = mocker.patch(
        "cats.resources.session_interact.get_role_arn",
        side_effect=lambda role_name, account_id, partition: f"arn:{partition}:iam::{account_id}:role/{role_name}",
    )
    return {"get_test_session": mock_get_session, "get_role_arn": mock_get_arn, "ssm": mock_ssm}


@pytest.fixture
def account_a(cats_yaml: Path, mock_session_boundary: dict[str, MagicMock]) -> AccountUnderTest:
    """AccountUnderTest constructed against config A."""
    write_config(cats_yaml, _config(_PREFIX_A, _REGION_A, _PARTITION_A))
    return AccountUnderTest(_ACCOUNT_NAME)


def _switch_to_config_b(cats_yaml: Path) -> None:
    """Wipe Settings() and reload with config B."""
    write_config(cats_yaml, _config(_PREFIX_B, _REGION_B, _PARTITION_B))
    # write_config already calls reset_settings(); force a reload
    _ = cats.config.Settings()


def test_account_attrs_match_construction_time_config_after_settings_swap(cats_yaml: Path, account_a: AccountUnderTest) -> None:
    # arrange
    _switch_to_config_b(cats_yaml)
    expected = {
        "cfn_stack_prefix": _PREFIX_A,
        "aws_region": _REGION_A,
        "partition": _PARTITION_A,
    }

    # act
    result = {
        "cfn_stack_prefix": account_a.cfn_stack_prefix,
        "aws_region": account_a.aws_region,
        "partition": account_a.partition,
    }

    # assert
    assert result == expected


def test_handler_ssm_document_uses_construction_time_cfn_stack_prefix(
    cats_yaml: Path,
    account_a: AccountUnderTest,
    mock_session_boundary: dict[str, MagicMock],
) -> None:
    # arrange
    _switch_to_config_b(cats_yaml)
    mock_ssm = mock_session_boundary["ssm"]
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")
    expected_doc_name = f"{_PREFIX_A}{_ACCOUNT_NAME}-Probe"

    # act
    account_a.private.ec2_http_call("target.internal", 8080)

    # assert
    assert mock_ssm.send_command.call_args.kwargs["DocumentName"] == expected_doc_name


def test_session_default_uses_construction_time_aws_region(
    cats_yaml: Path,
    account_a: AccountUnderTest,
    mock_session_boundary: dict[str, MagicMock],
) -> None:
    # arrange - the default session is assumed once, at construction, and reused: the region it carries is A's
    _switch_to_config_b(cats_yaml)
    mock_get_session = mock_session_boundary["get_test_session"]

    # act
    session = account_a.session

    # assert
    expected = (mock_get_session.return_value, 1, _REGION_A)
    assert (session, mock_get_session.call_count, mock_get_session.call_args.kwargs["aws_region"]) == expected


def test_session_default_uses_construction_time_partition(
    cats_yaml: Path,
    account_a: AccountUnderTest,
    mock_session_boundary: dict[str, MagicMock],
) -> None:
    # arrange
    _switch_to_config_b(cats_yaml)
    mock_get_arn = mock_session_boundary["get_role_arn"]

    # act
    _ = account_a.session

    # assert - the one role ARN ever built for the default role used A's partition
    assert [c.kwargs["partition"] for c in mock_get_arn.call_args_list] == [_PARTITION_A]
