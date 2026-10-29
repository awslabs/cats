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

"""Tests for cats.cfn.resolve - AMI and security group resolution in resolve_account."""

from typing import Any

import boto3
import pytest
from pytest_mock import MockerFixture

from cats.cfn.resolve import resolve_account
from cats.config import CatsConfig
from cats.exceptions import CatsInfrastructureError
from tests.utils import make_config

ACCOUNT_NAME = "testaccount"
TARGET_NAME = "privateSubnet"
SSM_AMI_PATH = "/cats/test/golden-ami"
SSM_AMI_VALUE = "ami-000000000000feed0"
SSM_SG_PATH = "/cats/test/lambda-security-group"
SSM_SG_VALUE = "sg-00000000000000000"


def make_resolve_config(infra: dict[str, str], existing_ec2_ami_id: str | None) -> CatsConfig:
    """Build a validated config with one network target and the given global AMI override."""
    config: dict[str, Any] = make_config(
        network_targets={TARGET_NAME: {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
    )
    config["global_network_settings"]["existing_ec2_ami_id"] = existing_ec2_ami_id
    return CatsConfig.model_validate(config)


def put_ssm_parameter(name: str, value: str) -> None:
    """Create an SSM parameter in moto."""
    ssm = boto3.Session(region_name="us-east-1").client("ssm")
    ssm.put_parameter(Name=name, Value=value, Type="String")


def test_literal_ami_override_passed_through(mock_aws_infra: dict[str, str]) -> None:
    """A literal ami- override is used as-is without any AWS lookup."""
    # arrange
    config = make_resolve_config(mock_aws_infra, "ami-0123456789abcdef0")
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    expected_ami_id = "ami-0123456789abcdef0"
    assert resolved.network_targets[TARGET_NAME].ami_id == expected_ami_id


def test_ssm_path_override_resolves_parameter(mock_aws_infra: dict[str, str]) -> None:
    """An SSM path override resolves to the parameter's AMI ID value."""
    # arrange
    put_ssm_parameter(SSM_AMI_PATH, SSM_AMI_VALUE)
    config = make_resolve_config(mock_aws_infra, SSM_AMI_PATH)
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    assert resolved.network_targets[TARGET_NAME].ami_id == SSM_AMI_VALUE


def test_target_level_ssm_path_override_resolves_parameter(mock_aws_infra: dict[str, str]) -> None:
    """A target-level SSM path override takes precedence over the global literal override."""
    # arrange
    put_ssm_parameter(SSM_AMI_PATH, SSM_AMI_VALUE)
    config_input: dict[str, Any] = make_config(
        network_targets={
            TARGET_NAME: {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"existing_ec2_ami_id": SSM_AMI_PATH},
            }
        },
    )
    config = CatsConfig.model_validate(config_input)
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    assert resolved.network_targets[TARGET_NAME].ami_id == SSM_AMI_VALUE


def test_ssm_path_resolving_to_non_ami_value_raises(mock_aws_infra: dict[str, str]) -> None:
    """A parameter whose value is not a plain AMI ID (e.g. a JSON blob) raises with guidance."""
    # arrange
    json_value = '{"image_id": "ami-0123456789abcdef0"}'
    put_ssm_parameter(SSM_AMI_PATH, json_value)
    config = make_resolve_config(mock_aws_infra, SSM_AMI_PATH)
    account = config.accounts_under_test[ACCOUNT_NAME]
    expected_message = (
        f"SSM parameter '{SSM_AMI_PATH}' (existing_ec2_ami_id in cats.yaml) resolved to "
        + '\'{"image_id": "ami-0123456789abcdef0"}\', which is not an AMI ID. '
        + "The parameter value must be a plain AMI ID (ami-...). "
        + "If this is a JSON 'recommended' parameter, use its /image_id sub-path instead."
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    assert str(exc_info.value) == expected_message


def test_missing_ssm_parameter_raises_infrastructure_error(mock_aws_infra: dict[str, str]) -> None:
    """An SSM path pointing at a nonexistent parameter raises the resolve error."""
    # arrange
    config = make_resolve_config(mock_aws_infra, "/cats/test/does-not-exist")
    account = config.accounts_under_test[ACCOUNT_NAME]
    expected_message = (
        f"Failed to resolve AWS resources for network target '{TARGET_NAME}' in target account '{ACCOUNT_NAME}' (defined in cats.yaml). "
        + "Check: 1) VPC IDs, subnet IDs and security group IDs in cats.yaml are correct, "
        + "2) Any SSM parameter paths (starting with /) exist and are readable, "
        + "3) The deploy role has ec2:DescribeSubnets, ec2:DescribeRouteTables, ssm:GetParameter permissions. "
        + "AWS error: ParameterNotFound"
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    assert str(exc_info.value) == expected_message


def test_no_override_uses_latest_ami_for_the_instance_type_architecture(mock_aws_infra: dict[str, str], mocker: MockerFixture) -> None:
    """With no override anywhere, the latest Amazon Linux 2023 AMI for the instance type's architecture is looked up."""
    # arrange
    mock_latest = mocker.patch("cats.cfn.resolve.get_latest_ami", return_value="ami-0fedcba9876543210")
    config_input: dict[str, Any] = make_config(
        network_targets={TARGET_NAME: {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    )
    config_input["global_network_settings"]["existing_ec2_ami_id"] = None
    config_input["global_network_settings"]["ec2_instance_type"] = "t4g.micro"
    config = CatsConfig.model_validate(config_input)
    account = config.accounts_under_test[ACCOUNT_NAME]
    session = boto3.Session(region_name="us-east-1")

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, session)

    # assert
    assert resolved.network_targets[TARGET_NAME].ami_id == "ami-0fedcba9876543210"
    mock_latest.assert_called_once_with(session, "us-east-1", "arm64")


def test_override_skips_the_architecture_lookup(mock_aws_infra: dict[str, str], mocker: MockerFixture) -> None:
    # arrange
    mock_architecture = mocker.patch("cats.cfn.resolve.get_instance_architecture")
    config = make_resolve_config(mock_aws_infra, "ami-0123456789abcdef0")
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    mock_architecture.assert_not_called()


def make_security_group_config(infra: dict[str, str], existing_security_group_ids: list[str] | None) -> CatsConfig:
    """Build a validated config with one network target and the given security group references."""
    target: dict[str, Any] = {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}
    if existing_security_group_ids is not None:
        target["existing_security_group_ids"] = existing_security_group_ids
    return CatsConfig.model_validate(make_config(network_targets={TARGET_NAME: target}))


def test_literal_security_group_id_passed_through(mock_aws_infra: dict[str, str]) -> None:
    """A literal sg- reference is used as-is without any AWS lookup."""
    # arrange
    config = make_security_group_config(mock_aws_infra, [mock_aws_infra["SG"]])
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    expected = [mock_aws_infra["SG"]]
    assert expected == resolved.network_targets[TARGET_NAME].existing_security_group_ids


def test_ssm_path_security_group_resolves_parameter(mock_aws_infra: dict[str, str]) -> None:
    """An SSM path resolves to the parameter's security group ID value."""
    # arrange
    put_ssm_parameter(SSM_SG_PATH, SSM_SG_VALUE)
    config = make_security_group_config(mock_aws_infra, [SSM_SG_PATH])
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    expected = [SSM_SG_VALUE]
    assert expected == resolved.network_targets[TARGET_NAME].existing_security_group_ids


def test_security_group_parameter_holding_several_ids_raises(mock_aws_infra: dict[str, str]) -> None:
    """A StringList-style parameter cannot be split into the template, so it must fail with guidance."""
    # arrange
    put_ssm_parameter(SSM_SG_PATH, f"{SSM_SG_VALUE},{SSM_SG_VALUE}")
    config = make_security_group_config(mock_aws_infra, [SSM_SG_PATH])
    account = config.accounts_under_test[ACCOUNT_NAME]
    expected_message = (
        f"existing_security_group_ids entry '{SSM_SG_PATH}' in cats.yaml resolved to "
        + f"'{SSM_SG_VALUE},{SSM_SG_VALUE}', which is not a security group ID. "
        + "Provide a security group ID (sg-...) or an SSM parameter path holding exactly one. "
        + "For several groups, list one entry per group in existing_security_group_ids."
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    assert str(exc_info.value) == expected_message


def test_unset_security_groups_resolve_to_none(mock_aws_infra: dict[str, str]) -> None:
    """Without existing_security_group_ids, the resolved target carries None so CATS creates its own."""
    # arrange
    config = make_security_group_config(mock_aws_infra, None)
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    assert resolved.network_targets[TARGET_NAME].existing_security_group_ids is None


def make_endpoint_security_group_config(infra: dict[str, str], interface_endpoint_security_group_ids: list[str] | None) -> CatsConfig:
    """Build a validated config with one network target, an ssm interface endpoint and the given endpoint security group references."""
    return CatsConfig.model_validate(
        make_config(
            network_targets={TARGET_NAME: {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            interface_endpoints=["ssm"],
            interface_endpoint_security_group_ids=interface_endpoint_security_group_ids,
        )
    )


def test_literal_interface_endpoint_security_group_id_passed_through(mock_aws_infra: dict[str, str]) -> None:
    """A literal sg- reference is used as-is without any AWS lookup."""
    # arrange
    config = make_endpoint_security_group_config(mock_aws_infra, [mock_aws_infra["SG"]])
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    expected = [mock_aws_infra["SG"]]
    assert expected == resolved.network_targets[TARGET_NAME].interface_endpoint_security_group_ids


def test_ssm_path_interface_endpoint_security_group_resolves_parameter(mock_aws_infra: dict[str, str]) -> None:
    """An SSM path resolves to the parameter's security group ID value."""
    # arrange
    put_ssm_parameter(SSM_SG_PATH, SSM_SG_VALUE)
    config = make_endpoint_security_group_config(mock_aws_infra, [SSM_SG_PATH])
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    expected = [SSM_SG_VALUE]
    assert expected == resolved.network_targets[TARGET_NAME].interface_endpoint_security_group_ids


def test_interface_endpoint_security_group_parameter_holding_several_ids_raises(mock_aws_infra: dict[str, str]) -> None:
    """The same guard as existing_security_group_ids, naming the field the user actually wrote."""
    # arrange
    put_ssm_parameter(SSM_SG_PATH, f"{SSM_SG_VALUE},{SSM_SG_VALUE}")
    config = make_endpoint_security_group_config(mock_aws_infra, [SSM_SG_PATH])
    account = config.accounts_under_test[ACCOUNT_NAME]
    expected_message = (
        f"interface_endpoint_security_group_ids entry '{SSM_SG_PATH}' in cats.yaml resolved to "
        + f"'{SSM_SG_VALUE},{SSM_SG_VALUE}', which is not a security group ID. "
        + "Provide a security group ID (sg-...) or an SSM parameter path holding exactly one. "
        + "For several groups, list one entry per group in interface_endpoint_security_group_ids."
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    assert str(exc_info.value) == expected_message


def test_unset_interface_endpoint_security_groups_resolve_to_none(mock_aws_infra: dict[str, str]) -> None:
    """Without interface_endpoint_security_group_ids, the resolved target carries None so the endpoints take the VPC's default group."""
    # arrange
    config = make_endpoint_security_group_config(mock_aws_infra, None)
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    assert resolved.network_targets[TARGET_NAME].interface_endpoint_security_group_ids is None


def test_latest_ami_lookup_cached_across_targets(mock_aws_infra: dict[str, str], mocker: MockerFixture) -> None:
    """The default AMI lookup runs once per account for targets sharing an instance type, not once per target."""
    # arrange
    mock_latest = mocker.patch("cats.cfn.resolve.get_latest_ami", return_value="ami-0fedcba9876543210")
    config_input: dict[str, Any] = make_config(
        network_targets={
            "targetOne": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]},
            "targetTwo": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"]]},
        },
    )
    config_input["global_network_settings"]["existing_ec2_ami_id"] = None
    config = CatsConfig.model_validate(config_input)
    account = config.accounts_under_test[ACCOUNT_NAME]

    # act
    resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))

    # assert
    mock_latest.assert_called_once()


def test_vpc_ssm_parameter_resolving_to_list_raises(mock_aws_infra: dict[str, str]) -> None:
    """A StringList of VPC ids would otherwise render raw into the template's VpcId."""
    # arrange
    ssm_path = "/cats/test/vpc-list"
    ssm = boto3.Session(region_name="us-east-1").client("ssm")
    ssm.put_parameter(Name=ssm_path, Value="vpc-00000000000000000,vpc-00000000000000000", Type="StringList")
    config = CatsConfig.model_validate(make_config(network_targets={TARGET_NAME: {"vpc_id": ssm_path, "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}))
    account = config.accounts_under_test[ACCOUNT_NAME]
    expected_message = (
        f"vpc_id '{ssm_path}' in cats.yaml resolved to 'vpc-00000000000000000,vpc-00000000000000000', "
        + "which is not a VPC ID. Provide a VPC ID (vpc-...) or an SSM parameter path holding exactly one."
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    assert str(exc_info.value) == expected_message


# =============================================================================
# Subnet order
# =============================================================================


def test_subnets_keep_the_order_cats_yaml_lists_them(mock_aws_infra: dict[str, str], mocker: MockerFixture) -> None:
    """The first subnet hosts the EC2 listener; describe_subnets makes no promise about response order, so config order wins."""
    # arrange - the config lists B then A; moto's describe_subnets answers A then B
    mocker.patch("cats.cfn.resolve.get_latest_ami", return_value="ami-0fedcba9876543210")
    config_input: dict[str, Any] = make_config(
        network_targets={TARGET_NAME: {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"], mock_aws_infra["SUBNET_A"]]}},
    )
    config_input["global_network_settings"]["existing_ec2_ami_id"] = None
    config = CatsConfig.model_validate(config_input)
    account = config.accounts_under_test[ACCOUNT_NAME]
    session = boto3.Session(region_name="us-east-1")
    described_order = [
        s["SubnetId"] for s in session.client("ec2").describe_subnets(SubnetIds=[mock_aws_infra["SUBNET_B"], mock_aws_infra["SUBNET_A"]])["Subnets"]
    ]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, session)

    # assert - the resolver did not inherit the API's order
    resolved_order = [subnet.subnet_id for subnet in resolved.network_targets[TARGET_NAME].subnets]
    assert (described_order, resolved_order) == (
        [mock_aws_infra["SUBNET_A"], mock_aws_infra["SUBNET_B"]],
        [mock_aws_infra["SUBNET_B"], mock_aws_infra["SUBNET_A"]],
    )
