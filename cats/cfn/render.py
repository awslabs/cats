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

"""Render CloudFormation templates from Jinja2."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from cats.cfn.resolve import ResolvedAccount, ResolvedAccountSettings, placeholder_resolve_account, resolve_account_settings
from cats.config import CatsConfig, CatsDeploymentSettings, GatewayEndpoints, partition_dns_suffix
from cats.constants import LISTENER_FAILED_MARKER, LISTENER_READY_MARKER
from cats.exceptions import CatsConfigError


class VpcEndpointData(TypedDict):
    """VPC endpoint configuration for CFN template rendering."""

    vpc_id: str
    subnet_ids: list[str]
    route_table_ids: list[str]
    gateway_endpoints: list[GatewayEndpoints]
    interface_endpoints: list[str]
    interface_endpoint_security_group_ids: list[str] | None


# The settings that shape a VPC's endpoints. The endpoints are rendered once per VPC, so targets
# sharing a VPC must agree on every one of these; `render_template` refuses a config where they differ.
_PER_VPC_ENDPOINT_SETTINGS: tuple[Literal["gateway_endpoints", "interface_endpoints", "interface_endpoint_security_group_ids"], ...] = (
    "gateway_endpoints",
    "interface_endpoints",
    "interface_endpoint_security_group_ids",
)


class NetworkTargetData(TypedDict):
    """Network target configuration for CFN template rendering."""

    name: str
    vpc_id: str
    subnet_ids: list[str]
    route_table_ids: list[str]
    existing_security_group_ids: list[str] | None
    enabled_handlers: list[str]
    ec2_instance_type: str
    existing_ec2_instance_profile_name: str | None
    ami_id: str
    gateway_endpoints: list[GatewayEndpoints]
    interface_endpoints: list[str]
    interface_endpoint_security_group_ids: list[str] | None
    tcp_listen_ports: list[int]
    udp_listen_ports: list[int]


# The per-file license header, as it appears in the ec2_server source files.
# Rendered templates, userdata, and instance files are dynamically created artifacts:
# the license header stays on the static source files and is stripped at embed time.
_FILE_LICENSE_HEADER = """# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
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
"""

# AWS::Lambda::Function Code.ZipFile inline source limit. probe.py is embedded
# verbatim (license header aside), so its source must stay under this.
LAMBDA_ZIPFILE_LIMIT = 4096

# CloudFormation's limit on a template passed inline as TemplateBody, which is how every CATS
# stack is created and updated. `estimate_template_size` measures a config against it without
# AWS; `cats validate` and `cats infra deploy` both refuse a config that renders past it.
CFN_TEMPLATE_BODY_LIMIT = 51200


def _without_license_header(text: str) -> str:
    """Drop the license header block (and its trailing blank line) for embedding."""
    return text.replace(_FILE_LICENSE_HEADER + "\n", "", 1)


def _embedded_file(name: str) -> str:
    """One ec2_server file as it is embedded: license header off, surrounding whitespace off."""
    return _without_license_header((Path(__file__).parent / "ec2_server" / name).read_text()).strip()


_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _jinja_environment() -> Environment:
    """The environment both templates render in. Strict: a misspelled variable fails the render, never renders empty."""
    # Security rationale: this renders CloudFormation YAML and a shell script, not HTML; HTML autoescaping would corrupt infrastructure values.
    # nosemgrep: python_escaping_rule-jinja2-autoescape-false  # noqa: ERA001
    return Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=False, undefined=StrictUndefined)  # noqa: S701  # nosec B701


@dataclass(frozen=True)
class ListenerCode:
    """The four files installed on every EC2 listener, license headers stripped, as ``userdata.sh.j2`` embeds them."""

    udpserver_py: str
    tcpserver_py: str
    tcpserver_service: str
    udpserver_service: str

    @classmethod
    def load(cls) -> "ListenerCode":
        """Read the four files from ``cats/cfn/ec2_server``."""
        return cls(
            udpserver_py=_embedded_file("udpserver.py"),
            tcpserver_py=_embedded_file("tcpserver.py"),
            tcpserver_service=_embedded_file("tcpserver.service"),
            udpserver_service=_embedded_file("udpserver.service"),
        )


def render_userdata(code: ListenerCode) -> str:
    """The script every EC2 listener runs at first boot, minus the per-listener prefix.

    ``userdata.sh.j2`` installs both server scripts and both systemd units, writes the env files the
    units read their arguments from, enables each unit whose port list is non-empty, then waits for
    every configured port to be bound and writes its verdict to the serial console: the
    ``LISTENER_READY_MARKER`` line, or the ``LISTENER_FAILED_MARKER`` line naming the unbound ports.
    ``cats infra deploy`` reads that verdict (cats/cfn/readiness.py). It is the same on every
    listener, so the stack template embeds it once, as ``Mappings.CatsEmbeddedCode.code.userdata``;
    each listener's ``UserData`` is a few lines setting ``CATS_TCP_PORTS``, ``CATS_UDP_PORTS`` and,
    for UDP, ``CATS_BUCKET`` and ``CATS_UDP_RECEIPT_TOKEN``, followed by ``${Userdata}``, which
    Fn::Sub replaces with this text.
    """
    return (
        _jinja_environment()
        .get_template("userdata.sh.j2")
        .render(code=code, ready_marker=LISTENER_READY_MARKER, failed_marker=LISTENER_FAILED_MARKER)
    )


def _managed_policy_arn(policy: str, partition: str) -> str:
    """A bare policy name becomes the AWS-managed policy ARN; a full ARN passes through."""
    if policy.startswith("arn:"):
        return policy
    return f"arn:{partition}:iam::aws:policy/{policy}"


def _with_managed_policy_arns(account_settings: ResolvedAccountSettings, partition: str) -> ResolvedAccountSettings:
    """Resolve every managed policy reference to a full ARN, so the template stays declarative."""
    return replace(
        account_settings,
        default_test_role_managed_policies=[_managed_policy_arn(p, partition) for p in account_settings.default_test_role_managed_policies],
        additional_roles={
            role_name: role.model_copy(update={"managed_policies": [_managed_policy_arn(p, partition) for p in role.managed_policies]})
            for role_name, role in account_settings.additional_roles.items()
        },
    )


def render_template(
    resolved: ResolvedAccount,
    deployment_settings: CatsDeploymentSettings,
    account_settings: ResolvedAccountSettings,
    *,
    securityhub_region: str | None,
) -> str:
    """Render CFN template from Jinja2.

    ``securityhub_region`` is ``CatsConfig.effective_securityhub_region()``: the Region whose
    Security Hub the ``securityhub`` report format imports into, or None when the format is not
    configured. When set, the test role gets ``securityhub:BatchImportFindings`` on this
    account's default product ARN in that Region and nothing else.
    """
    if deployment_settings.parent_account_id is None:
        raise CatsConfigError(
            "cats_deployment_settings.parent_account_id is not set. "
            + "Test roles in each account under test trust this account: "
            + "set it to the 12-digit ID of the account that runs your tests."
        )
    account_settings = _with_managed_policy_arns(account_settings, deployment_settings.partition)
    template = _jinja_environment().get_template("stack.yaml.j2")

    network_targets: list[NetworkTargetData] = []
    for name, t in resolved.network_targets.items():
        network_targets.append(
            {
                "name": name,
                "vpc_id": t.vpc_id,
                "subnet_ids": [s.subnet_id for s in t.subnets],
                "route_table_ids": list({s.route_table_id for s in t.subnets if s.route_table_id}),
                "existing_security_group_ids": t.existing_security_group_ids,
                "enabled_handlers": [h.value for h in t.settings.enabled_network_handlers],
                "ec2_instance_type": t.settings.ec2_instance_type,
                "existing_ec2_instance_profile_name": t.settings.existing_ec2_instance_profile_name,
                "ami_id": t.ami_id,
                "gateway_endpoints": t.settings.gateway_endpoints,
                "interface_endpoints": t.settings.interface_endpoints,
                "interface_endpoint_security_group_ids": t.interface_endpoint_security_group_ids,
                "tcp_listen_ports": t.settings.tcp_listen_ports,
                "udp_listen_ports": t.settings.udp_listen_ports,
            }
        )

    # Collect unique VPCs with their endpoint settings. Endpoints are created once per VPC, so every
    # target in a VPC must agree on the settings that shape them.
    vpc_endpoints: dict[str, VpcEndpointData] = {}
    for nt in network_targets:
        vpc_id: str = nt["vpc_id"]
        if vpc_id not in vpc_endpoints:
            vpc_endpoints[vpc_id] = {
                "vpc_id": vpc_id,
                "subnet_ids": nt["subnet_ids"],
                "route_table_ids": nt["route_table_ids"],
                "gateway_endpoints": nt["gateway_endpoints"],
                "interface_endpoints": nt["interface_endpoints"],
                "interface_endpoint_security_group_ids": nt["interface_endpoint_security_group_ids"],
            }
            continue
        existing = vpc_endpoints[vpc_id]
        for setting in _PER_VPC_ENDPOINT_SETTINGS:
            if existing[setting] != nt[setting]:
                raise CatsConfigError(
                    f"VPC {vpc_id} has conflicting {setting}: {existing[setting]} vs {nt[setting]}. "
                    + "All targets in the same VPC must have identical endpoint settings."
                )
    unique_vpcs = list(vpc_endpoints.values())

    return template.render(
        account_name=resolved.account_name,
        resource_prefix=f"{deployment_settings.cfn_stack_prefix}{resolved.account_name}",
        deployment_settings=deployment_settings,
        account_settings=account_settings,
        network_targets=network_targets,
        probe_code=_without_license_header((Path(__file__).parent / "ec2_server" / "probe.py").read_text()),
        userdata=render_userdata(ListenerCode.load()),
        unique_vpcs=unique_vpcs,
        securityhub_region=securityhub_region,
        endpoint_service_prefix=_endpoint_service_prefix(deployment_settings.partition),
    )


def template_size(template: str) -> int:
    """The size CloudFormation measures a TemplateBody by."""
    return len(template.encode("utf-8"))


# Stands in for an unset deploy Region. The Region appears only in VPC endpoint ServiceNames and the
# Security Hub product ARN, a few bytes per occurrence; 14 characters covers the longest names in use
# (ap-southeast-7, cn-northwest-1), and a longer one could not move a template across the limit.
_PLACEHOLDER_REGION = "x" * 14

# Stands in for an unset parent_account_id, which validation works without (only deploy and
# show-template require it: docs/guides/configuration.md). Every account ID is 12 digits and it
# appears only in the two trust-policy principals, so the measurement is exact, not a bound.
_PLACEHOLDER_PARENT_ACCOUNT_ID = "0" * 12


def estimate_template_size(config: CatsConfig, account_name: str) -> int:
    """The size of this account's template, measured without AWS.

    Renders the account from ``placeholder_resolve_account``, so the result equals the deploy-time
    render for a config of literal IDs and bounds it from above otherwise. Config loading works
    without a deploy Region or a parent account (deploy and show-template require them, validation
    does not), so an unset Region is measured at the widest Region name and an unset parent account
    at a 12-digit placeholder; ``render_template`` keeps refusing the latter for the commands that
    would deploy it. The Security Hub grant follows ``effective_securityhub_region()``, read against
    the placeholder settings, so the estimate and the deploy render decide it by the same rule.
    """
    account = config.accounts_under_test[account_name]
    deployment_settings = config.cats_deployment_settings
    placeholders: dict[str, str] = {}
    if deployment_settings.aws_region is None:
        placeholders["aws_region"] = _PLACEHOLDER_REGION
    if deployment_settings.parent_account_id is None:
        placeholders["parent_account_id"] = _PLACEHOLDER_PARENT_ACCOUNT_ID
    if len(placeholders) > 0:
        deployment_settings = deployment_settings.model_copy(update=placeholders)
        config = config.model_copy(update={"cats_deployment_settings": deployment_settings})
    template = render_template(
        resolved=placeholder_resolve_account(account_name, account),
        deployment_settings=deployment_settings,
        account_settings=resolve_account_settings(account),
        securityhub_region=config.effective_securityhub_region(),
    )
    return template_size(template)


def reject_oversized_template(account_name: str, size: int) -> None:
    """Raise CatsConfigError when a rendered template is too large to send as TemplateBody."""
    if size <= CFN_TEMPLATE_BODY_LIMIT:
        return
    raise CatsConfigError(
        f"Template for '{account_name}' renders to {size:,} bytes; CloudFormation's TemplateBody limit is "
        + f"{CFN_TEMPLATE_BODY_LIMIT:,}. Reduce network targets, listen ports or enabled network handlers in this account, "
        + "or split its targets across a second account entry with the same account_id."
    )


def _endpoint_service_prefix(partition: str) -> str:
    """The start of a VPC endpoint ServiceName in this partition, before the region and service.

    In every partition AWS documents it is the partition's DNS name with its labels reversed:
    com.amazonaws in aws and aws-us-gov, cn.com.amazonaws in aws-cn. AWS states the format per
    partition rather than promising this rule, so a partition that breaks it fails at stack
    creation with an invalid ServiceName, not silently.
    """
    labels = partition_dns_suffix(partition).split(".")
    labels.reverse()
    return ".".join(labels)
