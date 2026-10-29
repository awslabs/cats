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

"""Tests for _parse_cfn_outputs_to_network_targets function."""

import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from pytest_mock import MockerFixture

import cats.config
from cats.config import AWS_REGION_UNSET_MESSAGE
from cats.constants import DeployedEC2Resources, DeployedLambdaResources, DeployedTargetResources
from cats.exceptions import CatsConfigError, CatsInfrastructureError, CatsUsageError, CatsVersionMismatchError
from cats.resources.account_under_test import AccountUnderTest, _parse_cfn_outputs_to_network_targets
from cats.resources.network_interact import lookup_udp_receipt_token
from cats.version import __version__
from tests.utils import make_config, make_stack_tags, write_config


@pytest.fixture
def cats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Create isolated cats.yaml for tests."""
    config_path = tmp_path / "cats.yaml"
    monkeypatch.setenv("CATS_CONFIG", str(config_path))
    cats.config.reset_settings()
    yield config_path
    cats.config.reset_settings()


def _write_test_config(
    cats_yaml: Path,
    network_targets: dict[str, dict[str, Any]],
) -> None:
    """Write a test config with specified network targets."""
    config = make_config(
        account_name="myaccount",
        account_id="123456789012",
        network_targets=network_targets,
        cfn_stack_prefix="CATS-",
    )
    write_config(cats_yaml, config)


def test_single_target_with_lambda() -> None:
    # arrange
    output_map = {
        "Ec2DnsNameprivate": "ip-10-0-1-5.ec2.internal",
        "Ec2InstanceIdprivate": "i-abc123",
        "VpcIdprivate": "vpc-111",
        "SubnetIdsprivate": "subnet-aaa,subnet-bbb",
        "ProbeLambdaNameprivate": "CATS-probe-private",
        "EnabledHandlersprivate": "lambda",
        "ResponseBucketName": "cats-bucket-123",
    }
    expected = {
        "private": DeployedTargetResources(
            response_bucket_name="cats-bucket-123",
            vpc_id="vpc-111",
            subnet_id_list=["subnet-aaa", "subnet-bbb"],
            account_name="testaccount",
            target_name="private",
            enabled_handlers=["lambda"],
            ec2=DeployedEC2Resources(dns_name="ip-10-0-1-5.ec2.internal", instance_id="i-abc123"),
            lambda_=DeployedLambdaResources(probe_lambda_name="CATS-probe-private"),
        )
    }

    # act
    result = _parse_cfn_outputs_to_network_targets(
        output_map=output_map,
        network_target_names=["private"],
        account_name="testaccount",
        stack_name="CATS-testaccount",
        response_bucket="cats-bucket-123",
    )

    # assert
    assert result == expected


def test_lambda_enabled_without_probe_output_raises_stale_stack_error() -> None:
    # arrange - a pre-1.0 stack: EnabledHandlers says lambda, but the outputs carry the old *LambdaName keys
    output_map = {
        "Ec2DnsNameprivate": "ip-10-0-1-5.ec2.internal",
        "Ec2InstanceIdprivate": "i-abc123",
        "VpcIdprivate": "vpc-111",
        "SubnetIdsprivate": "subnet-aaa",
        "HttpLambdaNameprivate": "CATS-http-private",
        "TcpLambdaNameprivate": "CATS-tcp-private",
        "UdpLambdaNameprivate": "CATS-udp-private",
        "EnabledHandlersprivate": "lambda,ec2",
        "ResponseBucketName": "cats-bucket-123",
    }
    expected_error = (
        "Stack CATS-testaccount was deployed by another CATS release and its outputs do not match this version. "
        + "Recreate it: cats infra destroy testaccount && cats infra deploy testaccount"
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        _parse_cfn_outputs_to_network_targets(
            output_map=output_map,
            network_target_names=["private"],
            account_name="testaccount",
            stack_name="CATS-testaccount",
            response_bucket="cats-bucket-123",
        )
    assert str(exc_info.value) == expected_error


def test_ec2_only_without_probe_output_is_not_an_error() -> None:
    # arrange - a legitimate EC2-only stack has no ProbeLambdaName output and no lambda handler
    output_map = {
        "Ec2DnsNameprivate": "ip-10-0-1-5.ec2.internal",
        "Ec2InstanceIdprivate": "i-abc123",
        "VpcIdprivate": "vpc-111",
        "SubnetIdsprivate": "subnet-aaa",
        "EnabledHandlersprivate": "ec2",
        "ResponseBucketName": "cats-bucket-123",
    }

    # act
    result = _parse_cfn_outputs_to_network_targets(
        output_map=output_map,
        network_target_names=["private"],
        account_name="testaccount",
        stack_name="CATS-testaccount",
        response_bucket="cats-bucket-123",
    )

    # assert
    assert result["private"].lambda_ is None


def test_single_target_ec2_only() -> None:
    # arrange
    output_map = {
        "Ec2DnsNameprivate": "ip-10-0-1-5.ec2.internal",
        "Ec2InstanceIdprivate": "i-abc123",
        "VpcIdprivate": "vpc-111",
        "SubnetIdsprivate": "subnet-aaa",
        "EnabledHandlersprivate": "ec2",
        "ResponseBucketName": "cats-bucket-123",
    }
    expected = {
        "private": DeployedTargetResources(
            response_bucket_name="cats-bucket-123",
            vpc_id="vpc-111",
            subnet_id_list=["subnet-aaa"],
            account_name="testaccount",
            target_name="private",
            enabled_handlers=["ec2"],
            ec2=DeployedEC2Resources(dns_name="ip-10-0-1-5.ec2.internal", instance_id="i-abc123"),
            lambda_=None,
        )
    }

    # act
    result = _parse_cfn_outputs_to_network_targets(
        output_map=output_map,
        network_target_names=["private"],
        account_name="testaccount",
        stack_name="CATS-testaccount",
        response_bucket="cats-bucket-123",
    )

    # assert
    assert result == expected


def test_multiple_targets() -> None:
    # arrange
    output_map = {
        "Ec2DnsNameprivate": "ip-10-0-1-5.ec2.internal",
        "Ec2InstanceIdprivate": "i-abc123",
        "VpcIdprivate": "vpc-111",
        "SubnetIdsprivate": "subnet-aaa",
        "EnabledHandlersprivate": "ec2",
        "Ec2DnsNamepublic": "ip-10-0-2-5.ec2.internal",
        "Ec2InstanceIdpublic": "i-def456",
        "VpcIdpublic": "vpc-222",
        "SubnetIdspublic": "subnet-bbb,subnet-ccc",
        "EnabledHandlerspublic": "ec2",
        "ResponseBucketName": "cats-bucket-123",
    }
    expected = {
        "private": DeployedTargetResources(
            response_bucket_name="cats-bucket-123",
            vpc_id="vpc-111",
            subnet_id_list=["subnet-aaa"],
            account_name="testaccount",
            target_name="private",
            enabled_handlers=["ec2"],
            ec2=DeployedEC2Resources(dns_name="ip-10-0-1-5.ec2.internal", instance_id="i-abc123"),
            lambda_=None,
        ),
        "public": DeployedTargetResources(
            response_bucket_name="cats-bucket-123",
            vpc_id="vpc-222",
            subnet_id_list=["subnet-bbb", "subnet-ccc"],
            account_name="testaccount",
            target_name="public",
            enabled_handlers=["ec2"],
            ec2=DeployedEC2Resources(dns_name="ip-10-0-2-5.ec2.internal", instance_id="i-def456"),
            lambda_=None,
        ),
    }

    # act
    result = _parse_cfn_outputs_to_network_targets(
        output_map=output_map,
        network_target_names=["private", "public"],
        account_name="testaccount",
        stack_name="CATS-testaccount",
        response_bucket="cats-bucket-123",
    )

    # assert
    assert result == expected


def test_missing_output_raises_error_with_redeploy_hint() -> None:
    # arrange - config has target "private" but stack outputs don't include it
    # (happens when config changed after deploy)
    output_map: dict[str, str] = {
        "ResponseBucketName": "cats-bucket-123",
    }
    expected_error = (
        "Stack output 'Ec2DnsNameprivate' not found for network target 'private' in target account 'testaccount'. "
        + "This usually means cats.yaml changed since last deploy. Redeploy: cats infra deploy testaccount"
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        _parse_cfn_outputs_to_network_targets(
            output_map=output_map,
            network_target_names=["private"],
            account_name="testaccount",
            stack_name="CATS-testaccount",
            response_bucket="cats-bucket-123",
        )

    assert str(exc_info.value) == expected_error


def test_stack_not_found_raises_error_with_deploy_hint(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]}})

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.side_effect = ClientError(
        {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
        "DescribeStacks",
    )

    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn

    mocker.patch.object(
        AccountUnderTest,
        "role",
        mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)),
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError, match=r"CATS stack .* not found.*cats infra deploy"):
        AccountUnderTest("myaccount")


def test_unknown_account_name_raises_cats_config_error(cats_yaml: Path) -> None:
    # arrange
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]}})
    expected_message = f"Account 'nope' not found in {cats_yaml}. Available accounts: myaccount"

    # act & assert
    with pytest.raises(CatsConfigError, match=f"^{re.escape(expected_message)}$"):
        AccountUnderTest("nope")


def test_unset_region_raises_cats_config_error_before_any_aws_call(cats_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.delenv("AWS_REGION", raising=False)
    config = make_config(account_name="myaccount", account_id="123456789012", cfn_stack_prefix="CATS-")
    del config["cats_deployment_settings"]["aws_region"]
    write_config(cats_yaml, config)

    # act & assert
    with pytest.raises(CatsConfigError, match=f"^{re.escape(AWS_REGION_UNSET_MESSAGE)}$"):
        AccountUnderTest("myaccount")


def test_account_id_is_read_from_config(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]}})
    _mock_account_role(mocker, _private_target_outputs([]))

    # act
    account = AccountUnderTest("myaccount")

    # assert
    assert account.account_id == "123456789012"


def test_getattr_nonexistent_target_raises_with_available_targets(mocker: MockerFixture, cats_yaml: Path) -> None:
    """Error message should show available targets when accessing non-existent one."""
    # arrange
    _write_test_config(
        cats_yaml,
        {
            "private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]},
            "public": {"vpc_id": "vpc-222", "subnet_ids": ["subnet-bbb"]},
        },
    )

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "cats-bucket"},
                    {"OutputKey": "Ec2DnsNameprivate", "OutputValue": "ip-10.ec2.internal"},
                    {"OutputKey": "Ec2InstanceIdprivate", "OutputValue": "i-abc"},
                    {"OutputKey": "VpcIdprivate", "OutputValue": "vpc-111"},
                    {"OutputKey": "SubnetIdsprivate", "OutputValue": "subnet-aaa"},
                    {"OutputKey": "EnabledHandlersprivate", "OutputValue": "ec2"},
                    {"OutputKey": "Ec2DnsNamepublic", "OutputValue": "ip-20.ec2.internal"},
                    {"OutputKey": "Ec2InstanceIdpublic", "OutputValue": "i-def"},
                    {"OutputKey": "VpcIdpublic", "OutputValue": "vpc-222"},
                    {"OutputKey": "SubnetIdspublic", "OutputValue": "subnet-bbb"},
                    {"OutputKey": "EnabledHandlerspublic", "OutputValue": "ec2"},
                ],
            }
        ]
    }

    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn

    mocker.patch.object(
        AccountUnderTest,
        "role",
        mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)),
    )

    account = AccountUnderTest("myaccount")
    expected_error = "Network target 'nonexistent' not found for account 'myaccount'. Available targets: ['private', 'public']"

    # act & assert
    with pytest.raises(AttributeError) as exc_info:
        _ = account.nonexistent

    assert str(exc_info.value) == expected_error


def test_available_network_targets_returns_deployed_target_names(mocker: MockerFixture, cats_yaml: Path) -> None:
    """available_network_targets should return list of deployed target names."""
    # arrange
    _write_test_config(
        cats_yaml,
        {
            "app": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]},
            "web": {"vpc_id": "vpc-222", "subnet_ids": ["subnet-bbb"]},
        },
    )

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "cats-bucket"},
                    {"OutputKey": "Ec2DnsNameapp", "OutputValue": "ip-10.ec2.internal"},
                    {"OutputKey": "Ec2InstanceIdapp", "OutputValue": "i-abc"},
                    {"OutputKey": "VpcIdapp", "OutputValue": "vpc-111"},
                    {"OutputKey": "SubnetIdsapp", "OutputValue": "subnet-aaa"},
                    {"OutputKey": "EnabledHandlersapp", "OutputValue": "ec2"},
                    {"OutputKey": "Ec2DnsNameweb", "OutputValue": "ip-20.ec2.internal"},
                    {"OutputKey": "Ec2InstanceIdweb", "OutputValue": "i-def"},
                    {"OutputKey": "VpcIdweb", "OutputValue": "vpc-222"},
                    {"OutputKey": "SubnetIdsweb", "OutputValue": "subnet-bbb"},
                    {"OutputKey": "EnabledHandlersweb", "OutputValue": "ec2"},
                ],
            }
        ]
    }

    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn

    mocker.patch.object(
        AccountUnderTest,
        "role",
        mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)),
    )

    account = AccountUnderTest("myaccount")
    expected = ["app", "web"]

    # act
    result = account.available_network_targets

    # assert
    assert result == expected


def test_network_handlers_iterates_with_target_name_on_metadata(mocker: MockerFixture, cats_yaml: Path) -> None:
    """network_handlers should yield handlers with target_name on metadata."""
    # arrange
    _write_test_config(
        cats_yaml,
        {
            "app": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]},
            "web": {"vpc_id": "vpc-222", "subnet_ids": ["subnet-bbb"]},
        },
    )

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "cats-bucket"},
                    {"OutputKey": "Ec2DnsNameapp", "OutputValue": "ip-10.ec2.internal"},
                    {"OutputKey": "Ec2InstanceIdapp", "OutputValue": "i-abc"},
                    {"OutputKey": "VpcIdapp", "OutputValue": "vpc-111"},
                    {"OutputKey": "SubnetIdsapp", "OutputValue": "subnet-aaa"},
                    {"OutputKey": "EnabledHandlersapp", "OutputValue": "ec2"},
                    {"OutputKey": "Ec2DnsNameweb", "OutputValue": "ip-20.ec2.internal"},
                    {"OutputKey": "Ec2InstanceIdweb", "OutputValue": "i-def"},
                    {"OutputKey": "VpcIdweb", "OutputValue": "vpc-222"},
                    {"OutputKey": "SubnetIdsweb", "OutputValue": "subnet-bbb"},
                    {"OutputKey": "EnabledHandlersweb", "OutputValue": "ec2"},
                ],
            }
        ]
    }

    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn

    mocker.patch.object(
        AccountUnderTest,
        "role",
        mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)),
    )

    account = AccountUnderTest("myaccount")

    # act
    handlers = list(account.network_handlers)
    target_names = [h._deployed.target_name for h in handlers]

    # assert
    assert len(handlers) == 2
    assert set(target_names) == {"app", "web"}


def test_network_handlers_by_tag_filters_by_key(mocker: MockerFixture, cats_yaml: Path) -> None:
    """network_handlers_by_tag returns only handlers with matching tag key."""
    # arrange
    _write_test_config(
        cats_yaml,
        {
            "app": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"], "tags": {"internal": None}},
            "web": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-2"], "tags": {"public": None}},
        },
    )

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "bucket"},
                    {"OutputKey": "Ec2DnsNameapp", "OutputValue": "ip.internal"},
                    {"OutputKey": "Ec2InstanceIdapp", "OutputValue": "i-abc"},
                    {"OutputKey": "VpcIdapp", "OutputValue": "vpc-1"},
                    {"OutputKey": "SubnetIdsapp", "OutputValue": "subnet-1"},
                    {"OutputKey": "EnabledHandlersapp", "OutputValue": "ec2"},
                    {"OutputKey": "Ec2DnsNameweb", "OutputValue": "ip.internal"},
                    {"OutputKey": "Ec2InstanceIdweb", "OutputValue": "i-def"},
                    {"OutputKey": "VpcIdweb", "OutputValue": "vpc-1"},
                    {"OutputKey": "SubnetIdsweb", "OutputValue": "subnet-2"},
                    {"OutputKey": "EnabledHandlersweb", "OutputValue": "ec2"},
                ],
            }
        ]
    }
    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn
    mocker.patch.object(AccountUnderTest, "role", mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)))

    account = AccountUnderTest("myaccount")

    # act
    handlers = list(account.network_handlers_by_tag("internal"))

    # assert
    assert len(handlers) == 1
    assert handlers[0].name == "app"


def test_get_network_handler_by_tag_returns_single_match(mocker: MockerFixture, cats_yaml: Path) -> None:
    """get_network_handler_by_tag returns handler when exactly one matches."""
    # arrange
    _write_test_config(
        cats_yaml,
        {
            "app": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"], "tags": {"routing": "private"}},
            "web": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-2"], "tags": {"routing": "public"}},
        },
    )

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "bucket"},
                    {"OutputKey": "Ec2DnsNameapp", "OutputValue": "ip.internal"},
                    {"OutputKey": "Ec2InstanceIdapp", "OutputValue": "i-abc"},
                    {"OutputKey": "VpcIdapp", "OutputValue": "vpc-1"},
                    {"OutputKey": "SubnetIdsapp", "OutputValue": "subnet-1"},
                    {"OutputKey": "EnabledHandlersapp", "OutputValue": "ec2"},
                    {"OutputKey": "Ec2DnsNameweb", "OutputValue": "ip.internal"},
                    {"OutputKey": "Ec2InstanceIdweb", "OutputValue": "i-def"},
                    {"OutputKey": "VpcIdweb", "OutputValue": "vpc-1"},
                    {"OutputKey": "SubnetIdsweb", "OutputValue": "subnet-2"},
                    {"OutputKey": "EnabledHandlersweb", "OutputValue": "ec2"},
                ],
            }
        ]
    }
    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn
    mocker.patch.object(AccountUnderTest, "role", mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)))

    account = AccountUnderTest("myaccount")

    # act
    handler = account.get_network_handler_by_tag("routing", "private")

    # assert
    assert handler.name == "app"


def test_get_network_handler_by_tag_raises_when_no_match(mocker: MockerFixture, cats_yaml: Path) -> None:
    """get_network_handler_by_tag raises CatsUsageError when no handler matches."""
    # arrange
    _write_test_config(cats_yaml, {"app": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"]}})

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "bucket"},
                    {"OutputKey": "Ec2DnsNameapp", "OutputValue": "ip.internal"},
                    {"OutputKey": "Ec2InstanceIdapp", "OutputValue": "i-abc"},
                    {"OutputKey": "VpcIdapp", "OutputValue": "vpc-1"},
                    {"OutputKey": "SubnetIdsapp", "OutputValue": "subnet-1"},
                    {"OutputKey": "EnabledHandlersapp", "OutputValue": "ec2"},
                ],
            }
        ]
    }
    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn
    mocker.patch.object(AccountUnderTest, "role", mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)))

    account = AccountUnderTest("myaccount")

    # act & assert
    with pytest.raises(CatsUsageError, match="No network handler found with tag 'nonexistent'"):
        account.get_network_handler_by_tag("nonexistent")


def test_get_network_handler_by_tag_raises_when_multiple_match(mocker: MockerFixture, cats_yaml: Path) -> None:
    """get_network_handler_by_tag raises CatsUsageError when multiple handlers match."""
    # arrange
    _write_test_config(
        cats_yaml,
        {
            "app": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"], "tags": {"internal": None}},
            "web": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-2"], "tags": {"internal": None}},
        },
    )

    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": make_stack_tags(),
                "Outputs": [
                    {"OutputKey": "ResponseBucketName", "OutputValue": "bucket"},
                    {"OutputKey": "Ec2DnsNameapp", "OutputValue": "ip.internal"},
                    {"OutputKey": "Ec2InstanceIdapp", "OutputValue": "i-abc"},
                    {"OutputKey": "VpcIdapp", "OutputValue": "vpc-1"},
                    {"OutputKey": "SubnetIdsapp", "OutputValue": "subnet-1"},
                    {"OutputKey": "EnabledHandlersapp", "OutputValue": "ec2"},
                    {"OutputKey": "Ec2DnsNameweb", "OutputValue": "ip.internal"},
                    {"OutputKey": "Ec2InstanceIdweb", "OutputValue": "i-def"},
                    {"OutputKey": "VpcIdweb", "OutputValue": "vpc-1"},
                    {"OutputKey": "SubnetIdsweb", "OutputValue": "subnet-2"},
                    {"OutputKey": "EnabledHandlersweb", "OutputValue": "ec2"},
                ],
            }
        ]
    }
    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn
    mocker.patch.object(AccountUnderTest, "role", mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)))

    account = AccountUnderTest("myaccount")

    # act & assert
    with pytest.raises(CatsUsageError, match="Multiple network handlers found with tag 'internal'"):
        account.get_network_handler_by_tag("internal")


# =============================================================================
# UdpReceiptToken output parsing and registry population
# =============================================================================

RECEIPT_TOKEN = "0123456789abcdef0123456789abcdef"  # noqa: S105


def _mock_account_role(mocker: MockerFixture, outputs: list[dict[str, str]], version: str | None = __version__) -> None:
    """Patch AccountUnderTest.role so init reads the given stack outputs and version tag."""
    mock_cfn = mocker.MagicMock()
    mock_cfn.describe_stacks.return_value = {"Stacks": [{"Tags": make_stack_tags(version), "Outputs": outputs}]}
    mock_session = mocker.MagicMock()
    mock_session.client.return_value = mock_cfn
    mocker.patch.object(
        AccountUnderTest,
        "role",
        mocker.PropertyMock(return_value=mocker.MagicMock(default=mock_session)),
    )


def _private_target_outputs(extra: list[dict[str, str]]) -> list[dict[str, str]]:
    """Stack outputs for one ec2-only target named 'private', plus any extra outputs."""
    return [
        {"OutputKey": "ResponseBucketName", "OutputValue": "cats-bucket"},
        {"OutputKey": "Ec2DnsNameprivate", "OutputValue": "ip-10-0-1-5.ec2.internal"},
        {"OutputKey": "Ec2InstanceIdprivate", "OutputValue": "i-abc"},
        {"OutputKey": "VpcIdprivate", "OutputValue": "vpc-111"},
        {"OutputKey": "SubnetIdsprivate", "OutputValue": "subnet-aaa"},
        {"OutputKey": "EnabledHandlersprivate", "OutputValue": "ec2"},
        *extra,
    ]


def test_udp_receipt_token_output_present_sets_property(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, _private_target_outputs([{"OutputKey": "UdpReceiptToken", "OutputValue": RECEIPT_TOKEN}]))

    # act
    account = AccountUnderTest("myaccount")

    # assert
    assert account.udp_receipt_token == RECEIPT_TOKEN


def test_udp_receipt_token_output_present_registers_target_dns(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, _private_target_outputs([{"OutputKey": "UdpReceiptToken", "OutputValue": RECEIPT_TOKEN}]))

    # act
    AccountUnderTest("myaccount")

    # assert
    assert lookup_udp_receipt_token("ip-10-0-1-5.ec2.internal") == RECEIPT_TOKEN


def test_udp_receipt_token_output_absent_property_is_none(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange - a pre-token stack has no UdpReceiptToken output
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, _private_target_outputs([]))

    # act
    account = AccountUnderTest("myaccount")

    # assert
    assert account.udp_receipt_token is None


def test_udp_receipt_token_output_absent_registers_nothing(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, _private_target_outputs([]))

    # act
    AccountUnderTest("myaccount")

    # assert
    assert lookup_udp_receipt_token("ip-10-0-1-5.ec2.internal") is None


# =============================================================================
# AccountUnderTest deployed version
# =============================================================================


def test_deployed_version_is_read_from_stack_tag(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange
    mocker.patch("cats.resources.account_under_test.__version__", "1.0.0")
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, _private_target_outputs([]), version="1.0.0")
    expected = "1.0.0"

    # act
    account = AccountUnderTest("myaccount")

    # assert
    assert expected == account.deployed_version


def test_stack_from_other_release_raises_before_outputs_are_parsed(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange - outputs are empty, which would raise a missing-output error if parsing ran first
    mocker.patch("cats.resources.account_under_test.__version__", "1.0.0")
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, [], version="0.99.0")
    expected_error = (
        "CATS stack 'CATS-myaccount' in target account 'myaccount' was deployed by CATS 0.99.0 "
        + "but CATS 1.0.0 is installed. Deployed listeners and handlers may not match this library. "
        + "Recreate it: cats infra destroy myaccount && cats infra deploy myaccount "
        + "(set CATS_SKIP_VERSION_CHECK=1 to bypass this check)"
    )

    # act
    with pytest.raises(CatsVersionMismatchError) as exc_info:
        AccountUnderTest("myaccount")

    # assert
    assert str(exc_info.value) == expected_error


def test_stack_without_version_tag_raises(mocker: MockerFixture, cats_yaml: Path) -> None:
    # arrange
    mocker.patch("cats.resources.account_under_test.__version__", "1.0.0")
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, _private_target_outputs([]), version=None)
    expected_error = (
        "CATS stack 'CATS-myaccount' in target account 'myaccount' has no cats:version tag, "
        + "so CATS cannot confirm it matches installed CATS 1.0.0. It was not created by 'cats infra deploy'. "
        + "Recreate it: cats infra destroy myaccount && cats infra deploy myaccount "
        + "(set CATS_SKIP_VERSION_CHECK=1 to bypass this check)"
    )

    # act
    with pytest.raises(CatsVersionMismatchError) as exc_info:
        AccountUnderTest("myaccount")

    # assert
    assert str(exc_info.value) == expected_error


def test_skip_env_var_lets_stack_from_other_release_construct(mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch, cats_yaml: Path) -> None:
    # arrange
    mocker.patch("cats.resources.account_under_test.__version__", "1.0.0")
    monkeypatch.setenv("CATS_SKIP_VERSION_CHECK", "1")
    _write_test_config(cats_yaml, {"private": {"vpc_id": "vpc-111", "subnet_ids": ["subnet-aaa"]}})
    _mock_account_role(mocker, _private_target_outputs([]), version="0.99.0")
    expected = "0.99.0"

    # act
    account = AccountUnderTest("myaccount")

    # assert
    assert expected == account.deployed_version
