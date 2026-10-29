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

"""AMI lookup utilities."""

import logging

import boto3
from botocore.exceptions import ClientError

from cats.exceptions import CatsConfigError, CatsInfrastructureError

logger = logging.getLogger(__name__)

# The architectures Amazon Linux 2023 publishes, as EC2 names them.
AL2023_ARCHITECTURES = frozenset({"x86_64", "arm64"})


def get_instance_architecture(session: boto3.Session, region: str, instance_type: str) -> str:
    """The processor architecture an instance type runs, so the AMI lookup matches it (t4g is arm64, t3 is x86_64)."""
    ec2 = session.client("ec2", region_name=region)
    try:
        # The stubs type InstanceTypes as a closed Literal of every known type; a user-supplied string is not one.
        instance_types = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"]  # type: ignore[list-item]
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidInstanceType":
            raise CatsConfigError(f"ec2_instance_type '{instance_type}' is not an instance type EC2 offers in region '{region}'.") from e
        raise CatsInfrastructureError(
            f"Failed to describe instance type '{instance_type}' in region '{region}'. "
            + "Check the deploy role has ec2:DescribeInstanceTypes permission. "
            + f"AWS error: {e.response['Error']['Code']}"
        ) from e
    architectures = set(instance_types[0]["ProcessorInfo"]["SupportedArchitectures"]) & AL2023_ARCHITECTURES
    if len(architectures) == 0:
        published = ", ".join(sorted(AL2023_ARCHITECTURES))
        raise CatsConfigError(f"ec2_instance_type '{instance_type}' runs none of the architectures Amazon Linux 2023 is published for ({published}).")
    return sorted(architectures)[0]


def get_latest_ami(session: boto3.Session, region: str, architecture: str) -> str:
    """Get the latest Amazon Linux 2023 AMI ID for the architecture, and log which one was chosen."""
    ec2 = session.client("ec2", region_name=region)
    try:
        response = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": [f"al2023-ami-2023.*-{architecture}"]},
                {"Name": "state", "Values": ["available"]},
                {"Name": "architecture", "Values": [architecture]},
            ],
        )
    except ClientError as e:
        raise CatsInfrastructureError(
            f"Failed to look up AMI in region '{region}'. "
            + "CATS needs an Amazon Linux 2023 AMI to deploy EC2 test infrastructure. "
            + "Check the deploy role has ec2:DescribeImages permission. "
            + f"AWS error: {e.response['Error']['Code']}"
        ) from e
    images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
    if not images:
        raise CatsInfrastructureError(f"No {architecture} Amazon Linux 2023 AMI found in region '{region}'")
    chosen = images[0]
    logger.info("Using AMI %s (%s) for %s listeners in %s", chosen["ImageId"], chosen.get("Name", "unnamed"), architecture, region)
    return str(chosen["ImageId"])
