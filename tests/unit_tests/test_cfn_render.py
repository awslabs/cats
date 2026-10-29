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

"""Tests for cats.cfn.render module - CloudFormation template generation."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from syrupy.assertion import SnapshotAssertion

from cats.cfn.render import (
    _PLACEHOLDER_REGION,
    CFN_TEMPLATE_BODY_LIMIT,
    estimate_template_size,
    reject_oversized_template,
    render_template,
    template_size,
)
from cats.cfn.resolve import ResolvedAccount, ResolvedAccountSettings, placeholder_resolve_account, resolve_account, resolve_account_settings
from cats.config import (
    CatsConfig,
    CatsDeploymentSettings,
    ReportFormat,
    interface_endpoint_logical_id_part,
)
from cats.exceptions import CatsConfigError
from tests.utils import load_cfn_template, make_cats_config, make_config, normalize_aws_ids, write_config

ACCOUNT_NAME = "testaccount"


def build_scenarios(infra: dict[str, str]) -> dict[str, CatsConfig]:
    """Build all test scenarios using moto infrastructure IDs."""
    return {
        "single_target": make_cats_config(
            network_targets={
                "privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"], infra["SUBNET_B"]]},
            },
        ),
        "multiple_targets": make_cats_config(
            network_targets={
                "appTier": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]},
                "dataTier": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_B"]]},
                "dmz": {"vpc_id": infra["VPC2"], "subnet_ids": [infra["SUBNET2_A"]]},
            },
        ),
        "existing_security_groups": make_cats_config(
            network_targets={
                "privateSubnet": {
                    "vpc_id": infra["VPC"],
                    "subnet_ids": [infra["SUBNET_A"]],
                    "existing_security_group_ids": [infra["SG"]],
                },
            },
        ),
        "lambda_existing_security_groups": make_cats_config(
            network_targets={
                "privateSubnet": {
                    "vpc_id": infra["VPC"],
                    "subnet_ids": [infra["SUBNET_A"]],
                    "existing_security_group_ids": [infra["SG"]],
                },
            },
            enabled_network_handlers=["lambda", "ec2"],
        ),
        "additional_roles": make_cats_config(
            network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            additional_roles={"readonly": {"managed_policies": ["ReadOnlyAccess"]}},
            default_test_role_managed_policies=["SecurityAudit"],
        ),
        "gateway_endpoints": make_cats_config(
            network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            gateway_endpoints=["S3", "DYNAMODB"],
        ),
        "interface_endpoints": make_cats_config(
            network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            interface_endpoints=["ssm", "lambda"],
        ),
        "interface_endpoint_security_groups": make_cats_config(
            network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            interface_endpoints=["ssm"],
            interface_endpoint_security_group_ids=[infra["SG"]],
        ),
        "no_lambda_handler": make_cats_config(
            network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            enabled_network_handlers=[],
        ),
        "ec2_handler": make_cats_config(
            network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            enabled_network_handlers=["ec2"],
        ),
        "custom_instance_profile": make_cats_config(
            network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
            existing_ec2_instance_profile_name="EC2-Default-SSM-Role",
        ),
        "allow_shell": CatsConfig.model_validate(
            make_config(
                network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}},
                account_settings={"allow_shell": True},
            )
        ),
        "securityhub": CatsConfig.model_validate(
            make_config(network_targets={"privateSubnet": {"vpc_id": infra["VPC"], "subnet_ids": [infra["SUBNET_A"]]}})
            | {"reporting": {"report_formats": ["cats_json", "securityhub"], "securityhub": {"aws_region": "eu-west-1"}}}
        ),
    }


@pytest.fixture
def scenario(mock_aws_infra: dict[str, str], request: pytest.FixtureRequest) -> CatsConfig:
    """Build scenario config using moto IDs."""
    scenarios = build_scenarios(mock_aws_infra)
    return scenarios[request.param]


@pytest.fixture
def scenarios(mock_aws_infra: dict[str, str]) -> dict[str, CatsConfig]:
    """All scenarios as a dict for direct access."""
    return build_scenarios(mock_aws_infra)


SCENARIO_NAMES = [
    "single_target",
    "multiple_targets",
    "existing_security_groups",
    "lambda_existing_security_groups",
    "additional_roles",
    "gateway_endpoints",
    "interface_endpoints",
    "interface_endpoint_security_groups",
    "no_lambda_handler",
    "ec2_handler",
    "custom_instance_profile",
    "allow_shell",
    "securityhub",
]


@pytest.mark.parametrize("scenario", SCENARIO_NAMES, indirect=True)
def test_snapshot_render_template(
    cats_yaml: Path,
    snapshot_cfn: SnapshotAssertion,
    scenario: CatsConfig,
) -> None:
    # arrange
    write_config(cats_yaml, scenario.model_dump(mode="json"))
    account = scenario.accounts_under_test[ACCOUNT_NAME]

    # act
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    account_settings = ResolvedAccountSettings(
        additional_roles=account.account_settings.additional_roles,
        default_test_role_managed_policies=account.account_settings.default_test_role_managed_policies,
        default_test_role_inline_policies=account.account_settings.default_test_role_inline_policies,
        fixture_scope=account.account_settings.fixture_scope,
        allow_shell=account.account_settings.allow_shell,
    )
    result = render_template(
        resolved=resolved,
        deployment_settings=scenario.cats_deployment_settings,
        account_settings=account_settings,
        securityhub_region=scenario.effective_securityhub_region(),
    )

    # assert
    assert normalize_aws_ids(result) == snapshot_cfn


# =============================================================================
# Focused tests - prove specific features render correctly
# =============================================================================


def _render(config: CatsConfig) -> str:
    """Render a config to CFN template."""
    account = config.accounts_under_test[ACCOUNT_NAME]
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    account_settings = ResolvedAccountSettings(
        additional_roles=account.account_settings.additional_roles,
        default_test_role_managed_policies=account.account_settings.default_test_role_managed_policies,
        default_test_role_inline_policies=account.account_settings.default_test_role_inline_policies,
        fixture_scope=account.account_settings.fixture_scope,
        allow_shell=account.account_settings.allow_shell,
    )
    return render_template(resolved, config.cats_deployment_settings, account_settings, securityhub_region=config.effective_securityhub_region())


def test_render_without_parent_account_id_raises_config_error() -> None:
    """A trust policy must never render with an account nobody chose."""
    # arrange
    resolved = ResolvedAccount(account_id="111111111111", account_name=ACCOUNT_NAME, network_targets={})
    deployment_settings = CatsDeploymentSettings(parent_account_id=None)
    account_settings = ResolvedAccountSettings(
        additional_roles={},
        default_test_role_managed_policies=[],
        default_test_role_inline_policies={},
        fixture_scope="session",
        allow_shell=False,
    )
    expected_message = (
        "cats_deployment_settings.parent_account_id is not set. "
        + "Test roles in each account under test trust this account: "
        + "set it to the 12-digit ID of the account that runs your tests."
    )

    # act
    with pytest.raises(CatsConfigError) as excinfo:
        render_template(resolved, deployment_settings, account_settings, securityhub_region=None)

    # assert
    assert str(excinfo.value) == expected_message


def test_additional_roles_renders_extra_iam_role(scenarios: dict[str, CatsConfig]) -> None:
    """Additional roles config must render the extra IAM role, named by its key, carrying its managed policies."""
    # act
    template = yaml.safe_load(_render(scenarios["additional_roles"]))
    role = template["Resources"]["CatsAdditionalRolereadonly"]

    # assert
    expected = ("AWS::IAM::Role", "CATS-test-infra-testaccount-readonly", ["arn:aws:iam::aws:policy/ReadOnlyAccess"])
    assert (role["Type"], role["Properties"]["RoleName"], role["Properties"]["ManagedPolicyArns"]) == expected


def test_role_logical_id_drops_underscores_and_role_name_keeps_them(mock_aws_infra: dict[str, str]) -> None:
    """A CloudFormation logical ID is alphanumeric; an IAM RoleName is not so constrained."""
    # arrange
    config = CatsConfig.model_validate(
        make_config(
            network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
            additional_roles={"read_only": {"managed_policies": ["ReadOnlyAccess"]}},
        )
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert
    assert template["Resources"]["CatsAdditionalRolereadonly"]["Properties"]["RoleName"] == "CATS-test-infra-testaccount-read_only"


def test_reordering_roles_leaves_template_resources_unchanged(mock_aws_infra: dict[str, str]) -> None:
    """Logical IDs derive from the role name, so reordering roles in cats.yaml does not replace them on update."""
    # arrange
    auditor: dict[str, object] = {"managed_policies": ["SecurityAudit"]}
    readonly: dict[str, object] = {"managed_policies": ["ReadOnlyAccess"]}
    network_targets = {"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    first = CatsConfig.model_validate(make_config(network_targets=network_targets, additional_roles={"auditor": auditor, "readonly": readonly}))
    second = CatsConfig.model_validate(make_config(network_targets=network_targets, additional_roles={"readonly": readonly, "auditor": auditor}))

    # act
    first_template = yaml.safe_load(_render(first))
    second_template = yaml.safe_load(_render(second))

    # assert
    assert first_template == second_template


def test_default_test_role_inline_policies_renders_policies(mock_aws_infra: dict[str, str]) -> None:
    """Inline policies in account settings must render as Policies on CatsTestRole."""
    # arrange
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["dynamodb:GetItem"], "Resource": "*"}],
    }
    config = CatsConfig.model_validate(
        make_config(
            network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
            account_settings={"default_test_role_inline_policies": {"dynamo-read": policy_document}},
        )
    )

    # act
    result = _render(config)

    # assert
    expected = [{"PolicyName": "dynamo-read", "PolicyDocument": policy_document}]
    template = yaml.safe_load(result)
    assert expected == template["Resources"]["CatsTestRole"]["Properties"]["Policies"]


def test_additional_role_inline_policies_renders_policies(mock_aws_infra: dict[str, str]) -> None:
    """Inline policies on an additional role must render as Policies on that role."""
    # arrange
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}],
    }
    config = CatsConfig.model_validate(
        make_config(
            network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
            additional_roles={"s3reader": {"inline_policies": {"s3-read": policy_document}}},
        )
    )

    # act
    result = _render(config)

    # assert
    expected = [{"PolicyName": "s3-read", "PolicyDocument": policy_document}]
    template = yaml.safe_load(result)
    assert expected == template["Resources"]["CatsAdditionalRoles3reader"]["Properties"]["Policies"]


def test_no_inline_policies_omits_policies_property(scenarios: dict[str, CatsConfig]) -> None:
    """Without inline policies configured, CatsTestRole must not have a Policies property."""
    # act
    result = _render(scenarios["single_target"])

    # assert
    template = yaml.safe_load(result)
    assert "Policies" not in template["Resources"]["CatsTestRole"]["Properties"]


def _vpc_endpoints(template: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Logical id -> (endpoint type, service name) for every VPC endpoint in the template."""
    return {
        name: (resource["Properties"]["VpcEndpointType"], resource["Properties"]["ServiceName"])
        for name, resource in template["Resources"].items()
        if resource["Type"] == "AWS::EC2::VPCEndpoint"
    }


def test_gateway_endpoints_renders_vpc_endpoints(scenarios: dict[str, CatsConfig]) -> None:
    """Gateway endpoints config must render one Gateway VPCEndpoint per service."""
    # act
    template = yaml.safe_load(_render(scenarios["gateway_endpoints"]))

    # assert
    expected = {
        "GatewayEndpointS3Vpc1": ("Gateway", "com.amazonaws.us-east-1.s3"),
        "GatewayEndpointDYNAMODBVpc1": ("Gateway", "com.amazonaws.us-east-1.dynamodb"),
    }
    assert _vpc_endpoints(template) == expected


def test_s3_express_gateway_endpoint_renders_a_valid_logical_id_and_service_name(mock_aws_infra: dict[str, str]) -> None:
    """S3_EXPRESS renders as the logical id GatewayEndpointS3EXPRESSVpc1 (no underscore) and the service name s3express."""
    # arrange
    config = make_cats_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        gateway_endpoints=["S3_EXPRESS"],
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert
    expected = {"GatewayEndpointS3EXPRESSVpc1": ("Gateway", "com.amazonaws.us-east-1.s3express")}
    assert _vpc_endpoints(template) == expected


def test_interface_endpoints_renders_vpc_endpoints(scenarios: dict[str, CatsConfig]) -> None:
    """Interface endpoints config must render one Interface VPCEndpoint per service."""
    # act
    template = yaml.safe_load(_render(scenarios["interface_endpoints"]))

    # assert
    expected = {
        "InterfaceEndpointSsmVpc1": ("Interface", "com.amazonaws.us-east-1.ssm"),
        "InterfaceEndpointLambdaVpc1": ("Interface", "com.amazonaws.us-east-1.lambda"),
    }
    assert _vpc_endpoints(template) == expected


def test_interface_endpoint_logical_id_matches_the_config_validators_prediction(mock_aws_infra: dict[str, str]) -> None:
    """The validator that rejects colliding endpoint names computes the id the template renders; a dotted name is the case that differs."""
    # arrange
    config = make_cats_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        interface_endpoints=["ecr.api"],
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert
    expected = {f"InterfaceEndpoint{interface_endpoint_logical_id_part('ecr.api')}Vpc1": ("Interface", "com.amazonaws.us-east-1.ecr.api")}
    assert _vpc_endpoints(template) == expected


# =============================================================================
# VPC endpoint policy and interface endpoint security groups
# =============================================================================

# The policy every CATS-created VPC endpoint carries: any principal, any action, any resource, in
# the account under test only. Without it an endpoint takes AWS's default, which has no account condition.
ACCOUNT_SCOPED_ENDPOINT_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": "*",
            "Action": "*",
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:PrincipalAccount": {"Ref": "AWS::AccountId"}}},
        }
    ],
}


def _both_endpoint_types_config(mock_aws_infra: dict[str, str], interface_endpoint_security_group_ids: list[str] | None) -> CatsConfig:
    """One target with an S3 gateway endpoint and an ssm interface endpoint, optionally pinning the interface endpoint's groups."""
    return make_cats_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        gateway_endpoints=["S3"],
        interface_endpoints=["ssm"],
        interface_endpoint_security_group_ids=interface_endpoint_security_group_ids,
    )


def _vpc_endpoint_property(template: dict[str, Any], key: str) -> dict[str, Any]:
    """Logical id -> the value of one property (None when absent) for every VPC endpoint in the template."""
    return {name: resource["Properties"].get(key) for name, resource in template["Resources"].items() if resource["Type"] == "AWS::EC2::VPCEndpoint"}


def test_every_vpc_endpoint_carries_the_account_scoped_policy(mock_aws_infra: dict[str, str]) -> None:
    """Gateway and interface endpoints alike: an endpoint with no PolicyDocument allows any principal anywhere."""
    # act
    template = load_cfn_template(_render(_both_endpoint_types_config(mock_aws_infra, None)))

    # assert
    expected = {"GatewayEndpointS3Vpc1": ACCOUNT_SCOPED_ENDPOINT_POLICY, "InterfaceEndpointSsmVpc1": ACCOUNT_SCOPED_ENDPOINT_POLICY}
    assert _vpc_endpoint_property(template, "PolicyDocument") == expected


def test_interface_endpoint_security_groups_render_on_interface_endpoints_only(mock_aws_infra: dict[str, str]) -> None:
    """The override attaches to the interface endpoint; a gateway endpoint takes no security group."""
    # act
    template = yaml.safe_load(_render(_both_endpoint_types_config(mock_aws_infra, [mock_aws_infra["SG"]])))

    # assert
    expected = {"GatewayEndpointS3Vpc1": None, "InterfaceEndpointSsmVpc1": [mock_aws_infra["SG"]]}
    assert _vpc_endpoint_property(template, "SecurityGroupIds") == expected


def test_interface_endpoints_without_security_groups_omit_the_property(mock_aws_infra: dict[str, str]) -> None:
    """Unset, the endpoint renders no SecurityGroupIds and so carries the VPC's default group, as it did before the override existed."""
    # act
    template = yaml.safe_load(_render(_both_endpoint_types_config(mock_aws_infra, None)))

    # assert
    expected = {"GatewayEndpointS3Vpc1": None, "InterfaceEndpointSsmVpc1": None}
    assert _vpc_endpoint_property(template, "SecurityGroupIds") == expected


def test_target_interface_endpoint_security_groups_override_global(mock_aws_infra: dict[str, str]) -> None:
    """The setting is hierarchical like the endpoint lists: a target's groups win over the global ones."""
    # arrange
    target_group = (
        boto3.Session(region_name="us-east-1")
        .client("ec2")
        .create_security_group(GroupName="endpoint-sg", Description="endpoint", VpcId=mock_aws_infra["VPC"])["GroupId"]
    )
    config = make_cats_config(
        network_targets={
            "privateSubnet": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"interface_endpoint_security_group_ids": [target_group]},
            }
        },
        interface_endpoints=["ssm"],
        interface_endpoint_security_group_ids=[mock_aws_infra["SG"]],
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert
    assert template["Resources"]["InterfaceEndpointSsmVpc1"]["Properties"]["SecurityGroupIds"] == [target_group]


def test_custom_instance_profile_uses_provided_name(scenarios: dict[str, CatsConfig]) -> None:
    """Custom instance profile config must use provided profile name and skip role creation."""
    # act
    template = yaml.safe_load(_render(scenarios["custom_instance_profile"]))
    iam_resources = sorted(name for name, r in template["Resources"].items() if r["Type"] in ("AWS::IAM::Role", "AWS::IAM::InstanceProfile"))

    # assert - the listener uses the provided profile and the only IAM role is the test role
    expected = ("EC2-Default-SSM-Role", ["CatsTestRole"])
    assert (template["Resources"]["Ec2ListenerprivateSubnet"]["Properties"]["IamInstanceProfile"], iam_resources) == expected


# =============================================================================
# existing_security_group_ids on Lambda targets
# =============================================================================


def _lambda_target_config(mock_aws_infra: dict[str, str], existing_security_group_ids: list[str] | None) -> CatsConfig:
    """Build a single lambda+ec2 target config, optionally pinned to pre-existing security groups."""
    target: dict[str, Any] = {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}
    if existing_security_group_ids is not None:
        target["existing_security_group_ids"] = existing_security_group_ids
    return make_cats_config(
        network_targets={"privateSubnet": target},
        enabled_network_handlers=["lambda", "ec2"],
    )


def _security_group_logical_ids(template: dict[str, Any]) -> list[str]:
    """Logical IDs of every security group the stack owns, in template order."""
    return [name for name, resource in template["Resources"].items() if resource["Type"] == "AWS::EC2::SecurityGroup"]


def test_lambda_target_with_existing_security_groups_owns_no_security_group(mock_aws_infra: dict[str, str]) -> None:
    """A stack-owned lambda security group cannot be deleted until Lambda releases its Hyperplane ENIs.

    Providing existing_security_group_ids must leave the stack owning no security group at all,
    so `cats infra destroy` never waits on ENI release.
    """
    # arrange
    config = _lambda_target_config(mock_aws_infra, [mock_aws_infra["SG"]])

    # act
    template = yaml.safe_load(_render(config))

    # assert
    expected: list[str] = []
    assert expected == _security_group_logical_ids(template)


def test_lambda_target_without_existing_security_groups_owns_lambda_and_ec2_groups(mock_aws_infra: dict[str, str]) -> None:
    """Without existing_security_group_ids, CATS creates its own group for each handler."""
    # arrange
    config = _lambda_target_config(mock_aws_infra, None)

    # act
    template = yaml.safe_load(_render(config))

    # assert
    expected = ["LambdaSGprivateSubnet", "Ec2SGprivateSubnet"]
    assert expected == _security_group_logical_ids(template)


def test_lambda_target_with_existing_security_groups_attaches_them_to_the_probe_lambda(mock_aws_infra: dict[str, str]) -> None:
    """The probe lambda must attach every provided group, not a stack-owned reference."""
    # arrange - two groups prove the list renders as separate YAML items
    second_group = (
        boto3.Session(region_name="us-east-1")
        .client("ec2")
        .create_security_group(
            GroupName="second-sg",
            Description="second",
            VpcId=mock_aws_infra["VPC"],
        )["GroupId"]
    )
    group_ids = [mock_aws_infra["SG"], second_group]
    config = _lambda_target_config(mock_aws_infra, group_ids)

    # act
    template = yaml.safe_load(_render(config))

    # assert
    expected = {"ProbeLambdaprivateSubnet": group_ids}
    attached = {
        name: resource["Properties"]["VpcConfig"]["SecurityGroupIds"]
        for name, resource in template["Resources"].items()
        if resource["Type"] == "AWS::Lambda::Function"
    }
    assert expected == attached


# =============================================================================
# EC2 listener security group ingress scoped to listen ports
# =============================================================================


def test_ec2_security_group_ingress_has_one_rule_per_listen_port(mock_aws_infra: dict[str, str]) -> None:
    """Ingress must be exactly one rule per configured port: tcp rules then udp rules."""
    # arrange
    config = make_cats_config(
        network_targets={
            "privateSubnet": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"tcp_listen_ports": [8080, 443], "udp_listen_ports": [5000]},
            },
        },
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert - one IPv4 and one IPv6 rule per port, so a dual-stack VPC probes the listener over either family
    expected = [
        {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "CidrIp": "0.0.0.0/0"},
        {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "CidrIpv6": "::/0"},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIpv6": "::/0"},
        {"IpProtocol": "udp", "FromPort": 5000, "ToPort": 5000, "CidrIp": "0.0.0.0/0"},
        {"IpProtocol": "udp", "FromPort": 5000, "ToPort": 5000, "CidrIpv6": "::/0"},
    ]
    assert expected == template["Resources"]["Ec2SGprivateSubnet"]["Properties"]["SecurityGroupIngress"]


def test_ec2_security_group_with_no_listen_ports_has_no_ingress(mock_aws_infra: dict[str, str]) -> None:
    """With no ports configured, the group must have no SecurityGroupIngress property at all."""
    # arrange
    config = make_cats_config(
        network_targets={
            "privateSubnet": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"tcp_listen_ports": [], "udp_listen_ports": []},
            },
        },
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert
    assert "SecurityGroupIngress" not in template["Resources"]["Ec2SGprivateSubnet"]["Properties"]


def test_userdata_is_a_per_target_prefix_over_the_shared_script(mock_aws_infra: dict[str, str]) -> None:
    """Each listener's user data is its own port prefix plus the one embedded script; only a UDP target's prefix carries the token."""
    # arrange
    config = make_cats_config(
        network_targets={
            "withUdp": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"tcp_listen_ports": [8080], "udp_listen_ports": [5000]},
            },
            "tcpOnly": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"tcp_listen_ports": [8080], "udp_listen_ports": []},
            },
        },
    )

    # act
    template = load_cfn_template(_render(config))
    with_udp_sub = template["Resources"]["Ec2ListenerwithUdp"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    tcp_only_sub = template["Resources"]["Ec2ListenertcpOnly"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]

    # assert
    userdata = {"Userdata": {"FindInMap": ["CatsEmbeddedCode", "code", "userdata"]}}
    expected = (
        "#!/bin/bash\nCATS_TCP_PORTS=8080\nCATS_UDP_PORTS=5000\nCATS_BUCKET=${CatsResponseBucket}\nCATS_UDP_RECEIPT_TOKEN=${CatsUdpReceiptToken}\n${Userdata}\n",
        userdata,
        "#!/bin/bash\nCATS_TCP_PORTS=8080\nCATS_UDP_PORTS=\n${Userdata}\n",
        userdata,
    )
    assert (with_udp_sub[0], with_udp_sub[1], tcp_only_sub[0], tcp_only_sub[1]) == expected


def test_ec2_security_group_egress_remains_open(mock_aws_infra: dict[str, str]) -> None:
    """Egress stays allow-all: the listener needs S3 (UDP receipts) and SSM outbound."""
    # arrange
    config = make_cats_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert
    expected = [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}, {"IpProtocol": "-1", "CidrIpv6": "::/0"}]
    assert expected == template["Resources"]["Ec2SGprivateSubnet"]["Properties"]["SecurityGroupEgress"]


def test_listener_root_volume_is_the_image_size_on_gp3(scenarios: dict[str, CatsConfig]) -> None:
    """The AL2023 snapshot is 8 GiB and the listener writes nothing; a 50 GiB gp2 volume was cost without purpose."""
    # act
    template = yaml.safe_load(_render(scenarios["single_target"]))

    # assert
    expected = [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "Encrypted": True}}]
    assert template["Resources"]["Ec2ListenerprivateSubnet"]["Properties"]["BlockDeviceMappings"] == expected


# =============================================================================
# VPC endpoint service names follow the partition
# =============================================================================


def _render_in_partition(mock_aws_infra: dict[str, str], partition: str, region: str) -> str:
    config_dict = make_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        gateway_endpoints=["S3"],
        interface_endpoints=["ssm"],
    )
    config_dict["cats_deployment_settings"]["partition"] = partition
    config_dict["cats_deployment_settings"]["aws_region"] = region
    return _render(CatsConfig.model_validate(config_dict))


def test_endpoint_service_names_in_aws_cn_carry_the_cn_prefix(mock_aws_infra: dict[str, str]) -> None:
    # act
    template = yaml.safe_load(_render_in_partition(mock_aws_infra, "aws-cn", "cn-north-1"))
    endpoints = {name: r["Properties"]["ServiceName"] for name, r in template["Resources"].items() if r["Type"] == "AWS::EC2::VPCEndpoint"}

    # assert
    expected = {"GatewayEndpointS3Vpc1": "cn.com.amazonaws.cn-north-1.s3", "InterfaceEndpointSsmVpc1": "cn.com.amazonaws.cn-north-1.ssm"}
    assert endpoints == expected


def test_endpoint_service_names_in_aws_us_gov_use_the_standard_prefix(mock_aws_infra: dict[str, str]) -> None:
    # act
    template = yaml.safe_load(_render_in_partition(mock_aws_infra, "aws-us-gov", "us-gov-west-1"))
    endpoints = {name: r["Properties"]["ServiceName"] for name, r in template["Resources"].items() if r["Type"] == "AWS::EC2::VPCEndpoint"}

    # assert
    expected = {"GatewayEndpointS3Vpc1": "com.amazonaws.us-gov-west-1.s3", "InterfaceEndpointSsmVpc1": "com.amazonaws.us-gov-west-1.ssm"}
    assert endpoints == expected


# =============================================================================
# VPC endpoint conflict validation tests
# =============================================================================


def test_conflicting_gateway_endpoints_in_same_vpc_raises(mock_aws_infra: dict[str, str]) -> None:
    """Two targets in same VPC with different gateway_endpoints must raise CatsConfigError."""
    # arrange - two targets in same VPC with different gateway endpoints
    config = make_cats_config(
        network_targets={
            "targetA": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"gateway_endpoints": ["S3"]},
            },
            "targetB": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_B"]],
                "network_settings": {"gateway_endpoints": ["DYNAMODB"]},
            },
        },
    )

    # act & assert
    with pytest.raises(CatsConfigError, match=r"VPC .* has conflicting gateway_endpoints"):
        _render(config)


def test_conflicting_interface_endpoints_in_same_vpc_raises(mock_aws_infra: dict[str, str]) -> None:
    """Two targets in same VPC with different interface_endpoints must raise CatsConfigError."""
    # arrange - two targets in same VPC with different interface endpoints
    config = make_cats_config(
        network_targets={
            "targetA": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"interface_endpoints": ["ssm"]},
            },
            "targetB": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_B"]],
                "network_settings": {"interface_endpoints": ["lambda"]},
            },
        },
    )

    # act & assert
    with pytest.raises(CatsConfigError, match=r"VPC .* has conflicting interface_endpoints"):
        _render(config)


def test_conflicting_interface_endpoint_security_groups_in_same_vpc_raises(mock_aws_infra: dict[str, str]) -> None:
    """The interface endpoints are created once per VPC, so two targets there cannot ask for different groups on them."""
    # arrange
    config = make_cats_config(
        network_targets={
            "targetA": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"interface_endpoint_security_group_ids": [mock_aws_infra["SG"]]},
            },
            "targetB": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"]]},
        },
        interface_endpoints=["ssm"],
    )
    expected_message = (
        f"VPC {mock_aws_infra['VPC']} has conflicting interface_endpoint_security_group_ids: ['{mock_aws_infra['SG']}'] vs None. "
        + "All targets in the same VPC must have identical endpoint settings."
    )

    # act
    with pytest.raises(CatsConfigError) as excinfo:
        _render(config)

    # assert
    assert str(excinfo.value) == expected_message


def test_same_endpoints_in_same_vpc_succeeds(mock_aws_infra: dict[str, str]) -> None:
    """Two targets in same VPC with identical endpoint settings must succeed."""
    # arrange - two targets in same VPC with same endpoints
    config = make_cats_config(
        network_targets={
            "targetA": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"gateway_endpoints": ["S3"]},
            },
            "targetB": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_B"]],
                "network_settings": {"gateway_endpoints": ["S3"]},
            },
        },
    )

    # act - should not raise
    result = _render(config)

    # assert - only one S3 endpoint created
    assert result.count("com.amazonaws.us-east-1.s3") == 1


# =============================================================================
# Hierarchical settings tests - prove per-target overrides affect CFN output
# =============================================================================


@pytest.mark.parametrize(
    ("setting_name", "global_val", "target_val", "cfn_check"),
    [
        ("tcp_listen_ports", [8080], [443], lambda r: "443" in r),
        ("udp_listen_ports", [5000], [6000], lambda r: "6000" in r),
        ("ec2_instance_type", "t3.micro", "t3.small", lambda r: "InstanceType: t3.small" in r),
        ("interface_endpoints", ["ssm"], ["lambda"], lambda r: "com.amazonaws.us-east-1.lambda" in r),
    ],
)
def test_target_setting_overrides_global(
    mock_aws_infra: dict[str, str],
    setting_name: str,
    global_val: object,
    target_val: object,
    cfn_check: Callable[[str], bool],
) -> None:
    """Per-target network_settings override global_network_settings in CFN output."""
    # arrange
    config_dict = make_config(
        network_targets={
            "privateSubnet": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {setting_name: target_val},
            }
        },
    )
    config_dict["global_network_settings"][setting_name] = global_val
    config = CatsConfig.model_validate(config_dict)

    # act
    result = _render(config)

    # assert
    assert cfn_check(result), f"CFN output check failed for {setting_name}"


def test_target_network_settings_overrides_global(mock_aws_infra: dict[str, str]) -> None:
    """Per-target network_settings overrides global_network_settings in CFN output."""
    # arrange - global has ec2, target overrides to lambda only
    config = make_cats_config(
        network_targets={
            "privateSubnet": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"enabled_network_handlers": ["lambda"]},
            }
        },
        enabled_network_handlers=["ec2"],  # global default
    )

    # act
    template = yaml.safe_load(_render(config))

    # assert - the target's handler list wins: a probe Lambda exists and the outputs say lambda only
    expected = ("AWS::Lambda::Function", "lambda")
    assert (template["Resources"]["ProbeLambdaprivateSubnet"]["Type"], template["Outputs"]["EnabledHandlersprivateSubnet"]["Value"]) == expected


def test_account_network_settings_overrides_global(mock_aws_infra: dict[str, str]) -> None:
    """Per-account network_settings overrides global_network_settings in CFN output."""
    # arrange - global has S3 endpoint, account opts out
    config_dict = make_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        gateway_endpoints=["S3"],  # global default
        account_network_settings={"gateway_endpoints": []},  # account opts out
    )
    config = CatsConfig.model_validate(config_dict)

    # act
    template = yaml.safe_load(_render(config))

    # assert - no endpoint at all (account opted out of the global S3 one)
    assert _vpc_endpoints(template) == {}


def test_default_instance_profile_creates_role(scenarios: dict[str, CatsConfig]) -> None:
    """Without custom instance profile, CATS must create its own role and profile and attach them."""
    # act
    template = load_cfn_template(_render(scenarios["single_target"]))
    resources = template["Resources"]

    # assert
    expected = ("AWS::IAM::Role", "AWS::IAM::InstanceProfile", [{"Ref": "CatsEc2Role"}], {"Ref": "CatsEc2InstanceProfile"})
    actual = (
        resources["CatsEc2Role"]["Type"],
        resources["CatsEc2InstanceProfile"]["Type"],
        resources["CatsEc2InstanceProfile"]["Properties"]["Roles"],
        resources["Ec2ListenerprivateSubnet"]["Properties"]["IamInstanceProfile"],
    )
    assert actual == expected


def test_every_listener_in_a_stack_shares_one_instance_role_and_profile(scenarios: dict[str, CatsConfig]) -> None:
    """Every listener runs with the same permissions, so one role serves them all; per-target copies only cost template bytes."""
    # act
    template = load_cfn_template(_render(scenarios["multiple_targets"]))
    resources = template["Resources"]

    # assert
    expected = (
        ["CatsEc2InstanceProfile", "CatsEc2Role"],
        "CATS-test-infra-testaccount-ec2",
        [{"Ref": "CatsEc2InstanceProfile"}] * 3,
    )
    actual = (
        sorted(name for name, resource in resources.items() if resource["Type"] in {"AWS::IAM::InstanceProfile", "AWS::IAM::Role"} and "Ec2" in name),
        resources["CatsEc2Role"]["Properties"]["RoleName"],
        [resources[f"Ec2Listener{target}"]["Properties"]["IamInstanceProfile"] for target in ("appTier", "dataTier", "dmz")],
    )
    assert actual == expected


def test_stack_with_only_existing_instance_profiles_renders_no_instance_role(scenarios: dict[str, CatsConfig]) -> None:
    # act
    template = load_cfn_template(_render(scenarios["custom_instance_profile"]))

    # assert
    expected = {"CatsEc2Role": False, "CatsEc2InstanceProfile": False, "IamInstanceProfile": "EC2-Default-SSM-Role"}
    actual = {
        "CatsEc2Role": "CatsEc2Role" in template["Resources"],
        "CatsEc2InstanceProfile": "CatsEc2InstanceProfile" in template["Resources"],
        "IamInstanceProfile": template["Resources"]["Ec2ListenerprivateSubnet"]["Properties"]["IamInstanceProfile"],
    }
    assert actual == expected


def test_every_probe_lambda_in_a_stack_shares_one_execution_role(mock_aws_infra: dict[str, str]) -> None:
    # arrange
    config = make_cats_config(
        network_targets={
            "appTier": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]},
            "dataTier": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"]]},
        },
        enabled_network_handlers=["ec2", "lambda"],
    )

    # act
    template = load_cfn_template(_render(config))
    resources = template["Resources"]

    # assert - no per-target LambdaRole{target} survives
    surviving_per_target_roles: list[str] = []
    expected = ("CATS-test-infra-testaccount-lambda", [{"GetAtt": "CatsLambdaRole.Arn"}] * 2, surviving_per_target_roles)
    actual = (
        resources["CatsLambdaRole"]["Properties"]["RoleName"],
        [resources[f"ProbeLambda{target}"]["Properties"]["Role"] for target in ("appTier", "dataTier")],
        [name for name in resources if name.startswith("LambdaRole")],
    )
    assert actual == expected


def test_test_role_gets_one_invoke_statement_naming_every_probe_lambda(mock_aws_infra: dict[str, str]) -> None:
    # arrange
    config = make_cats_config(
        network_targets={
            "appTier": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]},
            "dataTier": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"]]},
        },
        enabled_network_handlers=["ec2", "lambda"],
    )

    # act
    statement = _test_role_statements(load_cfn_template(_render(config)))["LambdaInvoke"]

    # assert
    expected = {
        "Sid": "LambdaInvoke",
        "Effect": "Allow",
        "Action": "lambda:InvokeFunction",
        "Resource": [{"GetAtt": "ProbeLambdaappTier.Arn"}, {"GetAtt": "ProbeLambdadataTier.Arn"}],
    }
    assert statement == expected


def test_ec2_only_stack_renders_no_lambda_role_or_invoke_policy(scenarios: dict[str, CatsConfig]) -> None:
    # act
    template = load_cfn_template(_render(scenarios["ec2_handler"]))

    # assert
    expected: list[str] = []
    assert [name for name in template["Resources"] if "Lambda" in name] == expected


def test_every_probe_lambda_in_a_stack_logs_to_one_stack_owned_group_that_expires(mock_aws_infra: dict[str, str]) -> None:
    """Without a declared group Lambda creates one per function on first invoke, outside the stack: never
    expiring, and left behind by `cats infra destroy`. The stack owns one group, shared like the execution role."""
    # arrange
    config = make_cats_config(
        network_targets={
            "appTier": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]},
            "dataTier": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"]]},
        },
        enabled_network_handlers=["ec2", "lambda"],
    )

    # act
    resources = load_cfn_template(_render(config))["Resources"]

    # assert
    expected = (
        {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/aws/lambda/CATS-test-infra-testaccount-probe", "RetentionInDays": 7}},
        [{"LogGroup": {"Ref": "CatsProbeLogGroup"}}] * 2,
    )
    actual = (
        resources["CatsProbeLogGroup"],
        [resources[f"ProbeLambda{target}"]["Properties"]["LoggingConfig"] for target in ("appTier", "dataTier")],
    )
    assert actual == expected


def test_ec2_only_stack_renders_no_probe_log_group(scenarios: dict[str, CatsConfig]) -> None:
    # act
    template = load_cfn_template(_render(scenarios["ec2_handler"]))

    # assert
    expected: list[str] = []
    assert [name for name in template["Resources"] if template["Resources"][name]["Type"] == "AWS::Logs::LogGroup"] == expected


# =============================================================================
# Template size: the measured boundary, the offline estimate, and the cap it replaces
# =============================================================================


def _size_config(mock_aws_infra: dict[str, str], targets: int, handlers: list[str], tcp: int, udp: int, subnets: int = 1) -> CatsConfig:
    """A config of `targets` identical targets, each with `tcp` + `udp` listen ports over `subnets` subnets."""
    subnet_ids = [mock_aws_infra["SUBNET_A"], mock_aws_infra["SUBNET_B"]][:subnets]
    network_targets = {
        f"target{i}": {
            "vpc_id": mock_aws_infra["VPC"],
            "subnet_ids": subnet_ids,
            "network_settings": {"tcp_listen_ports": [8000 + p for p in range(tcp)], "udp_listen_ports": [9000 + p for p in range(udp)]},
        }
        for i in range(targets)
    }
    return make_cats_config(network_targets=network_targets, enabled_network_handlers=handlers)


@pytest.mark.parametrize(
    ("targets", "handlers", "tcp", "udp", "fits"),
    [
        pytest.param(4, ["ec2", "lambda"], 1, 1, True, id="4 both-handler targets, 1+1 ports: fits"),
        pytest.param(13, ["ec2"], 1, 1, True, id="13 ec2 targets, 1+1 ports: fits (table row)"),
        pytest.param(14, ["ec2"], 1, 1, False, id="14 ec2 targets, 1+1 ports: does not fit"),
        pytest.param(7, ["ec2"], 5, 5, True, id="7 ec2 targets, 5+5 ports: fits (table row)"),
        pytest.param(8, ["ec2"], 5, 5, False, id="8 ec2 targets, 5+5 ports: does not fit"),
        pytest.param(5, ["ec2"], 10, 10, True, id="5 ec2 targets, 10+10 ports: fits (table row)"),
        pytest.param(6, ["ec2"], 10, 10, False, id="6 ec2 targets, 10+10 ports: does not fit"),
        pytest.param(4, ["ec2"], 30, 0, True, id="4 ec2 targets at the 30-port cap: fits (table row)"),
        pytest.param(5, ["ec2"], 30, 0, False, id="5 ec2 targets at the 30-port cap: does not fit"),
        pytest.param(8, ["ec2", "lambda"], 1, 1, True, id="8 both-handler targets, 1+1 ports: fits (table row)"),
        pytest.param(9, ["ec2", "lambda"], 1, 1, False, id="9 both-handler targets, 1+1 ports: does not fit"),
        pytest.param(5, ["ec2", "lambda"], 5, 5, True, id="5 both-handler targets, 5+5 ports: fits (table row)"),
        pytest.param(6, ["ec2", "lambda"], 5, 5, False, id="6 both-handler targets, 5+5 ports: does not fit"),
        pytest.param(4, ["ec2", "lambda"], 10, 10, True, id="4 both-handler targets, 10+10 ports: fits (table row)"),
        pytest.param(5, ["ec2", "lambda"], 10, 10, False, id="5 both-handler targets, 10+10 ports: does not fit"),
        pytest.param(3, ["ec2", "lambda"], 30, 0, True, id="3 both-handler targets at the 30-port cap: fits (table row)"),
        pytest.param(4, ["ec2", "lambda"], 30, 0, False, id="4 both-handler targets at the 30-port cap: does not fit"),
    ],
)
def test_template_size_boundary(mock_aws_infra: dict[str, str], targets: int, handlers: list[str], tcp: int, udp: int, fits: bool) -> None:
    """The boundary docs/guides/infrastructure.md states: every row of its table, and the shape one target past each.

    Every row is a config `cats validate` accepts before the size gate. Per-stack content (the
    listener user data, the probe, the instance and Lambda roles, the probe log group) is embedded
    once, so the cost of a target is its listener, security groups, probe function and outputs,
    plus two ingress rules per port. The rows sit a few hundred bytes under the limit, so anything
    added per target or per rule moves the table: measure before adding.
    """
    # act
    size = len(_render(_size_config(mock_aws_infra, targets, handlers, tcp, udp)).encode("utf-8"))

    # assert
    assert (size <= CFN_TEMPLATE_BODY_LIMIT) is fits, f"{size:,} bytes against {CFN_TEMPLATE_BODY_LIMIT:,}"


@pytest.mark.parametrize(
    ("targets", "handlers", "expected_outputs"),
    [
        pytest.param(13, ["ec2"], 68, id="13 ec2 targets: 3 base + 5 per target"),
        pytest.param(8, ["ec2", "lambda"], 51, id="8 both-handler targets: 3 base + 6 per target"),
    ],
)
def test_byte_gate_binds_before_the_output_limit(mock_aws_infra: dict[str, str], targets: int, handlers: list[str], expected_outputs: int) -> None:
    """CloudFormation also caps a stack at 200 outputs; the byte gate is the only limit a user can reach.

    These are the largest shapes the boundary table admits (one more target of either kind does not
    fit). Their output counts are well under 200, so no configuration passes the byte gate and then
    fails on outputs. The exact counts pin the per-target output contract as well: 5 outputs for an
    EC2 target, one more for the probe function name when the Lambda handler is enabled.
    """
    # act
    template = load_cfn_template(_render(_size_config(mock_aws_infra, targets, handlers, 1, 1)))

    # assert
    assert len(template["Outputs"]) == expected_outputs


@pytest.mark.parametrize("reporting_enabled", [True, False], ids=["reporting enabled", "reporting disabled"])
def test_estimate_equals_the_resolved_render_for_literal_ids(mock_aws_infra: dict[str, str], reporting_enabled: bool) -> None:
    """With literal IDs in cats.yaml the offline estimate is not a bound but the exact deploy-time size.

    The ``securityhub`` format is configured either way; only ``reporting.enabled`` varies. The deploy
    render grants the Security Hub import only when reporting is on, and the estimate must agree.
    """
    # arrange
    config = _size_config(mock_aws_infra, 3, ["ec2", "lambda"], 2, 1, subnets=2)
    config = config.model_copy(
        update={
            "reporting": config.reporting.model_copy(
                update={
                    "enabled": reporting_enabled,
                    "report_formats": [ReportFormat.CATS_JSON, ReportFormat.SECURITYHUB],
                    "securityhub": config.reporting.securityhub.model_copy(update={"aws_region": "eu-west-1"}),
                }
            )
        }
    )

    # act
    estimate = estimate_template_size(config, ACCOUNT_NAME)
    rendered = len(_render(config).encode("utf-8"))

    # assert
    assert estimate == rendered


def test_estimate_equals_the_resolved_render_with_endpoint_security_groups(mock_aws_infra: dict[str, str]) -> None:
    """The placeholder resolver carries the endpoint groups too, so cats validate measures the SecurityGroupIds the deploy renders."""
    # arrange
    config = _both_endpoint_types_config(mock_aws_infra, [mock_aws_infra["SG"]])

    # act
    estimate = estimate_template_size(config, ACCOUNT_NAME)
    rendered = len(_render(config).encode("utf-8"))

    # assert
    assert estimate == rendered


def test_estimate_bounds_the_resolved_render_for_ssm_paths(mock_aws_infra: dict[str, str]) -> None:
    """An SSM path resolves to an ID of at most 17 hex characters, which is what the estimate assumes."""
    # arrange - moto's IDs are 17 hex characters; the estimate must be at least the real render
    config = make_cats_config(
        network_targets={"privateSubnet": {"vpc_id": "/cats/vpc", "subnet_ids": ["/cats/subnet"], "network_settings": {"tcp_listen_ports": [8080]}}},
        enabled_network_handlers=["ec2", "lambda"],
    )
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/cats/vpc", Value=mock_aws_infra["VPC"], Type="String")
    ssm.put_parameter(Name="/cats/subnet", Value=mock_aws_infra["SUBNET_A"], Type="String")

    # act
    estimate = estimate_template_size(config, ACCOUNT_NAME)
    rendered = len(_render(config).encode("utf-8"))

    # assert
    assert estimate >= rendered


def test_estimate_needs_no_deploy_region(mock_aws_infra: dict[str, str]) -> None:
    """cats validate runs without a Region; the estimate measures an unset one at the widest Region name."""
    # arrange
    config = _size_config(mock_aws_infra, 1, ["ec2"], 1, 0)
    config.cats_deployment_settings.aws_region = None

    # act
    estimate = estimate_template_size(config, ACCOUNT_NAME)

    # assert
    assert estimate > 0


def test_estimate_with_securityhub_inheriting_an_unset_deploy_region_counts_the_grant(mock_aws_infra: dict[str, str]) -> None:
    """The securityhub format with no Region of its own inherits the deploy Region, which validate may leave unset.

    ``effective_securityhub_region()`` refuses that on the real config (deploy needs a Region); the
    estimate reads it against the placeholder Region instead, so the grant is measured, not skipped.
    The oracle is a direct render with the placeholder Region and the grant present.
    """
    # arrange
    config = _size_config(mock_aws_infra, 1, ["ec2"], 1, 0)
    config = config.model_copy(
        update={
            "cats_deployment_settings": config.cats_deployment_settings.model_copy(update={"aws_region": None}),
            "reporting": config.reporting.model_copy(update={"report_formats": [ReportFormat.CATS_JSON, ReportFormat.SECURITYHUB]}),
        }
    )
    account = config.accounts_under_test[ACCOUNT_NAME]
    expected = template_size(
        render_template(
            placeholder_resolve_account(ACCOUNT_NAME, account),
            config.cats_deployment_settings.model_copy(update={"aws_region": _PLACEHOLDER_REGION}),
            resolve_account_settings(account),
            securityhub_region=_PLACEHOLDER_REGION,
        )
    )

    # act
    estimate = estimate_template_size(config, ACCOUNT_NAME)

    # assert
    assert estimate == expected


def test_estimate_without_parent_account_id_equals_the_estimate_with_one(mock_aws_infra: dict[str, str]) -> None:
    """cats validate runs without a parent account; every account ID is 12 digits, so the placeholder measures the exact size."""
    # arrange
    config = _size_config(mock_aws_infra, 2, ["ec2", "lambda"], 1, 1)
    with_parent = estimate_template_size(config, ACCOUNT_NAME)
    config.cats_deployment_settings.parent_account_id = None

    # act
    without_parent = estimate_template_size(config, ACCOUNT_NAME)

    # assert
    assert without_parent == with_parent


def test_reject_oversized_template_message_names_the_account_and_the_levers() -> None:
    # act
    with pytest.raises(CatsConfigError) as excinfo:
        reject_oversized_template("prod", CFN_TEMPLATE_BODY_LIMIT + 1)

    # assert
    expected = (
        "Template for 'prod' renders to 51,201 bytes; CloudFormation's TemplateBody limit is 51,200. "
        + "Reduce network targets, listen ports or enabled network handlers in this account, "
        + "or split its targets across a second account entry with the same account_id."
    )
    assert str(excinfo.value) == expected


def test_reject_oversized_template_accepts_the_limit_exactly() -> None:
    # act - a template of exactly the limit is accepted, so nothing is raised
    reject_oversized_template("prod", CFN_TEMPLATE_BODY_LIMIT)


def test_five_network_targets_validate_when_the_template_fits(mock_aws_infra: dict[str, str]) -> None:
    """The former fixed cap of four targets is gone: the rendered size is the only bound."""
    # arrange
    network_targets = {f"target{i}": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]} for i in range(5)}

    # act
    config = CatsConfig.model_validate(make_config(network_targets=network_targets))

    # assert
    assert sorted(config.accounts_under_test[ACCOUNT_NAME].account_network_under_test) == [f"target{i}" for i in range(5)]


def _account_wide_names(template: dict[str, Any]) -> list[str]:
    """Every name the stack fixes that must be unique within an account: IAM role names, Lambda function names, SSM document names."""
    names: list[str] = []
    for resource in template["Resources"].values():
        properties = resource["Properties"]
        if resource["Type"] == "AWS::SSM::Document":
            names.append(properties["Name"])
        for key in ("RoleName", "FunctionName"):
            if key in properties:
                names.append(properties[key])
    return sorted(names)


def test_two_entries_with_the_same_account_id_render_stacks_with_distinct_account_wide_names(mock_aws_infra: dict[str, str]) -> None:
    """The escape hatch the oversized message names: a second keyname for the same account is a second stack, and nothing the stacks name collides."""
    # arrange
    config = CatsConfig.model_validate(
        make_config(
            accounts={"prod_vpc1": "111111111111", "prod_vpc2": "111111111111"},
            network_targets={"web": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
            enabled_network_handlers=["ec2", "lambda"],
        )
    )
    session = boto3.Session(region_name="us-east-1")

    # act
    names: dict[str, list[str]] = {}
    for name, account in config.accounts_under_test.items():
        resolved = resolve_account(name, account, session)
        template = render_template(resolved, config.cats_deployment_settings, resolve_account_settings(account), securityhub_region=None)
        names[name] = _account_wide_names(load_cfn_template(template))

    # assert
    expected = {
        "prod_vpc1": [
            "CATS-test-infra-prod_vpc1-Probe",
            "CATS-test-infra-prod_vpc1-default-test-role",
            "CATS-test-infra-prod_vpc1-ec2",
            "CATS-test-infra-prod_vpc1-lambda",
            "CATS-test-infra-prod_vpc1-probe-web",
        ],
        "prod_vpc2": [
            "CATS-test-infra-prod_vpc2-Probe",
            "CATS-test-infra-prod_vpc2-default-test-role",
            "CATS-test-infra-prod_vpc2-ec2",
            "CATS-test-infra-prod_vpc2-lambda",
            "CATS-test-infra-prod_vpc2-probe-web",
        ],
    }
    assert names == expected


# =============================================================================
# allow_shell permutation tests
# =============================================================================


def _render_shell_scenario(mock_aws_infra: dict[str, str], handlers: list[str], allow_shell: bool) -> str:
    """Render a single-target config with the given handlers and allow_shell flag."""
    config = make_cats_config(
        network_targets={
            "privateSubnet": {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
            }
        },
        enabled_network_handlers=handlers,
    )
    account = config.accounts_under_test[ACCOUNT_NAME]
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    account_settings = ResolvedAccountSettings(
        additional_roles=account.account_settings.additional_roles,
        default_test_role_managed_policies=account.account_settings.default_test_role_managed_policies,
        default_test_role_inline_policies=account.account_settings.default_test_role_inline_policies,
        fixture_scope=account.account_settings.fixture_scope,
        allow_shell=allow_shell,
    )
    return render_template(resolved, config.cats_deployment_settings, account_settings, securityhub_region=config.effective_securityhub_region())


PROBE_DOCUMENT_ARN = {"Sub": "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:document/CATS-test-infra-testaccount-Probe"}
RUN_SHELL_SCRIPT_ARN = {"Sub": "arn:aws:ssm:${AWS::Region}::document/AWS-RunShellScript"}


def _send_command_document_arns(template: dict[str, Any]) -> list[dict[str, str]] | None:
    """The document ARNs the test role may SendCommand, or None when the statement is not rendered."""
    statement = _test_role_statements(template).get("SsmSendCommandDocuments")
    if statement is None:
        return None
    documents: list[dict[str, str]] = statement["Resource"]
    return documents


def test_template_allow_shell_adds_document_arn(mock_aws_infra: dict[str, str]) -> None:
    # act
    template = load_cfn_template(_render_shell_scenario(mock_aws_infra, ["ec2"], allow_shell=True))

    # assert
    assert _send_command_document_arns(template) == [PROBE_DOCUMENT_ARN, RUN_SHELL_SCRIPT_ARN]


def test_template_allow_shell_false_no_document_arn(mock_aws_infra: dict[str, str]) -> None:
    # act
    template = load_cfn_template(_render_shell_scenario(mock_aws_infra, ["ec2"], allow_shell=False))

    # assert
    assert _send_command_document_arns(template) == [PROBE_DOCUMENT_ARN]


def test_template_lambda_only_with_allow_shell_emits_policy(mock_aws_infra: dict[str, str]) -> None:
    # act
    template = load_cfn_template(_render_shell_scenario(mock_aws_infra, ["lambda"], allow_shell=True))

    # assert - the shell grant alone; the CATS probe document is not rendered when only lambda is enabled
    assert (_send_command_document_arns(template), "CatsProbeDocument" in template["Resources"]) == ([RUN_SHELL_SCRIPT_ARN], False)


def test_template_lambda_only_no_shell_no_policy(mock_aws_infra: dict[str, str]) -> None:
    # act
    template = load_cfn_template(_render_shell_scenario(mock_aws_infra, ["lambda"], allow_shell=False))

    # assert
    assert _send_command_document_arns(template) is None


# =============================================================================
# The instance role grants SSM Agent what it needs and nothing else; SendCommand stays inside the stack
# =============================================================================


def test_instance_role_carries_no_managed_policy(scenarios: dict[str, CatsConfig]) -> None:
    """AmazonSSMManagedInstanceCore also grants ssm:GetParameter(s) and inventory/compliance writes; a shell on the listener must not inherit them."""
    # act
    template = yaml.safe_load(_render(scenarios["single_target"]))

    # assert
    assert "ManagedPolicyArns" not in template["Resources"]["CatsEc2Role"]["Properties"]


def test_instance_role_ssm_agent_policy_is_exactly_the_agent_channel_actions(scenarios: dict[str, CatsConfig]) -> None:
    # act
    template = yaml.safe_load(_render(scenarios["single_target"]))
    policies = {p["PolicyName"]: p["PolicyDocument"] for p in template["Resources"]["CatsEc2Role"]["Properties"]["Policies"]}

    # assert
    expected = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ssm:UpdateInstanceInformation",
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                    "ec2messages:AcknowledgeMessage",
                    "ec2messages:DeleteMessage",
                    "ec2messages:FailMessage",
                    "ec2messages:GetEndpoint",
                    "ec2messages:GetMessages",
                    "ec2messages:SendReply",
                ],
                "Resource": "*",
            }
        ],
    }
    assert (sorted(policies), policies["SsmAgent"]) == (["S3WriteAccess", "SsmAgent"], expected)


def test_instance_role_trusts_the_partition_ec2_principal(scenarios: dict[str, CatsConfig]) -> None:
    """ec2.amazonaws.com is wrong in aws-cn; URLSuffix resolves per partition."""
    # act
    template = load_cfn_template(_render(scenarios["single_target"]))
    principal = template["Resources"]["CatsEc2Role"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]

    # assert
    assert principal == {"Service": {"Sub": "ec2.${AWS::URLSuffix}"}}


def test_listener_is_tagged_with_its_stack_name(scenarios: dict[str, CatsConfig]) -> None:
    # act
    template = load_cfn_template(_render(scenarios["single_target"]))

    # assert
    expected = [{"Key": "cats:managed", "Value": "true"}, {"Key": "cats:stack", "Value": {"Ref": "AWS::StackName"}}]
    assert template["Resources"]["Ec2ListenerprivateSubnet"]["Properties"]["Tags"] == expected


def test_listener_never_gets_a_public_address(scenarios: dict[str, CatsConfig], mock_aws_infra: dict[str, str]) -> None:
    """The listener's group admits every source on its ports; in a subnet with MapPublicIpOnLaunch the instance would
    otherwise be reachable from the internet. The interface is declared so the address can be pinned off."""
    # act
    template = load_cfn_template(_render(scenarios["single_target"]))
    properties = template["Resources"]["Ec2ListenerprivateSubnet"]["Properties"]

    # assert - the interface carries the subnet and group, so neither appears at the top level
    expected_interfaces = [
        {"DeviceIndex": "0", "AssociatePublicIpAddress": False, "SubnetId": mock_aws_infra["SUBNET_A"], "GroupSet": [{"Ref": "Ec2SGprivateSubnet"}]}
    ]
    expected_keys = {
        "InstanceType",
        "ImageId",
        "NetworkInterfaces",
        "IamInstanceProfile",
        "MetadataOptions",
        "BlockDeviceMappings",
        "Tags",
        "UserData",
    }
    assert (properties["NetworkInterfaces"], set(properties)) == (expected_interfaces, expected_keys)


def test_listener_with_user_supplied_groups_still_gets_no_public_address(scenarios: dict[str, CatsConfig], mock_aws_infra: dict[str, str]) -> None:
    """existing_security_group_ids replaces the group, not the interface declaration."""
    # act
    template = load_cfn_template(_render(scenarios["existing_security_groups"]))
    interfaces = template["Resources"]["Ec2ListenerprivateSubnet"]["Properties"]["NetworkInterfaces"]

    # assert
    expected = [{"DeviceIndex": "0", "AssociatePublicIpAddress": False, "SubnetId": mock_aws_infra["SUBNET_A"], "GroupSet": [mock_aws_infra["SG"]]}]
    assert interfaces == expected


def test_send_command_on_instances_is_scoped_to_this_stack(scenarios: dict[str, CatsConfig]) -> None:
    """Two CATS stacks in one account share cats:managed; the grant must not let one reach the other's listeners."""
    # act
    statement = _test_role_statements(load_cfn_template(_render(scenarios["single_target"])))["SsmSendCommandInstances"]

    # assert
    expected = {
        "Sid": "SsmSendCommandInstances",
        "Effect": "Allow",
        "Action": "ssm:SendCommand",
        "Resource": {"Sub": "arn:aws:ec2:${AWS::Region}:${AWS::AccountId}:instance/*"},
        "Condition": {"StringEquals": {"ssm:resourceTag/cats:stack": {"Ref": "AWS::StackName"}}},
    }
    assert statement == expected


def _iam_policies(template: str) -> dict[str, Any]:
    """The AWS::IAM::Policy resources of a rendered template, by logical id."""
    resources: dict[str, Any] = yaml.safe_load(template.replace("!Sub ", "").replace("!Ref ", ""))["Resources"]
    return {name: resource for name, resource in resources.items() if resource["Type"] == "AWS::IAM::Policy"}


def _test_role_statements(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The statements of CatsTestRolePolicy, the one CATS-owned policy document on the test role, by Sid."""
    statements: list[dict[str, Any]] = template["Resources"]["CatsTestRolePolicy"]["Properties"]["PolicyDocument"]["Statement"]
    return {statement["Sid"]: statement for statement in statements}


def test_test_role_has_exactly_one_cats_owned_policy_resource(mock_aws_infra: dict[str, str]) -> None:
    """Every CATS grant on the test role is one statement of one document: the deploy readiness gate
    proves that document live with a single DescribeStacks probe, which only holds if nothing CATS
    grants the test role lives in another PutRolePolicy. The richest configuration renders them all."""
    # arrange
    config = CatsConfig.model_validate(
        make_config(
            network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
            enabled_network_handlers=["ec2", "lambda"],
            account_settings={"allow_shell": True},
            reporting={"report_formats": ["cats_json", "securityhub"], "securityhub": {"aws_region": "eu-west-1"}},
        )
    )

    # act
    policies = _iam_policies(_render(config))
    on_test_role = {name for name, policy in policies.items() if policy["Properties"]["Roles"] == ["CatsTestRole"]}
    sids = [statement["Sid"] for statement in policies["CatsTestRolePolicy"]["Properties"]["PolicyDocument"]["Statement"]]

    # assert
    expected_sids = [
        "CfnDescribeStacks",
        "S3ReadResponses",
        "SsmSendCommandDocuments",
        "SsmSendCommandInstances",
        "SsmReadCommands",
        "LambdaInvoke",
        "SecurityHubImportFindings",
    ]
    assert (on_test_role, sids) == ({"CatsTestRolePolicy"}, expected_sids)


def test_securityhub_format_grants_import_on_the_accounts_product_in_the_configured_region(mock_aws_infra: dict[str, str]) -> None:
    """One action, one resource: this account's default product ARN in the securityhub Region, not the deploy Region."""
    # arrange
    config = CatsConfig.model_validate(
        make_config(network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}})
        | {"reporting": {"report_formats": ["cats_json", "securityhub"], "securityhub": {"aws_region": "eu-west-1"}}}
    )

    # act
    statement = _test_role_statements(load_cfn_template(_render(config)))["SecurityHubImportFindings"]

    # assert
    expected = {
        "Sid": "SecurityHubImportFindings",
        "Effect": "Allow",
        "Action": "securityhub:BatchImportFindings",
        "Resource": {"Sub": "arn:aws:securityhub:eu-west-1:${AWS::AccountId}:product/${AWS::AccountId}/default"},
    }
    assert expected == statement


def test_securityhub_region_defaults_to_the_deploy_region(mock_aws_infra: dict[str, str]) -> None:
    # arrange
    config = CatsConfig.model_validate(
        make_config(
            network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
            aws_region="eu-central-1",
        )
        | {"reporting": {"report_formats": ["securityhub"]}}
    )

    # act
    statement = _test_role_statements(load_cfn_template(_render(config)))["SecurityHubImportFindings"]

    # assert
    expected = {"Sub": "arn:aws:securityhub:eu-central-1:${AWS::AccountId}:product/${AWS::AccountId}/default"}
    assert expected == statement["Resource"]


def test_without_securityhub_format_the_test_role_has_no_securityhub_statement(mock_aws_infra: dict[str, str]) -> None:
    """A securityhub block alone is inert: the grant follows the format, and the statement set is exactly today's."""
    # arrange
    config = CatsConfig.model_validate(
        make_config(network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}})
        | {"reporting": {"securityhub": {"aws_region": "eu-west-1"}}}
    )

    # act
    statements = _test_role_statements(load_cfn_template(_render(config)))

    # assert
    expected = ["CfnDescribeStacks", "S3ReadResponses", "SsmSendCommandDocuments", "SsmSendCommandInstances", "SsmReadCommands"]
    assert expected == list(statements)


def test_managed_policy_full_arn_renders_without_prefix(cats_yaml: Path, mock_aws_infra: dict[str, str]) -> None:
    """A full policy ARN attaches as-is; only bare names get the aws:policy/ prefix."""
    # arrange
    custom_arn = "arn:aws:iam::123456789012:policy/MyCustomPolicy"
    config = CatsConfig.model_validate(
        make_config(
            network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
            additional_roles={"custom": {"managed_policies": [custom_arn, "ReadOnlyAccess"]}},
        )
    )
    write_config(cats_yaml, config.model_dump(mode="json"))

    # act
    result = _render(config)

    # assert
    lines = result.splitlines()
    expected_arn_line = f"        - {custom_arn}"
    expected_name_line = "        - arn:aws:iam::aws:policy/ReadOnlyAccess"
    assert (expected_arn_line in lines, expected_name_line in lines) == (True, True)
