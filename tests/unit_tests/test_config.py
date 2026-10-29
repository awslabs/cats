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

"""Tests for cats.config module."""

import ast
import inspect
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer import BadParameter

import cats.config
from cats.config import (
    RESERVED_NETWORK_TARGET_NAMES,
    RESERVED_ROLE_ATTRIBUTE_NAMES,
    CatsAdditionalTestRole,
    CatsConfig,
    CatsNetworkUnderTest,
    EnabledNetworkHandler,
    NetworkSettings,
    click_is_valid_aws_account_id,
    override_config_path,
)
from cats.resources.account_under_test import AccountUnderTest
from cats.resources.session_interact import SessionHandler
from tests.utils import write_config


def test_defaults_when_no_config() -> None:
    """Settings returns sensible defaults when no cats.yaml exists."""
    # arrange
    cats.config.reset_settings()

    # assert
    expected_accounts: dict[str, object] = {}
    assert cats.config.Settings().cats_deployment_settings.parent_account_id is None
    assert cats.config.Settings().accounts_under_test == expected_accounts


def test_config_loads_from_yaml_file(cats_yaml: Path) -> None:
    """CatsConfig reads values from cats.yaml when CATS_CONFIG env var is set."""
    # arrange
    config_input = {
        "cats_deployment_settings": {
            "deploy_role_name": "TestRole",
            "parent_account_id": "111122223333",
        },
    }
    write_config(cats_yaml, config_input)

    # act
    config = CatsConfig()

    # assert
    expected_account_id = "111122223333"
    expected_role_name = "TestRole"
    assert config.cats_deployment_settings.parent_account_id == expected_account_id
    assert config.cats_deployment_settings.deploy_role_name == expected_role_name


def test_aws_region_env_var_respected(
    cats_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AWS_REGION env var sets aws_region without nested prefix."""
    # arrange
    write_config(cats_yaml, {})
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")

    # act
    config = CatsConfig()

    # assert
    expected_region = "ap-southeast-2"
    assert config.cats_deployment_settings.aws_region == expected_region


def test_yaml_aws_region_takes_precedence_over_env(
    cats_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YAML aws_region value takes precedence over AWS_REGION env var."""
    # arrange
    write_config(cats_yaml, {"cats_deployment_settings": {"aws_region": "eu-west-1"}})
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")

    # act
    config = CatsConfig()

    # assert
    expected_region = "eu-west-1"
    assert config.cats_deployment_settings.aws_region == expected_region


def test_network_target_name_with_underscore_rejected() -> None:
    """Network target names must be alphanumeric (no underscores) for CFN compatibility."""
    # arrange
    config_input = {
        "accounts_under_test": {
            "myaccount": {
                "account_id": "123456789012",
                "account_network_under_test": {
                    "private_subnet": {
                        "vpc_id": "vpc-123",
                        "subnet_ids": ["subnet-123"],
                    }
                },
            }
        }
    }

    # act & assert
    with pytest.raises(ValidationError, match=r"String should match pattern '\^\[A-Za-z\]\[A-Za-z0-9\]\*\$'"):
        CatsConfig.model_validate(config_input)


def test_tags_list_format_normalized_to_dict() -> None:
    """Tags in list format are normalized to dict."""
    # arrange
    config_input = {
        "accounts_under_test": {
            "myaccount": {
                "account_id": "123456789012",
                "tags": ["infrastructure", {"routing": "private"}],
                "account_network_under_test": {
                    "subnet": {
                        "vpc_id": "vpc-123",
                        "subnet_ids": ["subnet-123"],
                        "tags": ["isolated", {"env": "prod"}],
                    }
                },
            }
        }
    }

    # act
    config = CatsConfig.model_validate(config_input)

    # assert
    expected_account_tags = {"infrastructure": None, "routing": "private"}
    expected_network_tags = {"isolated": None, "env": "prod"}
    assert config.accounts_under_test["myaccount"].tags == expected_account_tags
    assert config.accounts_under_test["myaccount"].account_network_under_test["subnet"].tags == expected_network_tags


def test_tags_dict_format_still_works() -> None:
    """Tags in dict format continue to work."""
    # arrange
    config_input = {
        "accounts_under_test": {
            "myaccount": {
                "account_id": "123456789012",
                "tags": {"workload": None, "env": "prod"},
            }
        }
    }

    # act
    config = CatsConfig.model_validate(config_input)

    # assert
    expected_tags = {"workload": None, "env": "prod"}
    assert config.accounts_under_test["myaccount"].tags == expected_tags


def test_subnet_id_singular_normalizes_to_list() -> None:
    """subnet_id convenience field normalizes to subnet_ids list."""
    # arrange
    config_input = {
        "accounts_under_test": {
            "myaccount": {
                "account_id": "123456789012",
                "account_network_under_test": {
                    "subnet": {
                        "vpc_id": "vpc-123",
                        "subnet_id": "subnet-456",
                    }
                },
            }
        }
    }

    # act
    config = CatsConfig.model_validate(config_input)

    # assert
    expected_subnet_ids = ["subnet-456"]
    assert config.accounts_under_test["myaccount"].account_network_under_test["subnet"].subnet_ids == expected_subnet_ids


def test_subnet_id_and_subnet_ids_both_set_raises() -> None:
    """Setting both subnet_id and subnet_ids raises validation error."""
    # arrange
    config_input = {
        "accounts_under_test": {
            "myaccount": {
                "account_id": "123456789012",
                "account_network_under_test": {
                    "subnet": {
                        "vpc_id": "vpc-123",
                        "subnet_id": "subnet-aaa",
                        "subnet_ids": ["subnet-1", "subnet-2"],
                    }
                },
            }
        }
    }

    # act & assert
    with pytest.raises(ValidationError, match="Use subnet_id or subnet_ids, not both"):
        CatsConfig.model_validate(config_input)


def test_existing_ec2_ami_id_accepts_literal_ami_id() -> None:
    """existing_ec2_ami_id accepts a literal AMI ID."""
    # arrange
    config_input = {"global_network_settings": {"existing_ec2_ami_id": "ami-0123456789abcdef0"}}

    # act
    config = CatsConfig.model_validate(config_input)

    # assert
    expected_ami_id = "ami-0123456789abcdef0"
    assert config.global_network_settings.existing_ec2_ami_id == expected_ami_id


def test_existing_ec2_ami_id_accepts_ssm_parameter_path() -> None:
    """existing_ec2_ami_id accepts an SSM parameter path."""
    # arrange
    ssm_path = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
    config_input = {"global_network_settings": {"existing_ec2_ami_id": ssm_path}}

    # act
    config = CatsConfig.model_validate(config_input)

    # assert
    assert config.global_network_settings.existing_ec2_ami_id == ssm_path


def test_existing_ec2_ami_id_rejects_invalid_value() -> None:
    """existing_ec2_ami_id rejects values that are neither an AMI ID nor an SSM path."""
    # arrange
    config_input = {"global_network_settings": {"existing_ec2_ami_id": "latest-al2023"}}

    # act & assert
    with pytest.raises(ValidationError, match=r"String should match pattern '\^\(ami-\[0-9a-f\]\{8,17\}\|/\[a-zA-Z0-9_\.\\-/\]\+\)\$'"):
        CatsConfig.model_validate(config_input)


# =============================================================================
# Environment override removal
# =============================================================================


def test_env_var_cannot_disable_reporting(cats_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No env settings source: REPORTING in the environment must not switch evidence off."""
    # arrange
    write_config(cats_yaml, {})
    monkeypatch.setenv("REPORTING", '{"enabled": false}')

    # act
    config = CatsConfig()

    # assert
    assert config.reporting.enabled is True


def test_garbage_env_var_matching_a_field_name_is_ignored(cats_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With an env settings source this raised SettingsError before any yaml was read."""
    # arrange
    write_config(cats_yaml, {})
    monkeypatch.setenv("REPORTING", "garbage")

    # act
    config = CatsConfig()

    # assert
    assert config.reporting.enabled is True


# =============================================================================
# Account keyname pattern
# =============================================================================
# Rejection tests validate dicts directly: writing invalid yaml to the ambient
# config path would make the CATS plugin's own session reporting fail the test.


def test_keyname_with_underscore_is_valid() -> None:
    # act
    config = CatsConfig.model_validate({"accounts_under_test": {"shared_services": {"account_id": "111111111111"}}})

    # assert
    assert list(config.accounts_under_test) == ["shared_services"]


def test_keyname_with_hyphen_is_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match=r"String should match pattern '\^\[A-Za-z\]\[A-Za-z0-9_\]\*\$'"):
        CatsConfig.model_validate({"accounts_under_test": {"shared-services": {"account_id": "111111111111"}}})


# =============================================================================
# Additional role names (the keys of additional_roles)
# =============================================================================


def _roles(roles: dict[str, object]) -> dict[str, object]:
    return {"global_account_settings": {"additional_roles": roles}}


def _readonly_roles(*names: str) -> dict[str, object]:
    return _roles({name: {"managed_policies": ["ReadOnlyAccess"]} for name in names})


def test_reserved_role_name_default_rejected() -> None:
    # arrange
    expected_message = "additional role name 'default' is reserved: it is an existing attribute of AccountUnderTest.role. Reserved names: ['default']"

    # act / assert
    with pytest.raises(ValidationError, match=re.escape(expected_message)):
        CatsConfig.model_validate(_readonly_roles("default"))


def test_role_name_with_leading_underscore_rejected() -> None:
    """SessionHandler keeps everything but `default` underscore-private, so `_x` would collide with its plumbing."""
    # act / assert
    with pytest.raises(ValidationError, match=re.escape("additional_roles._x.[key]\n  String should match pattern '^[A-Za-z][A-Za-z0-9_]*$'")):
        CatsConfig.model_validate(_readonly_roles("_x"))


def test_role_name_that_is_a_python_keyword_rejected() -> None:
    """A role name becomes `account.role.<name>`; a keyword there is a SyntaxError in every test."""
    # arrange
    expected_message = "additional role name 'pass' is a Python keyword and cannot be an attribute of AccountUnderTest.role"

    # act / assert
    with pytest.raises(ValidationError, match=re.escape(expected_message)):
        CatsConfig.model_validate(_readonly_roles("pass"))


def test_role_name_with_underscore_accepted() -> None:
    # act
    config = CatsConfig.model_validate(_readonly_roles("read_only"))

    # assert
    assert list(config.global_account_settings.additional_roles) == ["read_only"]


def test_role_with_no_policies_rejected() -> None:
    # arrange
    expected_message = (
        "additional_roles.readonly\n  Value error, an additional role grants no permissions: set managed_policies, inline_policies, or both"
    )

    # act / assert
    with pytest.raises(ValidationError, match=re.escape(expected_message)):
        CatsConfig.model_validate(_roles({"readonly": {}}))


def test_role_with_only_inline_policies_accepted() -> None:
    # arrange
    policy_document: dict[str, object] = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}],
    }

    # act
    config = CatsConfig.model_validate(_roles({"s3reader": {"inline_policies": {"s3-read": policy_document}}}))

    # assert
    expected = CatsAdditionalTestRole(managed_policies=[], inline_policies={"s3-read": policy_document})
    assert expected == config.global_account_settings.additional_roles["s3reader"]


def test_role_name_key_is_rejected() -> None:
    """role_name is not a key: the deployed role name is derived from the map key and the resource prefix."""
    # act / assert
    with pytest.raises(ValidationError, match=re.escape("global_account_settings.additional_roles.readonly\n  unknown key 'role_name'")):
        CatsConfig.model_validate(_roles({"readonly": {"managed_policies": ["ReadOnlyAccess"], "role_name": "SomeName"}}))


def test_additional_roles_list_shape_rejected() -> None:
    """additional_roles is a map keyed by role name."""
    # act / assert
    with pytest.raises(ValidationError, match="additional_roles\n  Input should be a valid dictionary"):
        CatsConfig.model_validate(_roles([{"managed_policies": ["ReadOnlyAccess"]}]))  # type: ignore[arg-type]


# =============================================================================
# Reserved network target names
# =============================================================================


def test_reserved_network_target_name_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="network target name 'session' is reserved"):
        CatsConfig.model_validate(
            {
                "accounts_under_test": {
                    "myaccount": {
                        "account_id": "111111111111",
                        "account_network_under_test": {"session": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]}},
                    }
                }
            }
        )


def _instance_attributes(cls: type) -> set[str]:
    """Every ``self.<attr> = ...`` in ``cls``, read from source: the instances need AWS to construct."""
    tree = ast.parse(inspect.getsource(cls))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name) and node.value.id == "self"
    }


def test_reserved_names_match_account_under_test_attributes() -> None:
    """RESERVED_NETWORK_TARGET_NAMES is exactly the set of single-word AccountUnderTest attributes a target could shadow."""
    # arrange
    expected = {"name", "partition", "session", "role", "tags"}

    # act
    single_word = {attr for attr in set(dir(AccountUnderTest)) | _instance_attributes(AccountUnderTest) if "_" not in attr}

    # assert
    assert (single_word, set(RESERVED_NETWORK_TARGET_NAMES)) == (expected, expected), (
        "Every AccountUnderTest attribute outside RESERVED_NETWORK_TARGET_NAMES must contain '_', "
        + "which CFN_LOGICAL_ID_PATTERN forbids in a network target name"
    )


def test_reserved_role_names_match_session_handler_attributes() -> None:
    """RESERVED_ROLE_ATTRIBUTE_NAMES is exactly the public surface of SessionHandler a role name could shadow."""
    # arrange
    expected = {"default"}

    # act
    public = {attr for attr in set(dir(SessionHandler)) | _instance_attributes(SessionHandler) if attr.startswith("_") is False}

    # assert
    assert (public, set(RESERVED_ROLE_ATTRIBUTE_NAMES)) == (expected, expected), (
        "SessionHandler has one public name, 'default'; every other attribute is underscore-private, so a role name may not start with '_'"
    )


def test_network_target_name_that_is_a_python_keyword_rejected() -> None:
    """'class' passes the logical-id pattern but `account.class` is a SyntaxError in every test."""
    # act / assert
    with pytest.raises(ValidationError, match="network target name 'class' is a Python keyword"):
        CatsConfig.model_validate(
            {
                "accounts_under_test": {
                    "myaccount": {
                        "account_id": "111111111111",
                        "account_network_under_test": {"class": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]}},
                    }
                }
            }
        )


# =============================================================================
# regions removal, scalar validation
# =============================================================================


def test_regions_key_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match=re.escape("accounts_under_test.myaccount\n  unknown key 'regions'")):
        CatsConfig.model_validate({"accounts_under_test": {"myaccount": {"account_id": "111111111111", "regions": ["us-east-1"]}}})


def test_unknown_partition_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="'bogus' is not a known AWS partition"):
        CatsConfig.model_validate({"cats_deployment_settings": {"partition": "bogus"}})


def test_china_partition_accepted() -> None:
    # act
    config = CatsConfig.model_validate({"cats_deployment_settings": {"partition": "aws-cn"}})

    # assert
    assert config.cats_deployment_settings.partition == "aws-cn"


def test_instance_type_yaml_injection_rejected() -> None:
    """A newline in ec2_instance_type would otherwise land raw inside Ec2Listener.Properties."""
    # act / assert
    with pytest.raises(ValidationError, match="ec2_instance_type"):
        CatsConfig.model_validate({"global_network_settings": {"ec2_instance_type": "t3.micro\n      KeyName: attacker"}})


def test_metal_instance_type_accepted() -> None:
    # act
    config = CatsConfig.model_validate({"global_network_settings": {"ec2_instance_type": "u-6tb1.metal"}})

    # assert
    assert config.global_network_settings.ec2_instance_type == "u-6tb1.metal"


def test_metal_24xl_instance_type_accepted() -> None:
    """Bare-metal sizes carry a dash after the dot (metal-24xl) and are valid instance types."""
    # act
    config = CatsConfig.model_validate({"global_network_settings": {"ec2_instance_type": "c7i.metal-24xl"}})

    # assert
    assert config.global_network_settings.ec2_instance_type == "c7i.metal-24xl"


def test_tags_bare_string_is_rejected() -> None:
    """`tags: production` is a bare string, not a list: rejected rather than read as ten one-letter tags."""
    # arrange
    expected_message = "Value error, tags must be a mapping or a list of tag names and mappings, got str 'production'"

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate({"accounts_under_test": {"myaccount": {"account_id": "123456789012", "tags": "production"}}})

    # assert
    assert str(exc_info.value.errors()[0]["msg"]) == expected_message


def test_tags_list_item_of_another_type_is_a_validation_error() -> None:
    """`tags: [1]` is a validation error, not an AttributeError from the tag normalizer."""
    # arrange
    expected_message = "Value error, tags list items must be a tag name or a {key: value} mapping, got int 1"

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate({"accounts_under_test": {"myaccount": {"account_id": "123456789012", "tags": [1]}}})

    # assert
    assert str(exc_info.value.errors()[0]["msg"]) == expected_message


def test_stack_prefix_with_colon_rejected() -> None:
    """'CATS: x-' renders unparseable YAML and is an invalid stack name."""
    # act / assert
    with pytest.raises(ValidationError, match="cfn_stack_prefix"):
        CatsConfig.model_validate({"cats_deployment_settings": {"cfn_stack_prefix": "CATS: x-"}})


def test_eusc_region_accepted() -> None:
    # act
    config = CatsConfig.model_validate({"cats_deployment_settings": {"aws_region": "eusc-de-east-1"}})

    # assert
    assert config.cats_deployment_settings.aws_region == "eusc-de-east-1"


def test_interface_endpoint_with_underscore_rejected() -> None:
    """'s3_x' would render the invalid CFN logical id InterfaceEndpointS3_x."""
    # act / assert
    with pytest.raises(ValidationError, match="interface_endpoints"):
        CatsConfig.model_validate({"global_network_settings": {"interface_endpoints": ["s3_x"]}})


def test_managed_policy_full_arn_accepted() -> None:
    # arrange
    arn = "arn:aws:iam::123456789012:policy/MyCustomPolicy"

    # act
    config = CatsConfig.model_validate({"global_account_settings": {"default_test_role_managed_policies": [arn]}})

    # assert
    assert config.global_account_settings.default_test_role_managed_policies == [arn]


def test_managed_policy_malformed_arn_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="is not a valid IAM policy ARN"):
        CatsConfig.model_validate({"global_account_settings": {"default_test_role_managed_policies": ["arn:aws:iam::evil"]}})


def test_managed_policy_with_yaml_injection_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="is not a valid IAM managed policy name or ARN"):
        CatsConfig.model_validate({"global_account_settings": {"default_test_role_managed_policies": ["Read\nOnly"]}})


def test_subnet_id_is_input_only_so_a_dumped_target_re_validates() -> None:
    """model_dump emits subnet_ids alone: the loaded model never carries both spellings."""
    # arrange
    target = CatsNetworkUnderTest(vpc_id="vpc-123", subnet_id="subnet-456")

    # act
    dumped = target.model_dump(mode="json", exclude_unset=True)

    # assert
    expected = {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"]}
    assert dumped == expected


def test_empty_enabled_network_handlers_is_rejected() -> None:
    """A declared target exists to be probed, so it must name a handler."""
    # act
    with pytest.raises(ValidationError) as exc_info:
        NetworkSettings(enabled_network_handlers=[])

    # assert
    expected = [("enabled_network_handlers",)]
    assert [error["loc"] for error in exc_info.value.errors()] == expected


def test_global_empty_enabled_network_handlers_is_rejected() -> None:
    """The one-handler minimum holds at the global level too, so an empty list never reaches deploy."""
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate({"global_network_settings": {"enabled_network_handlers": []}})

    # assert
    expected = [("global_network_settings", "enabled_network_handlers")]
    assert [error["loc"] for error in exc_info.value.errors()] == expected


def test_omitted_enabled_network_handlers_defaults_to_ec2() -> None:
    # act
    config = CatsConfig.model_validate({})

    # assert
    assert config.global_network_settings.enabled_network_handlers == [EnabledNetworkHandler.EC2]


def test_top_level_section_names_are_case_sensitive(isolated_yaml: Path) -> None:
    """REPORTING: is an unknown key like every other misspelled key, not an alias of reporting:; the hint names the real key."""
    # arrange
    write_config(isolated_yaml, {"REPORTING": {"enabled": False}})

    # act
    with override_config_path(isolated_yaml), pytest.raises(ValidationError) as exc_info:
        CatsConfig()

    # assert
    expected = [((), "unknown key 'REPORTING', did you mean 'reporting'?")]
    assert [(error["loc"], error["msg"]) for error in exc_info.value.errors()] == expected


# =============================================================================
# Reject at validation what deploy or the first test call would refuse
# =============================================================================


def test_network_call_timeout_above_ssm_maximum_rejected() -> None:
    """The value is passed straight to SSM SendCommand.TimeoutSeconds, whose maximum is 2,592,000."""
    # act / assert
    with pytest.raises(ValidationError, match="network_call_timeout_seconds\n  Input should be less than or equal to 2592000"):
        CatsConfig.model_validate({"global_network_settings": {"network_call_timeout_seconds": 2_592_001}})


def test_network_call_timeout_at_ssm_maximum_accepted() -> None:
    # act
    config = CatsConfig.model_validate({"global_network_settings": {"network_call_timeout_seconds": 2_592_000}})

    # assert
    assert config.global_network_settings.network_call_timeout_seconds == 2_592_000


def test_deploy_role_name_surrounding_whitespace_is_stripped() -> None:
    """Surrounding whitespace is stripped; ' AWSControlTowerExecution ' would otherwise reach sts.assume_role inside a malformed ARN."""
    # act
    config = CatsConfig.model_validate({"cats_deployment_settings": {"deploy_role_name": " AWSControlTowerExecution "}})

    # assert
    assert config.cats_deployment_settings.deploy_role_name == "AWSControlTowerExecution"


def test_deploy_role_name_with_newline_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="deploy_role_name\n  String should match pattern"):
        CatsConfig.model_validate({"cats_deployment_settings": {"deploy_role_name": "x\n  Evil: y"}})


def test_deploy_role_name_as_arn_rejected() -> None:
    """The field is a role name interpolated into arn:{partition}:iam::{account}:role/{name}; an ARN there is doubled."""
    # act / assert
    with pytest.raises(ValidationError, match="deploy_role_name\n  String should match pattern"):
        CatsConfig.model_validate({"cats_deployment_settings": {"deploy_role_name": "arn:aws:iam::123456789012:role/x"}})


def test_empty_deploy_role_name_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="deploy_role_name\n  String should match pattern"):
        CatsConfig.model_validate({"cats_deployment_settings": {"deploy_role_name": ""}})


def test_partition_is_derived_from_the_region_when_unset() -> None:
    """eusc-de-east-1 under an assumed 'aws' would render arn:aws: ARNs and amazonaws.com endpoints; the partition follows the Region."""
    # act
    config = CatsConfig.model_validate({"cats_deployment_settings": {"aws_region": "eusc-de-east-1"}})

    # assert
    assert config.cats_deployment_settings.partition == "aws-eusc"


def test_govcloud_region_derives_the_govcloud_partition() -> None:
    # act
    config = CatsConfig.model_validate({"cats_deployment_settings": {"aws_region": "us-gov-west-1"}})

    # assert
    assert config.cats_deployment_settings.partition == "aws-us-gov"


def test_region_no_partition_claims_keeps_the_aws_default() -> None:
    """botocore treats a Region it has not heard of as aws; so does CATS, rather than refusing a new Region prefix."""
    # act
    config = CatsConfig.model_validate({"cats_deployment_settings": {"aws_region": "xx-north-1"}})

    # assert
    assert config.cats_deployment_settings.partition == "aws"


def test_explicit_partition_matching_the_region_accepted() -> None:
    # act
    config = CatsConfig.model_validate({"cats_deployment_settings": {"aws_region": "cn-north-1", "partition": "aws-cn"}})

    # assert
    assert config.cats_deployment_settings.partition == "aws-cn"


def test_explicit_partition_disagreeing_with_the_region_rejected() -> None:
    # arrange
    expected_message = (
        "Value error, cats_deployment_settings.partition 'aws-cn' does not contain Region 'us-east-1', which belongs to "
        + "partition 'aws'. Remove partition (CATS derives it from aws_region) or correct one of the two."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate({"cats_deployment_settings": {"aws_region": "us-east-1", "partition": "aws-cn"}})

    # assert
    assert str(exc_info.value.errors()[0]["msg"]) == expected_message


def test_securityhub_region_in_another_partition_rejected() -> None:
    """Findings are imported through the test role, which lives in the deploy partition."""
    # arrange
    expected_message = (
        "Value error, reporting.securityhub.aws_region 'cn-north-1' belongs to partition 'aws-cn', not 'aws', the partition of "
        + "cats_deployment_settings.aws_region 'us-east-1'; Security Hub findings are imported through the test role in that partition."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(
            {
                "cats_deployment_settings": {"aws_region": "us-east-1"},
                "reporting": {"report_formats": ["securityhub"], "securityhub": {"aws_region": "cn-north-1"}},
            }
        )

    # assert
    assert str(exc_info.value.errors()[0]["msg"]) == expected_message


def test_vpc_id_uppercase_rejected() -> None:
    """'VPC-...' is neither a literal id nor an SSM name; sent to ssm:GetParameter it would fail with ParameterNotFound and blame SSM."""
    # act / assert
    with pytest.raises(ValidationError, match="vpc_id\n  String should match pattern"):
        CatsNetworkUnderTest.model_validate({"vpc_id": "VPC-0123456789abcdef0", "subnet_ids": ["subnet-aaa"]})


def test_vpc_id_bare_ssm_parameter_name_rejected() -> None:
    """An SSM parameter name must start with '/'; anything else is read as a literal id and fails its pattern."""
    # act / assert
    with pytest.raises(ValidationError, match="vpc_id\n  String should match pattern"):
        CatsNetworkUnderTest.model_validate({"vpc_id": "my-vpc-param", "subnet_ids": ["subnet-aaa"]})


def test_subnet_id_with_underscore_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match=r"subnet_ids\.0\n  String should match pattern"):
        CatsNetworkUnderTest.model_validate({"vpc_id": "vpc-123", "subnet_ids": ["subnet_0123456789abcdef0"]})


def test_existing_security_group_id_uppercase_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match=r"existing_security_group_ids\.0\n  String should match pattern"):
        CatsNetworkUnderTest.model_validate({"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"], "existing_security_group_ids": ["SG-0a1b2c3d"]})


def test_network_ids_accept_literal_ids_and_ssm_paths_alike() -> None:
    # act
    target = CatsNetworkUnderTest.model_validate(
        {"vpc_id": "/cats/vpc", "subnet_ids": ["subnet-aaa", "/cats/subnet-b"], "existing_security_group_ids": ["sg-aaa", "/cats/sg"]}
    )

    # assert
    assert target.subnet_ids == ["subnet-aaa", "/cats/subnet-b"]


def test_non_ascii_role_name_rejected() -> None:
    """str.isidentifier() accepts Unicode letters; IAM RoleName, which the name is rendered into, does not. The pattern is ASCII."""
    # act / assert
    with pytest.raises(ValidationError, match=re.escape("additional_roles.ünïcode.[key]\n  String should match pattern '^[A-Za-z][A-Za-z0-9_]*$'")):
        CatsConfig.model_validate(_readonly_roles("ünïcode"))


def test_stack_prefix_with_ssm_reserved_prefix_rejected() -> None:
    """The probe SSM document is named {prefix}{account}-Probe; SSM refuses names starting aws, amazon or amzn."""
    # arrange
    expected_message = (
        "Value error, cfn_stack_prefix 'aws-cats-' starts with 'aws', which SSM reserves for AWS-owned document names, so the probe "
        + "document 'aws-cats-<account>-Probe' could not be created. Choose a prefix that does not start with aws, amazon or amzn (in any case)."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate({"cats_deployment_settings": {"cfn_stack_prefix": "aws-cats-"}})

    # assert
    assert str(exc_info.value.errors()[0]["msg"]) == expected_message


def test_stack_prefix_ssm_reservation_is_case_insensitive() -> None:
    """Whether SSM compares case-insensitively is not documented; rejecting 'AWSCats-' is the safe side."""
    # act / assert
    with pytest.raises(ValidationError, match="cfn_stack_prefix 'AWSCats-' starts with 'aws'"):
        CatsConfig.model_validate({"cats_deployment_settings": {"cfn_stack_prefix": "AWSCats-"}})


def test_string_values_are_stripped_of_surrounding_whitespace() -> None:
    """Every free-text field is stripped, so a trailing space is never silently kept in a rendered name."""
    # act
    config = CatsConfig.model_validate({"accounts_under_test": {"prod": {"account_id": "111111111111", "account_name": "  Production  "}}})

    # assert
    assert config.accounts_under_test["prod"].account_name == "Production"


ARABIC_INDIC_TWELVE_DIGITS = "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0660\u0661\u0662"


def test_account_id_rejects_non_ascii_digits() -> None:
    """\\d matches every Unicode digit; an AWS account id is twelve ASCII digits."""
    # act / assert
    with pytest.raises(ValidationError, match="account_id"):
        CatsConfig.model_validate({"accounts_under_test": {"prod": {"account_id": ARABIC_INDIC_TWELVE_DIGITS}}})


def test_aws_region_rejects_non_ascii_digits() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="aws_region"):
        CatsConfig.model_validate({"cats_deployment_settings": {"aws_region": "us-east-\u0661"}})


def test_report_formats_rejects_an_empty_list() -> None:
    """No formats would mean a report run with nothing to write; `enabled: false` is the switch for that."""
    # act / assert
    with pytest.raises(ValidationError, match="report_formats"):
        CatsConfig.model_validate({"reporting": {"report_formats": []}})


@pytest.mark.parametrize("account_id", ["123456789012\n", ARABIC_INDIC_TWELVE_DIGITS, "12345678901", "1234567890123"])
def test_click_account_id_validator_rejects_lookalikes(account_id: str) -> None:
    """re.match with $ accepted a trailing newline and \\d accepted non-ASCII digits."""
    # act / assert
    with pytest.raises(BadParameter, match="12 numbers"):
        click_is_valid_aws_account_id(account_id)


def test_click_account_id_validator_accepts_twelve_ascii_digits() -> None:
    # act
    result = click_is_valid_aws_account_id("123456789012")

    # assert
    assert result == "123456789012"
