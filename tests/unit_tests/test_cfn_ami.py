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

"""Tests for cats/cfn/ami.py - the instance type decides the architecture, the architecture decides the AMI."""

import logging
from collections.abc import Generator
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from cats.cfn.ami import get_instance_architecture, get_latest_ami
from cats.exceptions import CatsConfigError, CatsInfrastructureError

REGION = "us-east-1"


@pytest.fixture
def aws_session() -> Generator[boto3.Session, None, None]:
    with mock_aws():
        yield boto3.Session(region_name=REGION)


# =============================================================================
# get_instance_architecture
# =============================================================================


@pytest.mark.parametrize(
    ("instance_type", "expected"),
    [
        pytest.param("t3.micro", "x86_64", id="t3_is_x86"),
        pytest.param("t4g.micro", "arm64", id="t4g_is_graviton"),
        pytest.param("m7g.large", "arm64", id="m7g_is_graviton"),
        pytest.param("c6gn.large", "arm64", id="c6gn_is_graviton"),
        pytest.param("a1.medium", "arm64", id="a1_is_graviton"),
    ],
)
def test_architecture_follows_the_instance_type(aws_session: boto3.Session, instance_type: str, expected: str) -> None:
    # act
    result = get_instance_architecture(aws_session, REGION, instance_type)

    # assert
    assert result == expected


def test_unknown_instance_type_is_a_config_error(aws_session: boto3.Session) -> None:
    # arrange
    expected_message = "ec2_instance_type 't9z.nano' is not an instance type EC2 offers in region 'us-east-1'."

    # act & assert
    with pytest.raises(CatsConfigError) as exc_info:
        get_instance_architecture(aws_session, REGION, "t9z.nano")
    assert str(exc_info.value) == expected_message


def test_other_describe_error_is_an_infrastructure_error() -> None:
    # arrange
    mock_session = MagicMock()
    mock_session.client.return_value.describe_instance_types.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "no"}}, "DescribeInstanceTypes"
    )
    expected_message = (
        "Failed to describe instance type 't3.micro' in region 'us-east-1'. "
        + "Check the deploy role has ec2:DescribeInstanceTypes permission. AWS error: UnauthorizedOperation"
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        get_instance_architecture(mock_session, REGION, "t3.micro")
    assert str(exc_info.value) == expected_message


def test_architecture_amazon_linux_does_not_publish_is_a_config_error() -> None:
    # arrange - a Mac instance type reports x86_64_mac, which AL2023 is not published for
    mock_session = MagicMock()
    mock_session.client.return_value.describe_instance_types.return_value = {
        "InstanceTypes": [{"ProcessorInfo": {"SupportedArchitectures": ["x86_64_mac"]}}]
    }
    expected_message = "ec2_instance_type 'mac1.metal' runs none of the architectures Amazon Linux 2023 is published for (arm64, x86_64)."

    # act & assert
    with pytest.raises(CatsConfigError) as exc_info:
        get_instance_architecture(mock_session, REGION, "mac1.metal")
    assert str(exc_info.value) == expected_message


# =============================================================================
# get_latest_ami
# =============================================================================


@pytest.mark.parametrize("architecture", ["x86_64", "arm64"])
def test_latest_ami_matches_the_requested_architecture(aws_session: boto3.Session, architecture: str) -> None:
    # arrange - moto provides default AL2023 AMIs for both architectures
    ec2 = aws_session.client("ec2")

    # act
    ami_id = get_latest_ami(aws_session, REGION, architecture)

    # assert
    image = ec2.describe_images(ImageIds=[ami_id])["Images"][0]
    assert (image["Architecture"], image["Name"].startswith("al2023-ami-2023.")) == (architecture, True)


def test_latest_ami_is_the_newest_by_creation_date() -> None:
    # arrange
    mock_session = MagicMock()
    mock_session.client.return_value.describe_images.return_value = {
        "Images": [
            {"ImageId": "ami-older", "Name": "al2023-ami-2023.6.20250101.0-kernel-6.1-x86_64", "CreationDate": "2025-01-01T00:00:00.000Z"},
            {"ImageId": "ami-newer", "Name": "al2023-ami-2023.7.20250301.0-kernel-6.1-x86_64", "CreationDate": "2025-03-01T00:00:00.000Z"},
        ]
    }

    # act
    ami_id = get_latest_ami(mock_session, REGION, "x86_64")

    # assert
    assert ami_id == "ami-newer"


def test_latest_ami_logs_the_chosen_image(caplog: pytest.LogCaptureFixture) -> None:
    # arrange - the AMI is unpinned; a redeploy that picks a newer one replaces the instance, so the choice is visible
    mock_session = MagicMock()
    mock_session.client.return_value.describe_images.return_value = {
        "Images": [{"ImageId": "ami-0abc", "Name": "al2023-ami-2023.7.20250301.0-kernel-6.1-arm64", "CreationDate": "2025-03-01T00:00:00.000Z"}]
    }

    # act
    with caplog.at_level(logging.INFO, logger="cats.cfn.ami"):
        get_latest_ami(mock_session, REGION, "arm64")

    # assert
    assert caplog.messages == ["Using AMI ami-0abc (al2023-ami-2023.7.20250301.0-kernel-6.1-arm64) for arm64 listeners in us-east-1"]


def test_latest_ami_raises_when_no_ami_found() -> None:
    # arrange
    mock_session = MagicMock()
    mock_session.client.return_value.describe_images.return_value = {"Images": []}
    expected_message = "No arm64 Amazon Linux 2023 AMI found in region 'us-east-1'"

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        get_latest_ami(mock_session, REGION, "arm64")
    assert str(exc_info.value) == expected_message


def test_latest_ami_describe_error_is_an_infrastructure_error() -> None:
    # arrange
    mock_session = MagicMock()
    mock_session.client.return_value.describe_images.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "no"}}, "DescribeImages"
    )
    expected_message = (
        "Failed to look up AMI in region 'us-east-1'. CATS needs an Amazon Linux 2023 AMI to deploy EC2 test infrastructure. "
        + "Check the deploy role has ec2:DescribeImages permission. AWS error: UnauthorizedOperation"
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        get_latest_ami(mock_session, REGION, "x86_64")
    assert str(exc_info.value) == expected_message
