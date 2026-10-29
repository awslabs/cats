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

"""Every cats.yaml list whose elements each render a resource, an output or a writer must be unique.

Rendered, a duplicate is a duplicate CloudFormation key, a repeated ARN or group id IAM and EC2
reject, or a writer run twice; validation refuses it first.
"""

import pytest
from pydantic import ValidationError

from cats.config import CatsConfig


def _network_target(**network_settings: object) -> dict[str, object]:
    return {
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
                "account_network_under_test": {
                    "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"], "network_settings": network_settings},
                },
            }
        }
    }


def test_duplicate_interface_endpoints_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="interface_endpoints\n  List must be unique"):
        CatsConfig.model_validate({"global_network_settings": {"interface_endpoints": ["ssm", "ssm"]}})


def test_interface_endpoints_that_render_one_logical_id_rejected() -> None:
    """The template strips '.' and '-' to build the logical id, so 'ssmmessages' and 'ssm-messages' collide."""
    # arrange
    expected_message = (
        "Value error, interface_endpoints 'ssmmessages' and 'ssm-messages' both render the CloudFormation "
        + "logical id 'InterfaceEndpointSsmmessagesVpc<n>'; keep one of them."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate({"global_network_settings": {"interface_endpoints": ["ssmmessages", "ssm-messages"]}})

    # assert
    assert str(exc_info.value.errors()[0]["msg"]) == expected_message


def test_duplicate_gateway_endpoints_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="gateway_endpoints\n  List must be unique"):
        CatsConfig.model_validate({"global_network_settings": {"gateway_endpoints": ["S3", "S3"]}})


def test_duplicate_enabled_network_handlers_rejected() -> None:
    """[ec2, ec2] would render 'EnabledHandlers...: ec2,ec2' into the outputs contract."""
    # act / assert
    with pytest.raises(ValidationError, match="enabled_network_handlers\n  List must be unique"):
        CatsConfig.model_validate(_network_target(enabled_network_handlers=["ec2", "ec2"]))


def test_duplicate_report_formats_rejected() -> None:
    """[cats_json, cats_json] would run the writer twice."""
    # act / assert
    with pytest.raises(ValidationError, match="report_formats\n  List must be unique"):
        CatsConfig.model_validate({"reporting": {"report_formats": ["cats_json", "cats_json"]}})


def test_duplicate_additional_role_managed_policies_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="managed_policies\n  List must be unique"):
        CatsConfig.model_validate(
            {"global_account_settings": {"additional_roles": {"readonly": {"managed_policies": ["ReadOnlyAccess", "ReadOnlyAccess"]}}}}
        )


def test_duplicate_default_test_role_managed_policies_rejected() -> None:
    # act / assert
    with pytest.raises(ValidationError, match="default_test_role_managed_policies\n  List must be unique"):
        CatsConfig.model_validate({"global_account_settings": {"default_test_role_managed_policies": ["ReadOnlyAccess", "ReadOnlyAccess"]}})


def test_duplicate_existing_security_group_ids_rejected() -> None:
    # arrange
    target = {"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa"], "existing_security_group_ids": ["sg-aaa", "sg-aaa"]}

    # act / assert
    with pytest.raises(ValidationError, match="existing_security_group_ids\n  List must be unique"):
        CatsConfig.model_validate(
            {"accounts_under_test": {"prod": {"account_id": "111111111111", "account_network_under_test": {"private": target}}}}
        )


def test_duplicate_subnet_ids_rejected() -> None:
    # arrange
    target = {"vpc_id": "vpc-123", "subnet_ids": ["subnet-aaa", "subnet-aaa"]}

    # act / assert
    with pytest.raises(ValidationError, match="subnet_ids\n  List must be unique"):
        CatsConfig.model_validate(
            {"accounts_under_test": {"prod": {"account_id": "111111111111", "account_network_under_test": {"private": target}}}}
        )
