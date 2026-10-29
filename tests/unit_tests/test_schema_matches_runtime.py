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

"""The published JSON schema agrees with the runtime: it rejects what `CatsConfig` rejects and accepts what it accepts.

The schema is the editor's view of cats.yaml (`# yaml-language-server: $schema=./.schema.json`). A rule the runtime
enforces but the schema does not is a blind spot: the editor shows a clean file that `cats validate` refuses. The
reverse, a schema stricter than the runtime, is worse: a deployable file shown as broken.

Three rules cannot be expressed in JSON Schema and are the runtime's alone: the length of a generated IAM role name
(prefix + keyname + role name), two role names under one account that differ only by case or underscores, and a
``test_selection.excluded`` marker that is neither ``standard_name``, ``cats_requires`` nor a declared custom marker (the
accepted set depends on another section's value).
"""

from collections.abc import Mapping
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from cats.config import CatsConfig
from tests.utils import make_config


def _account(**account: object) -> dict[str, Any]:
    return {"accounts_under_test": {"prod": {"account_id": "111111111111", **account}}}


def _target(**target: object) -> dict[str, Any]:
    return _account(account_network_under_test={"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"], **target}})


def _role(name: str = "readonly", **role: object) -> dict[str, Any]:
    return {"global_account_settings": {"additional_roles": {name: {"managed_policies": ["ReadOnlyAccess"], **role}}}}


def _targets(**targets: Mapping[str, object]) -> dict[str, Any]:
    return _account(account_network_under_test=targets)


REJECTED_BY_THE_RUNTIME: dict[str, dict[str, Any]] = {
    "keyname with a hyphen": {"accounts_under_test": {"shared-services": {"account_id": "111111111111"}}},
    "keyname over 25 characters": {"accounts_under_test": {"a" * 26: {"account_id": "111111111111"}}},
    "target name with an underscore": _targets(private_subnet={"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"]}),
    "target name that is a reserved attribute": _targets(session={"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"]}),
    "target name that is a Python keyword": _targets(**{"class": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"]}}),
    "target with neither subnet_id nor subnet_ids": _targets(private={"vpc_id": "vpc-123"}),
    "target with both subnet_id and subnet_ids": _target(subnet_id="subnet-bbb"),
    "vpc_id that is neither an id nor an SSM path": _targets(private={"vpc_id": "my-vpc", "subnet_ids": ["subnet-aaa"]}),
    "duplicate subnet_ids": _targets(private={"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa", "subnet-aaa"]}),
    "tags as a bare string": _account(tags="production"),
    "unknown partition": {"cats_deployment_settings": {"partition": "bogus"}},
    "explicit partition disagreeing with the Region": {"cats_deployment_settings": {"aws_region": "us-east-1", "partition": "aws-cn"}},
    "stack prefix starting with an SSM-reserved word": {"cats_deployment_settings": {"cfn_stack_prefix": "Aws-x-"}},
    "deploy_role_name as an ARN": {"cats_deployment_settings": {"deploy_role_name": "arn:aws:iam::111111111111:role/x"}},
    "role name with a hyphen": _role("read-only"),
    "role name that is reserved": _role("default"),
    "role name with a leading underscore": _role("_x"),
    "role name that is a Python keyword": _role("class"),
    "role that grants nothing": _role(managed_policies=[]),
    "role with a duplicate managed policy": _role(managed_policies=["ReadOnlyAccess", "ReadOnlyAccess"]),
    "managed policy with a newline": {"global_account_settings": {"default_test_role_managed_policies": ["Read\nOnly"]}},
    "managed policy as a malformed ARN": {"global_account_settings": {"default_test_role_managed_policies": ["arn:aws:iam::evil"]}},
    "duplicate managed policies": {"global_account_settings": {"default_test_role_managed_policies": ["ReadOnlyAccess", "ReadOnlyAccess"]}},
    "inline policy without a Statement": {"global_account_settings": {"default_test_role_inline_policies": {"p": {"Version": "2012-10-17"}}}},
    "inline policy name with a space": {"global_account_settings": {"default_test_role_inline_policies": {"bad name": {"Statement": []}}}},
    "duplicate listen ports": {"global_network_settings": {"tcp_listen_ports": [80, 80]}},
    "duplicate interface endpoints": {"global_network_settings": {"interface_endpoints": ["ssm", "ssm"]}},
    "interface endpoint security group that is neither an id nor an SSM path": {
        "global_network_settings": {"interface_endpoint_security_group_ids": ["my-sg"]}
    },
    "duplicate interface endpoint security groups": {"global_network_settings": {"interface_endpoint_security_group_ids": ["sg-1", "sg-1"]}},
    "timeout above the SSM maximum": {"global_network_settings": {"network_call_timeout_seconds": 2_592_001}},
    "custom marker that is a pytest marker": {"reporting": {"custom_markers": ["skip"]}},
    "custom marker that is a @cats keyword": {"reporting": {"custom_markers": ["standard_name"]}},
    "custom marker with the cats_ prefix": {"reporting": {"custom_markers": ["cats_team"]}},
    "custom marker that is not an identifier": {"reporting": {"custom_markers": ["team name"]}},
    "duplicate custom markers": {"reporting": {"custom_markers": ["team", "team"]}},
    "duplicate report formats": {"reporting": {"report_formats": ["cats_json", "cats_json"]}},
    "no report formats": {"reporting": {"report_formats": []}},
    "exclusion without a reason": {"test_selection": {"excluded": [{"marker": "standard_name", "values": ["X"]}]}},
    "exclusion with an empty reason": {"test_selection": {"excluded": [{"marker": "standard_name", "values": ["X"], "reason": ""}]}},
    "exclusion with empty values": {"test_selection": {"excluded": [{"marker": "standard_name", "values": [], "reason": "r"}]}},
    "exclusion with an integer value": {"test_selection": {"excluded": [{"marker": "standard_name", "values": [1], "reason": "r"}]}},
    "exclusion with a repeated value": {"test_selection": {"excluded": [{"marker": "standard_name", "values": ["X", "X"], "reason": "r"}]}},
    "empty provided requirement": {"test_selection": {"provides": [""]}},
    "duplicate provided requirements": {"test_selection": {"provides": ["a", "a"]}},
    "unknown key": {"cats_deployment_settings": {"region": "us-east-1"}},
}

ACCEPTED_BY_THE_RUNTIME: dict[str, dict[str, Any]] = {
    "empty mapping": {},
    "the test suite's standard config": make_config(),
    "lambda handler, endpoints, roles and an inline policy": make_config(
        enabled_network_handlers=["ec2", "lambda"],
        interface_endpoints=["ssm", "ssmmessages", "ecr.api"],
        gateway_endpoints=["S3", "DYNAMODB"],
        interface_endpoint_security_group_ids=["sg-00000000000000000"],
        additional_roles={
            "readonly": {"managed_policies": ["ReadOnlyAccess", "arn:aws:iam::aws:policy/SecurityAudit"]},
            "s3reader": {"inline_policies": {"allow-s3": {"Version": "2012-10-17", "Statement": []}}},
        },
        account_settings={"default_test_role_inline_policies": {"allow-s3": {"Version": "2012-10-17", "Statement": []}}},
    ),
    "EUSC Region with the partition derived": {"cats_deployment_settings": {"aws_region": "eusc-de-east-1"}},
    "China Region with the partition written": {"cats_deployment_settings": {"aws_region": "cn-north-1", "partition": "aws-cn"}},
    "a Region no partition claims, under an explicit partition": {"cats_deployment_settings": {"aws_region": "xx-north-1", "partition": "aws"}},
    "single-subnet spelling": _account(account_network_under_test={"private": {"vpc_id": "vpc-123", "subnet_id": "subnet-aaa"}}),
    "five targets (the template's size, not a count, is the bound)": _targets(
        **{f"t{i}": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"]} for i in range(5)}
    ),
    "SSM parameter names for every id": _account(
        network_settings={"interface_endpoint_security_group_ids": ["/cats/endpoint-sg"]},
        account_network_under_test={
            "private": {"vpc_id": "/cats/vpc", "subnet_ids": ["/cats/subnet-a"], "existing_security_group_ids": ["/cats/sg"]}
        },
    ),
    "tags in list form": _account(tags=["flag", {"env": "prod"}]),
    "Security Hub reporting with a Region": {
        "cats_deployment_settings": {"aws_region": "us-east-1"},
        "reporting": {"report_formats": ["cats_json", "securityhub"], "securityhub": {"aws_region": "eu-west-1", "failed_severity": "CRITICAL"}},
    },
    "custom markers": {"reporting": {"custom_markers": ["team", "priority", "_internal", "requires"]}},
    "test selection with string and boolean exclusion values": {
        "reporting": {"custom_markers": ["mutating"]},
        "test_selection": {
            "provides": ["container-deployment"],
            "excluded": [
                {"marker": "standard_name", "values": ["ACME-G-SCP-34"], "reason": "RDS not in use"},
                {"marker": "mutating", "values": [True], "reason": "Change freeze"},
                {"marker": "cats_requires", "values": ["uc-plus"], "reason": "not licensed"},
            ],
        },
    },
    "timeout at the SSM maximum": {"global_network_settings": {"network_call_timeout_seconds": 2_592_000}},
    "an instance profile and a literal AMI": {
        "global_network_settings": {"existing_ec2_instance_profile_name": "my-profile", "existing_ec2_ami_id": "ami-0123456789abcdef0"}
    },
}


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    return Draft202012Validator(CatsConfig.model_json_schema())


def test_schema_is_a_valid_2020_12_document() -> None:
    # act / assert: check_schema raises SchemaError on an invalid schema
    Draft202012Validator.check_schema(CatsConfig.model_json_schema())


@pytest.mark.parametrize("config", REJECTED_BY_THE_RUNTIME.values(), ids=REJECTED_BY_THE_RUNTIME.keys())
def test_schema_rejects_what_the_runtime_rejects(schema_validator: Draft202012Validator, config: dict[str, Any]) -> None:
    # arrange: the premise, that the runtime refuses this file
    with pytest.raises(ValidationError):
        CatsConfig.model_validate(config)

    # act
    result = schema_validator.is_valid(config)

    # assert
    assert result is False


@pytest.mark.parametrize("config", ACCEPTED_BY_THE_RUNTIME.values(), ids=ACCEPTED_BY_THE_RUNTIME.keys())
def test_schema_accepts_what_the_runtime_accepts(schema_validator: Draft202012Validator, config: dict[str, Any]) -> None:
    # arrange: the premise, that the runtime loads this file
    CatsConfig.model_validate(config)

    # act
    errors = [error.message for error in schema_validator.iter_errors(config)]

    # assert
    assert errors == []
