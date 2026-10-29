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

"""Resolve SSM parameters and subnet AZs before template generation."""

import re
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import ClientError

from cats.cfn.ami import get_instance_architecture, get_latest_ami
from cats.config import (
    CatsAccountUnderTest,
    CatsAdditionalTestRole,
    NetworkSettings,
)
from cats.exceptions import CatsInfrastructureError

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient


@dataclass
class ResolvedAccountSettings:
    """Fully resolved account settings."""

    additional_roles: dict[str, CatsAdditionalTestRole]
    default_test_role_managed_policies: list[str]
    default_test_role_inline_policies: dict[str, dict[str, object]]
    fixture_scope: Literal["session", "function", "module", "class"]
    allow_shell: bool


@dataclass
class ResolvedSubnet:
    """Subnet with resolved availability zone and route table."""

    subnet_id: str
    availability_zone: str
    route_table_id: str


@dataclass
class ResolvedNetworkTarget:
    """Resolved values for one network target."""

    target_name: str
    vpc_id: str
    ami_id: str
    subnets: list[ResolvedSubnet]
    existing_security_group_ids: list[str] | None
    interface_endpoint_security_group_ids: list[str] | None
    settings: NetworkSettings


@dataclass
class ResolvedAccount:
    """All resolved values for one account."""

    account_id: str
    account_name: str
    network_targets: dict[str, ResolvedNetworkTarget]


def _resolve_ssm_value(ssm_client: "SSMClient", value: str, prefix: str) -> str:
    """Resolve a value that might be an SSM parameter path."""
    if value.startswith(prefix):
        return value
    # It's an SSM parameter path
    response = ssm_client.get_parameter(Name=value)
    return str(response["Parameter"]["Value"])


_RESOLVED_AMI_ID = re.compile(r"^ami-[0-9a-f]{8,17}$")
_RESOLVED_SG_ID = re.compile(r"^sg-[0-9a-f]{8,17}$")
_RESOLVED_VPC_ID = re.compile(r"^vpc-[0-9a-f]{8,17}$")


def _resolve_vpc_id(ssm_client: "SSMClient", reference: str) -> str:
    """Resolve a vpc_id reference and reject anything that is not exactly one VPC ID.

    An SSM StringList or unrelated parameter would otherwise render raw into the
    template's VpcId: and fail opaquely at CloudFormation.
    """
    resolved = _resolve_ssm_value(ssm_client, reference, "vpc-")
    if _RESOLVED_VPC_ID.match(resolved) is None:
        raise CatsInfrastructureError(
            f"vpc_id '{reference}' in cats.yaml resolved to '{resolved[:50]}', which is not a VPC ID. "
            + "Provide a VPC ID (vpc-...) or an SSM parameter path holding exactly one."
        )
    return resolved


def _resolve_security_group_id(ssm_client: "SSMClient", reference: str, field_name: str) -> str:
    """Resolve one security group reference: a literal sg- ID or an SSM parameter path holding one.

    ``field_name`` is the cats.yaml field the reference came from (``existing_security_group_ids``
    or ``interface_endpoint_security_group_ids``), named in the error.
    Validated after resolution because a value that is not a single ID - a StringList of several,
    or an unrelated parameter - would otherwise render into the template and fail opaquely at
    CloudFormation. The template emits one YAML list item per entry, so it cannot split a
    comma-separated value.
    """
    resolved = _resolve_ssm_value(ssm_client, reference, "sg-")
    if _RESOLVED_SG_ID.match(resolved) is None:
        raise CatsInfrastructureError(
            f"{field_name} entry '{reference}' in cats.yaml resolved to '{resolved[:50]}', "
            + "which is not a security group ID. "
            + "Provide a security group ID (sg-...) or an SSM parameter path holding exactly one. "
            + f"For several groups, list one entry per group in {field_name}."
        )
    return resolved


def _resolve_security_group_ids(ssm_client: "SSMClient", references: list[str] | None, field_name: str) -> list[str] | None:
    """Resolve every reference of one security group field; None stays None (the field is unset)."""
    if references is None:
        return None
    return [_resolve_security_group_id(ssm_client, reference, field_name) for reference in references]


def _resolve_ami_id(ssm_client: "SSMClient", session: boto3.Session, override: str | None, instance_type: str) -> str:
    """Resolve the effective AMI for a target: default lookup for the instance type's architecture, literal ID, or SSM parameter path."""
    if override is None:
        architecture = get_instance_architecture(session, session.region_name, instance_type)
        return get_latest_ami(session, session.region_name, architecture)
    resolved = _resolve_ssm_value(ssm_client, override, "ami-")
    if _RESOLVED_AMI_ID.match(resolved) is None:
        raise CatsInfrastructureError(
            f"SSM parameter '{override}' (existing_ec2_ami_id in cats.yaml) resolved to '{resolved[:50]}', which is not an AMI ID. "
            + "The parameter value must be a plain AMI ID (ami-...). "
            + "If this is a JSON 'recommended' parameter, use its /image_id sub-path instead."
        )
    return resolved


def resolve_account(
    account_name: str,
    account: CatsAccountUnderTest,
    session: boto3.Session,
) -> ResolvedAccount:
    """Resolve all SSM parameters and get subnet AZs before template generation."""
    ssm = session.client("ssm")
    ec2 = session.client("ec2")

    network_targets: dict[str, ResolvedNetworkTarget] = {}

    @cache
    def resolve_ami_id(override: str | None, instance_type: str) -> str:
        """Resolve each distinct (override, instance type) pair once per account."""
        return _resolve_ami_id(ssm, session, override, instance_type)

    for target_name, target in account.account_network_under_test.items():
        try:
            # Resolve VPC ID (might be SSM path)
            vpc_id = _resolve_vpc_id(ssm, target.vpc_id)

            # Resolve AMI (default lookup, literal ID, or SSM path)
            ami_id = resolve_ami_id(target.network_settings.existing_ec2_ami_id, target.network_settings.ec2_instance_type)

            # Resolve subnet IDs (might be SSM paths)
            subnet_ids: list[str] = []
            for subnet in target.subnet_ids:
                resolved = _resolve_ssm_value(ssm, subnet, "subnet-")
                subnet_ids.append(resolved)

            # Resolve security group IDs (might be SSM paths). None means CATS creates its own.
            security_group_ids = _resolve_security_group_ids(ssm, target.existing_security_group_ids, "existing_security_group_ids")

            # Resolve the interface endpoints' security group IDs. None means the VPC's default group.
            endpoint_security_group_ids = _resolve_security_group_ids(
                ssm, target.network_settings.interface_endpoint_security_group_ids, "interface_endpoint_security_group_ids"
            )

            # Get AZs for subnets, in the order cats.yaml lists them: the first subnet is where the
            # EC2 listener goes, and describe_subnets makes no promise about response order.
            described_subnets = {s["SubnetId"]: s for s in ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]}
            subnet_info = [described_subnets[subnet_id] for subnet_id in subnet_ids]

            # Get route tables for subnets
            rt_response = ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": subnet_ids}])
            subnet_to_rt: dict[str, str] = {}
            for rt in rt_response["RouteTables"]:
                for assoc in rt.get("Associations", []):
                    if assoc.get("SubnetId"):
                        subnet_to_rt[assoc["SubnetId"]] = rt["RouteTableId"]

            # Get main route table for subnets without explicit association
            main_rt_id: str | None = None
            for sid in subnet_ids:
                if sid not in subnet_to_rt:
                    if main_rt_id is None:
                        main_rt = ec2.describe_route_tables(
                            Filters=[
                                {"Name": "vpc-id", "Values": [vpc_id]},
                                {"Name": "association.main", "Values": ["true"]},
                            ]
                        )["RouteTables"]
                        main_rt_id = main_rt[0]["RouteTableId"] if main_rt else ""
                    subnet_to_rt[sid] = main_rt_id or ""

        except ClientError as e:
            raise CatsInfrastructureError(
                f"Failed to resolve AWS resources for network target '{target_name}' in target account '{account_name}' (defined in cats.yaml). "
                + "Check: 1) VPC IDs, subnet IDs and security group IDs in cats.yaml are correct, "
                + "2) Any SSM parameter paths (starting with /) exist and are readable, "
                + "3) The deploy role has ec2:DescribeSubnets, ec2:DescribeRouteTables, ssm:GetParameter permissions. "
                + f"AWS error: {e.response['Error']['Code']}"
            ) from e

        # All required data resolved
        subnets = [
            ResolvedSubnet(
                subnet_id=s["SubnetId"],
                availability_zone=s["AvailabilityZone"],
                route_table_id=subnet_to_rt[s["SubnetId"]],
            )
            for s in subnet_info
        ]

        network_targets[target_name] = ResolvedNetworkTarget(
            target_name=target_name,
            vpc_id=vpc_id,
            ami_id=ami_id,
            subnets=subnets,
            existing_security_group_ids=security_group_ids,
            interface_endpoint_security_group_ids=endpoint_security_group_ids,
            settings=target.network_settings,
        )

    return ResolvedAccount(
        account_id=account.account_id,
        account_name=account_name,
        network_targets=network_targets,
    )


# The widest ID each prefix can resolve to: 17 hex characters after the prefix. An offline
# render built from these is at least as long as any deploy-time render of the same config.
_PLACEHOLDER_HEX = "0" * 17


def _placeholder(reference: str, prefix: str) -> str:
    """A literal ID from cats.yaml as written; anything that would be looked up as the widest ID of its kind."""
    if reference.startswith(prefix):
        return reference
    return f"{prefix}{_PLACEHOLDER_HEX}"


def _placeholder_security_group_ids(references: list[str] | None) -> list[str] | None:
    """Each reference of one security group field as a placeholder; None stays None (the field is unset)."""
    if references is None:
        return None
    return [_placeholder(reference, "sg-") for reference in references]


def placeholder_resolve_account(account_name: str, account: CatsAccountUnderTest) -> ResolvedAccount:
    """The shape ``resolve_account`` would return, built without AWS.

    Every value the real resolver fetches is an ID of bounded width, so a render of this account is
    an upper bound on the deploy-time render's size, and exact when cats.yaml holds literal IDs.
    One SSM path resolves to one subnet (``resolve_account`` rejects anything else), and every
    subnet gets its own route table ID, the most the endpoint resources can render.
    """
    network_targets: dict[str, ResolvedNetworkTarget] = {}
    for target_name, target in account.account_network_under_test.items():
        ami_reference = target.network_settings.existing_ec2_ami_id
        network_targets[target_name] = ResolvedNetworkTarget(
            target_name=target_name,
            vpc_id=_placeholder(target.vpc_id, "vpc-"),
            ami_id=f"ami-{_PLACEHOLDER_HEX}" if ami_reference is None else _placeholder(ami_reference, "ami-"),
            subnets=[
                ResolvedSubnet(
                    subnet_id=_placeholder(subnet, "subnet-"),
                    availability_zone="",
                    route_table_id=f"rtb-{index:017x}",
                )
                for index, subnet in enumerate(target.subnet_ids)
            ],
            existing_security_group_ids=_placeholder_security_group_ids(target.existing_security_group_ids),
            interface_endpoint_security_group_ids=_placeholder_security_group_ids(target.network_settings.interface_endpoint_security_group_ids),
            settings=target.network_settings,
        )
    return ResolvedAccount(account_id=account.account_id, account_name=account_name, network_targets=network_targets)


def resolve_account_settings(account: CatsAccountUnderTest) -> ResolvedAccountSettings:
    """The account-level settings the template renders, taken from the hierarchically resolved account."""
    return ResolvedAccountSettings(
        additional_roles=account.account_settings.additional_roles,
        default_test_role_managed_policies=account.account_settings.default_test_role_managed_policies,
        default_test_role_inline_policies=account.account_settings.default_test_role_inline_policies,
        fixture_scope=account.account_settings.fixture_scope,
        allow_shell=account.account_settings.allow_shell,
    )
