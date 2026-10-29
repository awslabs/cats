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

import json
import logging
import os
import re
import threading
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from difflib import get_close_matches
from enum import StrEnum
from functools import cache
from importlib.resources.abc import Traversable
from keyword import iskeyword, kwlist
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, TypeAlias, TypeVar

import yaml
from botocore.loaders import create_loader
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, StrictBool, ValidationError, WithJsonSchema, field_validator, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource
from rich.console import Console
from typer import BadParameter

from cats.exceptions import CatsConfigError, CatsUsageError
from cats.filepath import get_cats_config_filepath, get_cats_config_override, get_default_output_dir, require_config_file, set_cats_config_filepath
from cats.utils.yaml_utils import dump_yaml, load_yaml
from cats.version import __version__

stdout_console = Console()
logger = logging.getLogger(__name__)


def click_is_valid_aws_account_id(account_id: str | None) -> str | None:
    if account_id is None:
        # Allows validating optionals
        return None
    # fullmatch and [0-9]: re.match with $ accepts a trailing newline and \d accepts non-ASCII digits.
    if re.fullmatch(r"[0-9]{12}", account_id):
        return account_id
    raise BadParameter("AWS Account ID must be a string of 12 numbers")


# AWS limits IAM RoleName to 64 characters and CloudFormation stack names to 128.
# The prefix is validated independently up to 64 characters; CatsConfig then validates
# every complete role name after account and target settings have been resolved.
# @cats keywords; any other keyword is a custom marker declared in reporting.custom_markers.
CATS_DECORATOR_KEYWORDS = frozenset({"standard_name", "control_ids", "control_group"})
# Marker names a custom marker may not take.
PYTEST_BUILTIN_MARKERS = frozenset({"skip", "skipif", "xfail", "parametrize", "usefixtures", "filterwarnings"})
CATS_MARKER_PREFIX = "cats_"
# The names test_selection.excluded[].marker accepts besides a declared custom marker: the two engine
# markers, each by the name written on the test (standard_name is the @cats keyword behind cats_standard;
# cats_requires is the decorator and the mark). Neither name is newly reserved: standard_name is a @cats
# keyword already, and the cats_ prefix is what keeps every engine mark clear of every custom marker.
TEST_SELECTION_MARKER_ALIASES = frozenset({"standard_name", "cats_requires"})

AWS_REGION_UNSET_MESSAGE = (
    "cats_deployment_settings.aws_region is not set. CATS deploys its stacks into and connects its test sessions "
    + "to that Region: set the key in cats.yaml, or export AWS_REGION."
)
MAX_IAM_ROLE_NAME_LENGTH = 64
MAX_LAMBDA_FUNCTION_NAME_LENGTH = 64
MAX_CFN_STACK_PREFIX_LENGTH = 64
MAX_ACCOUNT_KEYNAME_LENGTH = 25
MAX_NETWORK_TARGET_LENGTH = 15
# SSM SendCommand.TimeoutSeconds, which network_call_timeout_seconds is passed to, accepts 30..2,592,000.
MAX_SSM_COMMAND_TIMEOUT_SECONDS = 2_592_000

# SSM CreateDocument refuses names starting with these (and with AWSEC2, AWSConfigRemediation, AWSSupport,
# which all start with `aws`). Whether the service compares case-insensitively is undocumented; CATS does.
SSM_RESERVED_DOCUMENT_PREFIXES = ("aws", "amazon", "amzn")

# A CloudFormation stack name prefix; also the YAML injection guard for a value the template interpolates
CFN_STACK_PREFIX_PATTERN = r"^[A-Za-z][A-Za-z0-9-]*$"

# Alphanumeric only - used directly as CFN logical ID suffix, no transformation needed
CFN_LOGICAL_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9]*$"
# ASCII Python identifier with no leading `_`: account keynames and role names. Over ASCII on purpose:
# str.isidentifier() would also admit Unicode letters, which IAM RoleName rejects at deploy.
IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"

# The AccountUnderTest attributes a network target name would shadow: its __getattr__ only fires
# for missing attributes. Every other attribute contains `_`, which CFN_LOGICAL_ID_PATTERN forbids
# in a target name, so these five are the whole set.
RESERVED_NETWORK_TARGET_NAMES = frozenset({"name", "partition", "session", "role", "tags"})

# SessionHandler has one public name, `default`; every other attribute is underscore-private,
# so a role name may not start with `_` (see ValidRoleName).
RESERVED_ROLE_ATTRIBUTE_NAMES = frozenset({"default"})


@cache
def _partitions() -> list[dict[str, Any]]:
    """botocore's endpoint data for every partition, read once.

    Read through the data loader alone, never a botocore session: a session resolves its
    profile first, so a stale AWS_PROFILE would fail config loading with ProfileNotFound, and
    `cats validate` promises no AWS credentials are used. AWS_DATA_PATH is honored as the
    session would, for a custom model directory.
    """
    endpoints = create_loader(os.environ.get("AWS_DATA_PATH")).load_data("endpoints")
    partitions: list[dict[str, Any]] = endpoints["partitions"]
    return partitions


def _partition_dns_suffixes() -> dict[str, str]:
    """Each partition's DNS name (amazonaws.com, amazonaws.com.cn, c2s.ic.gov, ...)."""
    return {partition["partition"]: partition["dnsSuffix"] for partition in _partitions()}


def _known_partitions() -> frozenset[str]:
    """The partitions botocore's endpoint data knows about."""
    return frozenset(_partition_dns_suffixes())


def partition_dns_suffix(partition: str) -> str:
    """The DNS name a partition's endpoints live under. Config validation has already rejected a partition botocore does not know."""
    return _partition_dns_suffixes()[partition]


def partition_for_region(region: str) -> str | None:
    """The partition whose `regionRegex` matches the Region, or None when no partition claims it.

    botocore treats a Region no partition claims as `aws`; callers do the same rather than
    refusing a Region prefix that is newer than the installed botocore.
    """
    for partition in _partitions():
        if re.match(partition["regionRegex"], region) is not None:
            return str(partition["partition"])
    return None


def _is_known_partition(partition: str) -> str:
    known = _known_partitions()
    if partition not in known:
        raise ValueError(f"'{partition}' is not a known AWS partition. Known partitions: {', '.join(sorted(known))}")
    return partition


def _reject_ssm_reserved_prefix(prefix: str) -> str:
    """The probe SSM document is named `{prefix}{account}-Probe`; a reserved prefix fails the stack after roles and listeners exist."""
    for reserved in SSM_RESERVED_DOCUMENT_PREFIXES:
        if prefix.lower().startswith(reserved):
            raise ValueError(
                f"cfn_stack_prefix '{prefix}' starts with '{reserved}', which SSM reserves for AWS-owned document names, so the probe "
                + f"document '{prefix}<account>-Probe' could not be created. Choose a prefix that does not start with "
                + f"{', '.join(SSM_RESERVED_DOCUMENT_PREFIXES[:-1])} or {SSM_RESERVED_DOCUMENT_PREFIXES[-1]} (in any case)."
            )
    return prefix


# =============================================================================
# JSON schema extras: rules the runtime enforces in validators, stated for editors
# =============================================================================
#
# The schema is the editor's view of cats.yaml. pydantic emits every Field constraint, but an
# AfterValidator, a model_validator and the pattern of a dict key are invisible to it, so each
# is restated here in JSON Schema. tests/unit_tests/test_schema_matches_runtime.py pins that the
# schema and the runtime agree on every rule an editor can show.

# The shape pydantic's json_schema_extra takes (its own JsonDict alias is not exported).
JsonValue: TypeAlias = int | float | str | bool | list["JsonValue"] | dict[str, "JsonValue"] | None
JsonDict: TypeAlias = dict[str, JsonValue]


def _dict_key_schema(pattern: str, max_length: int | None = None, forbidden: frozenset[str] = frozenset()) -> JsonDict:
    """`propertyNames` for a dict field: pydantic emits the key pattern only as `patternProperties`, which does not reject a key that misses it."""
    names: JsonDict = {"pattern": pattern}
    if max_length is not None:
        names["maxLength"] = max_length
    if len(forbidden) > 0:
        names["not"] = {"enum": _json_list(forbidden)}
    return {"propertyNames": names}


def _json_list(names: frozenset[str]) -> list[JsonValue]:
    """A sorted enum list typed as JSON."""
    return [name for name in sorted(names)]


def _partition_schema(schema: JsonDict) -> None:
    """`partition` is one of botocore's partitions; read at schema generation, not at import."""
    schema["enum"] = _json_list(_known_partitions())


def _stack_prefix_schema(schema: JsonDict) -> None:
    """The runtime pattern plus a lookahead for the SSM-reserved words in any case (pydantic-core's regex has no lookahead; editors' do)."""
    reserved = "|".join("".join(f"[{letter.upper()}{letter.lower()}]" for letter in word) for word in SSM_RESERVED_DOCUMENT_PREFIXES)
    schema["pattern"] = rf"^(?!{reserved}){CFN_STACK_PREFIX_PATTERN.removeprefix('^')}"


def _deployment_settings_schema(schema: JsonDict, model: type[BaseModel]) -> None:
    """An explicit partition must agree with a Region some partition claims; a Region nobody claims is accepted, as at runtime."""
    every_region_pattern: list[JsonValue] = [{"pattern": partition["regionRegex"]} for partition in _partitions()]
    schema["allOf"] = [
        {
            "if": {"properties": {"partition": {"const": partition["partition"]}}, "required": ["partition"]},
            "then": {"properties": {"aws_region": {"anyOf": [{"pattern": partition["regionRegex"]}, {"not": {"anyOf": every_region_pattern}}]}}},
        }
        for partition in _partitions()
    ]


def _reject_case_only_collision(seen: set[str], name: str, what: str) -> None:
    """IAM role names are case-insensitive, so two names that differ only by case collide at deploy."""
    for other in seen:
        if other != name and other.lower() == name.lower():
            raise ValueError(
                f"{what} '{other}' and '{name}' differ only by case; IAM role names are case-insensitive, so they would collide at deploy."
            )


def _not_a_python_keyword(name: str) -> str:
    """A target name becomes `account.<name>`; a keyword there is a SyntaxError in every test."""
    if iskeyword(name):
        raise ValueError(f"network target name '{name}' is a Python keyword and cannot be an attribute of AccountUnderTest")
    return name


def _validate_role_name(name: str) -> str:
    """A role name becomes `account.role.<name>`: a keyword there is a SyntaxError, and `default` is already taken."""
    if iskeyword(name):
        raise ValueError(f"additional role name '{name}' is a Python keyword and cannot be an attribute of AccountUnderTest.role")
    if name in RESERVED_ROLE_ATTRIBUTE_NAMES:
        raise ValueError(
            f"additional role name '{name}' is reserved: it is an existing attribute of AccountUnderTest.role. "
            + f"Reserved names: {sorted(RESERVED_ROLE_ATTRIBUTE_NAMES)}"
        )
    return name


ValidNetworkTargetName = Annotated[
    str,
    Field(pattern=CFN_LOGICAL_ID_PATTERN, max_length=MAX_NETWORK_TARGET_LENGTH),
    AfterValidator(_not_a_python_keyword),
]
ValidAccountKeyname = Annotated[
    str,
    Field(pattern=IDENTIFIER_PATTERN, max_length=MAX_ACCOUNT_KEYNAME_LENGTH),
]
# Identifier-safe so `account.role.<name>` is an attribute; the template drops `_` for the CFN logical ID
ValidRoleName = Annotated[str, Field(pattern=IDENTIFIER_PATTERN), AfterValidator(_validate_role_name)]
# [0-9], not \d: the pattern also lands in the JSON schema, and \d admits non-ASCII digits in both engines.
ValidAwsAccountId = Annotated[str, Field(pattern=r"^[0-9]{12}$")]

# A network resource is named by its literal id or by an SSM parameter (a name starting with `/`) that holds
# the id, resolved in the target account at deploy time (cats.cfn.resolve). Anything else is a typo.
SSM_PARAMETER_NAME_PATTERN = r"/[a-zA-Z0-9_.\-/]+"
ValidVpcId = Annotated[str, Field(pattern=rf"^(vpc-[0-9a-f]+|{SSM_PARAMETER_NAME_PATTERN})$")]
ValidSubnetId = Annotated[str, Field(pattern=rf"^(subnet-[0-9a-f]+|{SSM_PARAMETER_NAME_PATTERN})$")]
ValidSecurityGroupId = Annotated[str, Field(pattern=rf"^(sg-[0-9a-f]+|{SSM_PARAMETER_NAME_PATTERN})$")]

# Each listen port renders two ingress rules (IPv4 and IPv6) on the listener security group; the default AWS quota is 60 rules.
MAX_COMBINED_LISTEN_PORTS = 30

ValidListenPort = Annotated[int, Field(ge=1, le=65535)]

T = TypeVar("T", bound=Hashable)


def _validate_unique_list(v: list[T]) -> list[T]:
    if len(v) != len(set(v)):
        raise PydanticCustomError("unique_list", "List must be unique")
    return v


UniqueList = Annotated[
    list[T],
    AfterValidator(_validate_unique_list),
    Field(json_schema_extra={"uniqueItems": True}),
]

# IAM names (roles, policies, instance profiles) are ASCII: https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreateRole.html
IAM_NAME_CHARACTER_CLASS = r"[A-Za-z0-9+=,.@_-]"
# A policy name with an optional path (SecurityAudit, job-function/ViewOnlyAccess)
IAM_POLICY_PATH_CHARACTER_CLASS = r"[A-Za-z0-9+=,.@_/-]"
IAM_ROLE_NAME_PATTERN = rf"^{IAM_NAME_CHARACTER_CLASS}{{1,{MAX_IAM_ROLE_NAME_LENGTH}}}$"
# IAM inline policy name constraints: https://docs.aws.amazon.com/IAM/latest/APIReference/API_PutRolePolicy.html
IAM_POLICY_NAME_PATTERN = rf"^{IAM_NAME_CHARACTER_CLASS}+$"
MAX_IAM_POLICY_NAME_LENGTH = 128
MANAGED_POLICY_ARN_PATTERN = rf"^arn:[a-z0-9-]+:iam::(aws|[0-9]{{12}}):policy/{IAM_POLICY_PATH_CHARACTER_CLASS}+$"
MANAGED_POLICY_NAME_PATTERN = rf"^{IAM_POLICY_PATH_CHARACTER_CLASS}+$"

ValidIamPolicyName = Annotated[
    str,
    Field(pattern=IAM_POLICY_NAME_PATTERN, max_length=MAX_IAM_POLICY_NAME_LENGTH),
]
ValidIamRoleName = Annotated[str, Field(pattern=IAM_ROLE_NAME_PATTERN)]


def _load_policy_document(value: object) -> dict[str, object]:
    """Validate an inline IAM policy document, loading string values as JSON files.

    String values are treated as paths to JSON policy files, resolved relative
    to the cats.yaml location so behavior does not depend on the invocation directory.

    Note: relative paths resolve against get_cats_config_filepath().parent, which reads
    CATS_CONFIG / the programmatic override. When validating models directly (tests,
    programmatic construction) with relative file references, that path must point at
    the intended cats.yaml or resolution will silently use the wrong base directory.
    """
    if isinstance(value, str):
        path = Path(value)
        if not path.is_absolute():
            path = get_cats_config_filepath().parent / path
        if not path.is_file():
            raise ValueError(f"policy file '{value}' not found at '{path}'")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"policy file '{path}' is not valid JSON: {e}") from e
    if not isinstance(value, dict):
        raise ValueError("policy document must be a JSON object, or a string path to a JSON file")
    if "Statement" not in value:
        raise ValueError("policy document must contain a 'Statement' key")
    return value


PolicyDocument = Annotated[
    dict[str, object],
    BeforeValidator(_load_policy_document),
    # The loader's rule for editors: a document (with a Statement) or a path to one.
    WithJsonSchema({"anyOf": [{"type": "object", "required": ["Statement"], "additionalProperties": True}, {"type": "string"}]}),
]


class CatsBaseModel(BaseModel):
    """
    Each configuration model inherits from this class, ensuring that only yaml keys
    represented in the config objects can be present in the file to parse successfully.

    String values are stripped of surrounding whitespace: a trailing space in a name that
    is interpolated into an ARN or a resource name would otherwise be kept silently.
    """

    model_config = SettingsConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _hint_unknown_keys(cls, data: object) -> object:
        """Name every unknown key with its nearest known key, so a typo (`region`) is fixed without opening the schema.

        Runs before field validation and sees the raw mapping; ``extra="forbid"`` stays as the
        backstop for anything that reaches it. A subclass's own before-validator runs first, so
        a removed key with a dedicated message (``junit_enabled``) keeps it.
        """
        if not isinstance(data, dict):
            return data
        unknown = [key for key in data if isinstance(key, str) and key not in cls.model_fields]
        if len(unknown) == 0:
            return data
        known_by_lowercase = {name.lower(): name for name in cls.model_fields}
        hints: list[str] = []
        for key in unknown:
            nearest = get_close_matches(key.lower(), list(known_by_lowercase), n=1, cutoff=0.6)
            hints.append(f"unknown key '{key}', did you mean '{known_by_lowercase[nearest[0]]}'?" if nearest else f"unknown key '{key}'")
        raise PydanticCustomError("unknown_key", "{hints}", {"hints": "; ".join(hints)})


class EnabledNetworkHandler(StrEnum):
    """Built-in network handlers that can be enabled."""

    LAMBDA = "lambda"
    EC2 = "ec2"


def _normalize_tags(v: dict[str, str | None] | list[str | dict[str, str]] | None) -> dict[str, str | None]:
    """Normalize tags from list format to dict.

    Accepts:
        - dict: {'key': 'value', 'flag': None}
        - list: ['flag', {'key': 'value'}]
    """
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, list) is False:
        raise ValueError(f"tags must be a mapping or a list of tag names and mappings, got {type(v).__name__} {v!r}")
    result: dict[str, str | None] = {}
    for item in v:
        if isinstance(item, str):
            result[item] = None
        elif isinstance(item, dict):
            for k, val in item.items():
                result[k] = val
        else:
            raise ValueError(f"tags list items must be a tag name or a {{key: value}} mapping, got {type(item).__name__} {item!r}")
    return result


def _tags_json_schema() -> dict[str, object]:
    """JSON schema that accepts both dict and list formats for tags."""
    return {
        "anyOf": [
            {"type": "object", "additionalProperties": {"type": ["string", "null"]}},
            {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "object", "additionalProperties": {"type": "string"}},
                    ]
                },
            },
        ],
        "default": {},
    }


# Type alias for tags field: runtime type is dict, but schema shows both formats
CatsTags = Annotated[dict[str, str | None], WithJsonSchema(_tags_json_schema())]


class GatewayEndpoints(StrEnum):
    """
    All currently available GatewayEndpoint types to enable using CATS in all VPCs under
    test.
    """

    S3 = "S3"
    DYNAMODB = "DYNAMODB"
    S3_EXPRESS = "S3_EXPRESS"

    @property
    def service_name(self) -> str:
        """The service segment of the endpoint's ServiceName: ``s3express`` for S3 Express One Zone, else the lowercase member."""
        return self.value.replace("_", "").lower()

    @property
    def logical_id_part(self) -> str:
        """A CloudFormation logical id segment: letters and digits only."""
        return self.value.replace("_", "")


# =============================================================================
# Settings Models (hierarchical: global -> account -> target)
# =============================================================================


class NetworkSettings(CatsBaseModel):
    """
    Network settings for CATS deployed infrastructure.

    Used at global, account, and target levels. Fields have sensible defaults.

    Inheritance: Settings inherit from parent levels (global → account → target).
    A field not explicitly set at a lower level inherits from the parent.
    To explicitly override with an empty value, set the field to an empty list [].
    Example: setting tcp_listen_ports: [] at account level disables ports even if
    global_network_settings has tcp_listen_ports: [8080].

    Attributes:
        enabled_network_handlers: Network handlers to deploy. First in list is used by http_call(), tcp_call(), udp_call().
        interface_endpoints: Interface VPC endpoints CATS creates in VPCs under test: an escape hatch for a VPC with no
            endpoint for the service and no DNS already answering for it. The create fails where the VPC has an endpoint
            or a private hosted zone for the name; where a Resolver rule already sends the name elsewhere the endpoint
            is created but never used. Where the organization's endpoints are reachable the EC2 handler needs none;
            where they are not, use the Lambda handler.
        gateway_endpoints: Gateway endpoints CATS creates in VPCs under test. S3 is what a UDP target's listener needs
            to write receipts where its subnet has no other path to S3.
        interface_endpoint_security_group_ids: Security groups for the interface endpoints CATS creates: security group
            ids (sg-...) or SSM parameter names (/...) holding one, resolved in the target account at deploy time.
            Unset, each interface endpoint is in its VPC's default security group, whose stock inbound rule admits
            only members of that group; the listener is in CATS's own group, so set this to a group admitting TCP 443
            from the listener's subnets unless the VPC's default group already does. Targets sharing a VPC must agree
            on it, as they must on the endpoint lists: the endpoints are created once per VPC. Gateway endpoints take
            no security group.
        udp_listen_ports: UDP ports to open and listen on on deployed EC2 listeners.
            Each port becomes two ingress rules (IPv4 and IPv6) on the listener's security group; only these ports accept inbound UDP.
        tcp_listen_ports: TCP ports to open and listen on on deployed EC2 listeners.
            Each port becomes two ingress rules (IPv4 and IPv6) on the listener's security group; only these ports accept inbound TCP.
            Combined with udp_listen_ports, at most 30 ports per network target (the default
            AWS quota is 60 inbound rules per security group).
        ec2_instance_type: EC2 instance type to deploy in VPCs under test. Defaults to t3.micro.
        existing_ec2_instance_profile_name: Existing IAM instance profile name to use for EC2 instances. If not set, CATS creates one.
        existing_ec2_ami_id: AMI ID (ami-...) or SSM parameter path (/...) resolved in the deploy region at deploy time.
            Defaults to the latest Amazon Linux AMI CATS selects for the instance type's architecture.
        network_call_timeout_seconds: Seconds SSM may take to deliver a probe command to the listener before CATS gives up;
            30 to 2,592,000 (SSM's own bounds). The probe's own network budgets and the Lambda timeout are fixed
            (see the probe README), not governed by this.
    """

    enabled_network_handlers: UniqueList[EnabledNetworkHandler] = Field(
        default=[EnabledNetworkHandler.EC2],
        min_length=1,
        description="Network handlers to deploy; at least one, a declared target exists to be probed. "
        + "First in list is used by http_call(), tcp_call(), udp_call().",
    )
    interface_endpoints: UniqueList[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")]] = Field(
        default=[],
        description="AWS service names (ssm, ssmmessages, ec2messages, ...) for interface VPC endpoints CATS creates in the target VPC. "
        + "An escape hatch for a VPC with no endpoint for the service and no DNS already answering for it: the create fails where the VPC "
        + "has an endpoint or a private hosted zone for the name, and where a Resolver rule already sends the name elsewhere the endpoint "
        + "is created but never used. Where the organization's endpoints are reachable the EC2 handler needs none; where they are not, "
        + "use the Lambda handler.",
    )
    gateway_endpoints: UniqueList[GatewayEndpoints] = Field(
        default=[],
        description="Gateway VPC endpoints (S3, DYNAMODB, S3_EXPRESS) CATS creates in the target VPC. "
        + "S3 is what a UDP target's listener needs to write receipts where its subnet has no other path to S3.",
    )
    interface_endpoint_security_group_ids: UniqueList[ValidSecurityGroupId] | None = Field(
        default=None,
        description="Security groups for the interface VPC endpoints CATS creates (sg-... or SSM parameter paths), admitting TCP 443 from the "
        + "listener subnets. Unset, each endpoint is in its VPC's default group, whose stock rule admits only that group's members, "
        + "which the listener is not. Targets sharing a VPC must agree on it.",
    )
    udp_listen_ports: UniqueList[ValidListenPort] = Field(
        default=[],
        description="UDP ports to open and listen on on deployed EC2 listeners. Each port becomes two security group ingress rules (IPv4 and IPv6).",
    )
    tcp_listen_ports: UniqueList[ValidListenPort] = Field(
        default=[],
        description="TCP ports to open and listen on on deployed EC2 listeners. Each port becomes two security group ingress rules (IPv4 and IPv6). "
        + "Combined with udp_listen_ports, at most 30 ports per network target.",
    )
    ec2_instance_type: str = Field(
        default="t3.micro",
        pattern=r"^[a-z0-9-]+\.[a-z0-9-]+$",  # a family, a dot, a size such as micro, 2xlarge or metal-24xl; also the YAML injection guard
        description="EC2 instance type of the listener deployed into each network target.",
    )
    existing_ec2_instance_profile_name: str | None = Field(
        default=None,
        pattern=rf"^{IAM_NAME_CHARACTER_CLASS}{{1,128}}$",
        description="Existing IAM instance profile name to use for EC2 instances. If not set, CATS creates one.",
    )
    existing_ec2_ami_id: str | None = Field(
        default=None,
        pattern=rf"^(ami-[0-9a-f]{{8,17}}|{SSM_PARAMETER_NAME_PATTERN})$",
        description="AMI ID (ami-...) or SSM parameter path (/...) resolved at deploy time. Defaults to the latest Amazon Linux AMI CATS selects.",
    )
    network_call_timeout_seconds: int = Field(
        default=30,
        ge=30,
        le=MAX_SSM_COMMAND_TIMEOUT_SECONDS,
        description="Seconds SSM may take to deliver a probe command before CATS gives up; 30 to 2,592,000, SSM's own bounds. "
        + "Probe and Lambda budgets are fixed.",
    )

    @model_validator(mode="after")
    def _reject_interface_endpoints_sharing_a_logical_id(self) -> Self:
        """Two names that differ only by '.' or '-' render one CloudFormation logical id, which the renderer refuses at deploy."""
        seen: dict[str, str] = {}
        for endpoint in self.interface_endpoints:
            logical_id = f"InterfaceEndpoint{interface_endpoint_logical_id_part(endpoint)}Vpc<n>"
            if logical_id in seen:
                raise ValueError(
                    f"interface_endpoints '{seen[logical_id]}' and '{endpoint}' both render the CloudFormation logical id "
                    + f"'{logical_id}'; keep one of them."
                )
            seen[logical_id] = endpoint
        return self


def interface_endpoint_logical_id_part(endpoint: str) -> str:
    """The service part of an interface endpoint's logical id, as stack.yaml.j2 builds it: `replace('.', '') | replace('-', '') | capitalize`."""
    return endpoint.replace(".", "").replace("-", "").capitalize()


# =============================================================================
# Network and Account Models
# =============================================================================


class CatsNetworkUnderTest(CatsBaseModel):
    """
    Target network segment to send and receive network plane data.

    The key name in cats.yaml defines the name of the generated AccountUnderTest
    resource within CATS test code.

    ``vpc_id``, ``subnet_ids`` and ``existing_security_group_ids`` each take a literal id
    (``vpc-...``, ``subnet-...``, ``sg-...``) or the name of an SSM parameter holding one, which
    must start with ``/``. The parameter is read in the target account at deploy time.

    Attributes:
        vpc_id: The VPC to work from: a VPC id (vpc-...) or an SSM parameter name (/...) containing one
        subnet_ids: A list of subnet ids (subnet-...) or SSM parameter names (/...) containing subnet IDs inside target VPC.
            The EC2 listener deploys into the first subnet; Lambda handlers and interface endpoints use all of them,
            so list every subnet (e.g. one per AZ) the Lambda probes should reach from.
        subnet_id: The single-subnet spelling: ``subnet_id: subnet-1`` means ``subnet_ids: [subnet-1]``. Give one
            or the other, not both. Input only: the loaded model carries ``subnet_ids`` alone, so a dumped
            configuration re-validates.
        existing_security_group_ids: Security groups to attach to every resource CATS deploys in this target -
            the EC2 listener and, when the lambda handler is enabled, the probe Lambda. Accepts security
            group ids (sg-...) or SSM parameter names (/...) holding one, resolved in the target account at deploy time.
            By default CATS creates its own groups instead: one for the EC2 listener with ingress scoped to the
            target's tcp_listen_ports and udp_listen_ports, and an egress-only one for the lambdas. Provide your
            own groups for any other ingress posture (open-all, custom source CIDRs, fully closed), or to speed
            up `cats infra destroy` with the lambda handler - CloudFormation cannot delete a stack-owned lambda
            security group until Lambda releases its Hyperplane ENIs, which takes up to ~20 minutes. Groups CATS
            does not own are never waited on.
            The groups must allow the traffic your tests need, in both directions. Outbound is the one most
            likely to bite: the lambda handlers only reach a destination if the groups permit egress to it,
            and a group with any explicit egress rule no longer allows all outbound traffic. Inbound matters
            too - reaching an EC2 listener requires access on its tcp_listen_ports and udp_listen_ports.
            Traffic a group blocks surfaces as a FAILURE result at test time, not a deploy-time error.
        tags: Metadata for filtering. List format: ['flag', {'key': 'value'}] or dict format: {'key': 'value'}
        network_settings: Per-target network settings. Populated with resolved values after config load.
    """

    vpc_id: ValidVpcId
    subnet_id: ValidSubnetId | None = Field(default=None, exclude=True)  # Input-only single-subnet spelling, folded into subnet_ids
    subnet_ids: UniqueList[ValidSubnetId] = []  # EC2 listener uses [0]; Lambda VpcConfig and interface endpoints use all
    existing_security_group_ids: UniqueList[ValidSecurityGroupId] | None = None
    tags: CatsTags = {}
    network_settings: NetworkSettings = NetworkSettings()

    # The either-or rule of _normalize_subnet_ids, for editors.
    model_config = SettingsConfigDict(json_schema_extra={"oneOf": [{"required": ["subnet_id"]}, {"required": ["subnet_ids"]}]})

    _normalize_tags: ClassVar[object] = field_validator("tags", mode="before")(_normalize_tags)

    @model_validator(mode="after")
    def _normalize_subnet_ids(self) -> "CatsNetworkUnderTest":
        if self.subnet_id is not None and len(self.subnet_ids) > 0:
            raise ValueError("Use subnet_id or subnet_ids, not both")
        if self.subnet_id is not None:
            self.subnet_ids = [self.subnet_id]
            self.subnet_id = None
        if len(self.subnet_ids) == 0:
            raise ValueError("Either subnet_id or subnet_ids must be provided")
        return self


def _validate_managed_policy(policy: str) -> str:
    """Accept an AWS managed policy name (optionally with path) or a full policy ARN."""
    if policy.startswith("arn:"):
        if re.fullmatch(MANAGED_POLICY_ARN_PATTERN, policy) is None:
            raise ValueError(f"'{policy}' is not a valid IAM policy ARN")
        return policy
    if re.match(MANAGED_POLICY_NAME_PATTERN, policy) is None:
        raise ValueError(f"'{policy}' is not a valid IAM managed policy name or ARN")
    return policy


ValidManagedPolicy = Annotated[
    str,
    AfterValidator(_validate_managed_policy),
    Field(json_schema_extra={"pattern": f"^({MANAGED_POLICY_ARN_PATTERN[1:-1]}|{MANAGED_POLICY_NAME_PATTERN[1:-1]})$"}),
]


class CatsAdditionalTestRole(CatsBaseModel):
    """
    CATS deploys one role by default: this configuration block allows defining additional roles.

    Each entry of `additional_roles` is keyed by the role's name: `account.role.<name>` returns a session
    for it, and the deployed IAM role is named {cfn_stack_prefix}{account_keyname}-<name>. A role
    declares at least one of `managed_policies` and `inline_policies`.

    Attributes:
        managed_policies: A list of AWS managed IAM policy names or full policy ARNs to attach to this role.
            Full list available at https://docs.aws.amazon.com/aws-managed-policy/latest/reference/policy-list.html
        inline_policies: Inline IAM policies to attach to this role, keyed by policy name.
            Each value is either a policy document (written inline in yaml/JSON) or a string
            path to a JSON policy file, resolved relative to cats.yaml.
    """

    # The editor's view of _require_a_policy: at least one of the two policy sources is present and non-empty.
    model_config = SettingsConfigDict(
        json_schema_extra={
            "anyOf": [
                {"required": ["managed_policies"], "properties": {"managed_policies": {"minItems": 1}}},
                {"required": ["inline_policies"], "properties": {"inline_policies": {"minProperties": 1}}},
            ]
        }
    )

    managed_policies: UniqueList[ValidManagedPolicy] = []
    inline_policies: dict[ValidIamPolicyName, PolicyDocument] = Field(
        default={}, json_schema_extra=_dict_key_schema(IAM_POLICY_NAME_PATTERN, MAX_IAM_POLICY_NAME_LENGTH)
    )

    @model_validator(mode="after")
    def _require_a_policy(self) -> Self:
        if self.managed_policies == [] and self.inline_policies == {}:
            raise ValueError("an additional role grants no permissions: set managed_policies, inline_policies, or both")
        return self


class AccountSettings(CatsBaseModel):
    """
    Account settings for CATS.

    Used at global and account levels. Fields have sensible defaults.

    Attributes:
        additional_roles: Additional IAM roles to create, keyed by role name and accessible as AccountUnderTest.role.<name>
        default_test_role_managed_policies: A list of AWS managed IAM policy names to attach to the default test role.
        default_test_role_inline_policies: Inline IAM policies to attach to the default test role, keyed by
            policy name. Each value is either a policy document (written inline in yaml/JSON) or a string
            path to a JSON policy file, resolved relative to cats.yaml.
        fixture_scope: Pytest fixture scope for account fixtures
        allow_shell: Enable NetworkHandler.shell() for arbitrary commands on CATS EC2 instances through SSM run-command.
            Off by default. This is a deploy-time setting: when true, the test role's SSM policy includes the
            run-command document. Changing it requires redeployment. See docs/security.md for implications.
    """

    additional_roles: dict[ValidRoleName, CatsAdditionalTestRole] = Field(
        default={}, json_schema_extra=_dict_key_schema(IDENTIFIER_PATTERN, forbidden=RESERVED_ROLE_ATTRIBUTE_NAMES | frozenset(kwlist))
    )
    default_test_role_managed_policies: UniqueList[ValidManagedPolicy] = Field(
        description="Additional permissions to grant CATS in each AccountUnderTest. Adds listed Managed Policies by name or ARN.",
        default=[],
    )
    default_test_role_inline_policies: dict[ValidIamPolicyName, PolicyDocument] = Field(
        description="Inline IAM policies for the default test role, keyed by policy name. "
        + "Values are policy documents, or string paths to JSON files relative to cats.yaml.",
        default={},
        json_schema_extra=_dict_key_schema(IAM_POLICY_NAME_PATTERN, MAX_IAM_POLICY_NAME_LENGTH),
    )
    fixture_scope: Literal["session", "function", "module", "class"] = Field(
        default="session",
        description="Pytest fixture scope for account fixtures",
    )
    allow_shell: bool = Field(
        default=False,
        description="Enable NetworkHandler.shell() for arbitrary commands on CATS EC2 instances through SSM run-command. Deploy-time setting.",
    )

    @model_validator(mode="after")
    def _reject_colliding_role_names(self) -> Self:
        seen: dict[str, str] = {}
        for name in self.additional_roles:
            key = name.lower().replace("_", "")
            if key in seen:
                raise ValueError(
                    f"additional role names '{seen[key]}' and '{name}' would collide at deploy: IAM role names are case-insensitive "
                    + "and the CloudFormation logical ID drops '_', so two names must differ by more than case and underscores."
                )
            seen[key] = name
        return self


class CatsAccountUnderTest(CatsBaseModel):
    """
    One target account to configure specific test resources and tag with metadata.

    The key name in cats.yaml defines the name of the generated AccountUnderTest
    resource within CATS test code.

    Attributes:
        account_id: 12-digit AWS account ID string (required) of target account
        account_name: Optional metadata on an account friendlyname
        account_email: Optional metadata on an account email
        notes: Optional metadata for general notes in your yaml file
        account_network_under_test: A key/value dictionary of network test targets in this account, where each entry
            will have network infrastructure deployed and made available on the AccountUnderTest class resource.
            The number of targets an account can hold is bounded by CloudFormation's 51,200-byte template limit,
            which `cats validate` measures for the exact configuration (targets, handlers and listen ports all
            count); split targets across a second account entry with the same account_id when it says so.
        tags: Metadata for filtering. List format: ['flag', {'key': 'value'}] or dict format: {'key': 'value'}
        network_settings: Per-account network settings override. Overrides global, overridden by target.
        account_settings: Per-account settings override. Overrides global_account_settings.
    """

    account_name: str | None = None
    account_email: str | None = None
    notes: str | None = None
    account_id: ValidAwsAccountId
    # No count cap: the binding constraint is the rendered template's size against CloudFormation's
    # 51,200-byte TemplateBody limit, which depends on handlers and ports as much as on targets, so
    # cats validate renders and measures instead (cats/cfn/render.py, estimate_template_size). The
    # 200-output limit (6 outputs per target, 5 without lambda, + 3 base) sits far beyond it.
    account_network_under_test: dict[ValidNetworkTargetName, CatsNetworkUnderTest] = Field(
        default={},
        json_schema_extra=_dict_key_schema(CFN_LOGICAL_ID_PATTERN, MAX_NETWORK_TARGET_LENGTH, RESERVED_NETWORK_TARGET_NAMES | frozenset(kwlist)),
    )
    tags: CatsTags = {}
    network_settings: NetworkSettings = NetworkSettings()
    account_settings: AccountSettings = AccountSettings()

    _normalize_tags: ClassVar[object] = field_validator("tags", mode="before")(_normalize_tags)

    @model_validator(mode="after")
    def _validate_target_names_not_reserved(self) -> Self:
        seen: set[str] = set()
        for target_name in self.account_network_under_test:
            if target_name in RESERVED_NETWORK_TARGET_NAMES:
                raise ValueError(
                    f"network target name '{target_name}' is reserved: it is an existing attribute "
                    + f"of AccountUnderTest. Reserved names: {sorted(RESERVED_NETWORK_TARGET_NAMES)}"
                )
            _reject_case_only_collision(seen, target_name, "network target names")
            seen.add(target_name)
        return self


# =============================================================================
# Top-Level Settings Models
# =============================================================================


class CatsDeploymentSettings(CatsBaseModel):
    """
    Settings for CATS infrastructure deployment. These settings are used when calling
    'cats infra' commands.

    Attributes:
        deploy_role_name: When deploying from a central account, role to use in all target accounts - for example, AWSControlTowerExecution.
            An IAM role name (not an ARN or a path): interpolated into arn:{partition}:iam::{account_id}:role/{deploy_role_name}.
        allow_deploy_to_current_account: Allow deploying to the starting account, with current role
        parent_account_id: 12-digit AWS account ID that test roles will trust (the account running CATS).
            Required by deploy and show-template; validation and config loading work without it.
        aws_region: Region CATS deploys its stacks into and connects its test sessions to. Falls back to the
            AWS_REGION environment variable when unset. Required by deploy, show-template, test-time session
            construction and `cats run`; validation and config loading work without it.
        partition: AWS partition. Derived from aws_region when unset (us-gov-west-1 gives aws-us-gov, cn-north-1
            gives aws-cn, eusc-de-east-1 gives aws-eusc); a Region no partition claims is treated as aws, as
            botocore does. An explicit value that disagrees with the Region is a configuration error.
        cfn_stack_prefix: Prefix for all deployed CATS resources. Used to namespace stacks, roles, and parameters.
            May not start with aws, amazon or amzn (in any case): SSM reserves those for its own document names,
            and the probe document is named {cfn_stack_prefix}{account}-Probe.
    """

    deploy_role_name: ValidIamRoleName | None = Field(
        default=None,
        description="IAM role name CATS assumes in each account under test to deploy; "
        + "interpolated into arn:{partition}:iam::{account_id}:role/{name}.",
    )
    allow_deploy_to_current_account: bool = Field(
        default=False,
        description="By default, CATS will not try and deploy infrastructure in the account it begins working in. Set this to True to allow deploying"
        + " test infrastructure without role assumption in the profile running CATS. For example, set this to deploy tests in an AWS Organizations"
        + " root account using the credentials you called `cats infra` with.",
    )
    parent_account_id: ValidAwsAccountId | None = None
    aws_region: str | None = Field(default=None, pattern=r"^[a-z]{2}[a-z0-9-]*-[0-9]+$")
    partition: Annotated[str, AfterValidator(_is_known_partition)] = Field(
        default="aws",
        description="AWS partition. Derived from aws_region when unset; an explicit value must agree with the Region.",
        json_schema_extra=_partition_schema,
    )
    cfn_stack_prefix: Annotated[str, AfterValidator(_reject_ssm_reserved_prefix)] = Field(
        default="CATS-test-infra-",
        pattern=CFN_STACK_PREFIX_PATTERN,
        max_length=MAX_CFN_STACK_PREFIX_LENGTH,
        json_schema_extra=_stack_prefix_schema,
    )

    model_config = SettingsConfigDict(json_schema_extra=_deployment_settings_schema)

    @model_validator(mode="after")
    def _derive_partition_from_region(self) -> Self:
        """The partition follows the Region: unset, it is the one botocore assigns the Region; explicit, it must agree."""
        if self.aws_region is None:
            return self
        region_partition = partition_for_region(self.aws_region)
        if region_partition is None:
            return self  # a Region prefix newer than the installed botocore is nobody's; botocore treats it as aws
        if "partition" not in self.model_fields_set:
            self.partition = region_partition
        elif self.partition != region_partition:
            raise ValueError(
                f"cats_deployment_settings.partition '{self.partition}' does not contain Region '{self.aws_region}', which belongs to "
                + f"partition '{region_partition}'. Remove partition (CATS derives it from aws_region) or correct one of the two."
            )
        return self


class ReportFormat(StrEnum):
    """Supported report output formats.

    Values map to report writers that run at the end of a pytest session.
    See ``cats.report_writers`` for implementations.

    Members:
        CATS_JSON: Native CATS report written to ``report.json``. The historical
            default and the format consumed by CATS's own tooling.
        OSCAL_AR: OSCAL 1.2.1 Assessment Results written to
            ``assessment-results.json``. Suitable for import into
            compliance-trestle workspaces and other OSCAL tooling.
        JUNIT: JUnit XML written to ``junit.xml`` for CI systems. Produced by
            pytest's own junitxml plugin, which CATS switches on at configure time
            and feeds a property per standard, account, tag and marker on every
            testcase; the testsuite carries the run id.
        SECURITYHUB: One AWS Security Hub finding per test per account under test,
            imported into each account through its CATS test role, so failures
            surface where control state is already watched and aggregated. Writes
            ``securityhub-findings.json`` (what was sent, and per-account delivery
            status) when report files are on; imports either way. Settings under
            ``reporting.securityhub``; requires ``cats infra deploy`` after enabling,
            for the ``securityhub:BatchImportFindings`` grant on the test role.
    """

    CATS_JSON = "cats_json"
    OSCAL_AR = "oscal_ar"
    JUNIT = "junit"
    SECURITYHUB = "securityhub"


class SecurityHubSeverity(StrEnum):
    """ASFF ``Severity.Label`` values, as Security Hub defines them."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFORMATIONAL = "INFORMATIONAL"


class CatsSecurityHubSettings(CatsBaseModel):
    """
    Settings for the ``securityhub`` report format.

    Attributes:
        aws_region: Region whose Security Hub receives the findings, the same in every account under
            test. Defaults to ``cats_deployment_settings.aws_region``. The test role's import grant
            is scoped to this Region's product ARN, so changing it requires ``cats infra deploy``.
        failed_severity: ``Severity.Label`` of the finding for a failed test. Defaults to ``HIGH``:
            a control that does not enforce. Errors are ``MEDIUM``, expected failures ``LOW``,
            passes and skips ``INFORMATIONAL``.
        only_with_standard: Import findings only for tests that carry a standard (``@cats(standard_name=...)``),
            the default: a compliance finding needs a control to attest to, and ``report.json`` still records
            every test. ``false`` imports every test that used an account fixture, typed ``CATS`` when it
            carries no standard. Removing ``@cats`` from a test stops updating its finding, which stays
            ``ACTIVE`` in Security Hub for 90 days.
    """

    aws_region: str | None = Field(
        default=None,
        pattern=r"^[a-z]{2}[a-z0-9-]*-[0-9]+$",
        description="Region whose Security Hub receives the findings. Defaults to cats_deployment_settings.aws_region.",
    )
    failed_severity: SecurityHubSeverity = Field(
        default=SecurityHubSeverity.HIGH,
        description="Severity label of the finding for a failed test.",
    )
    only_with_standard: bool = Field(
        default=True,
        description="Import findings only for tests that carry a standard. false imports every test that used an account fixture.",
    )


class CatsReportingSettings(CatsBaseModel):
    """
    Settings for CATS reporting while running Pytest tests.

    Report writers are dispatched at the end of a pytest session based on
    ``report_formats``. Multiple formats can be emitted from a single run —
    see the Reporting guide for the full output contract and the OSCAL
    ``pytest_cats_standards`` hook.

    Attributes:
        enabled: Whether to emit any reports at all. ``False`` disables every
            writer including ``junit`` and ``securityhub``, and withholds the
            ``securityhub:BatchImportFindings`` grant from the test role at deploy.
        report_formats: Writers to run, in order. Each value corresponds to a
            :class:`ReportFormat` member (``cats_json``, ``oscal_ar``, ``junit``,
            ``securityhub``). Defaults to ``[cats_json]``; at least one is required
            (to write nothing, set ``enabled`` to false). List ``securityhub`` last: the
            import is network-bound and the files before it never wait on it.
        custom_markers: Marker names permitted in ``@cats(...)`` kwargs. Values
            surface in both the CATS JSON and OSCAL outputs (as top-level keys
            and ``Observation.props[]`` respectively).
        oscal_import_ap_href: URI reference recorded as ``import-ap.href`` in the
            OSCAL assessment results — point it at your assessment plan document.
            Defaults to an empty reference.
        securityhub: Region and severity for the ``securityhub`` format; see
            :class:`CatsSecurityHubSettings`. Ignored unless ``securityhub`` is in
            ``report_formats``.
        report_dir: Directory for every report file (``report.json``,
            ``assessment-results.json``, ``junit.xml``, ``securityhub-findings.json``
            and their timestamped variants). A relative path resolves against the
            directory that holds ``cats.yaml``, like ``inline_policies`` file paths.
            ``--cats-report-dir`` on the pytest command line overrides it. Unset
            means the current working directory.
        latest_files: Write the fixed-name files (``report.json``,
            ``assessment-results.json``, ``junit.xml``, ``securityhub-findings.json``),
            overwritten on every run so they always describe the newest run.
        timestamped_files: Also write one immutable set per run, named with the
            run id: ``report-<run_id>.json``, ``assessment-results-<run_id>.json``,
            ``junit-<run_id>.xml``, ``securityhub-findings-<run_id>.json``. The run id is
            ``YYYYMMDDTHHMMSSZ`` (UTC) and is also recorded inside every file, so the
            files of one run can be matched. A run that writes timestamped files claims
            its id when the session starts, so two sessions starting in the same second
            never share one. With ``latest_files`` and ``timestamped_files`` both ``false``
            no report file is written at all; ``securityhub`` still imports its findings.
            ``enabled: false`` is the switch that disables every writer.
    """

    enabled: bool = True
    report_formats: UniqueList[ReportFormat] = Field(default=[ReportFormat.CATS_JSON], min_length=1)
    custom_markers: list[str] = Field(
        default=[],
        # _validate_custom_markers' rules, for editors: an identifier not prefixed cats_, not a reserved marker, listed once.
        json_schema_extra={
            "items": {
                "type": "string",
                "pattern": rf"^(?!{CATS_MARKER_PREFIX})[A-Za-z_][A-Za-z0-9_]*$",
                "not": {"enum": _json_list(PYTEST_BUILTIN_MARKERS | CATS_DECORATOR_KEYWORDS)},
            },
            "uniqueItems": True,
        },
    )
    oscal_import_ap_href: str = ""
    securityhub: CatsSecurityHubSettings = CatsSecurityHubSettings()
    report_dir: str | None = None
    latest_files: bool = True
    timestamped_files: bool = False

    @field_validator("custom_markers")
    @classmethod
    def _validate_custom_markers(cls, names: list[str]) -> list[str]:
        """A custom marker is a keyword of @cats and a pytest marker, so it must be usable as both and collide with neither."""
        seen: set[str] = set()
        for name in names:
            if name.isidentifier() is False or iskeyword(name) is True:
                raise ValueError(
                    f"reporting.custom_markers: '{name}' is not a valid Python identifier; a custom marker is passed to @cats as a keyword."
                )
            if name.startswith(CATS_MARKER_PREFIX) is True:
                raise ValueError(
                    f"reporting.custom_markers: '{name}' starts with '{CATS_MARKER_PREFIX}', the prefix reserved for CATS's own markers."
                )
            if name in PYTEST_BUILTIN_MARKERS:
                raise ValueError(f"reporting.custom_markers: '{name}' is a pytest built-in marker; apply @pytest.mark.{name} directly.")
            if name in CATS_DECORATOR_KEYWORDS:
                raise ValueError(f"reporting.custom_markers: '{name}' is a @cats keyword, not a custom marker.")
            if name in seen:
                raise ValueError(f"reporting.custom_markers: '{name}' is listed more than once.")
            seen.add(name)
        return names

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_junit_enabled(cls, data: object) -> object:
        """``junit_enabled`` is not a key: name the fix for the value the file carries, not 'Extra inputs are not permitted'.

        A file carrying ``false`` is told to delete the key; one carrying ``true`` is told to add
        the ``junit`` format, which is what the key would have meant.
        """
        if isinstance(data, dict) and "junit_enabled" in data:
            if data["junit_enabled"] is False:
                raise ValueError(
                    "reporting.junit_enabled has been removed: delete it. junit.xml is the optional 'junit' report format; "
                    + "add junit to reporting.report_formats only if you want the file."
                )
            raise ValueError(
                "reporting.junit_enabled has been removed: junit.xml is the 'junit' report format. "
                + "Delete junit_enabled and add junit to reporting.report_formats, e.g. report_formats: [cats_json, junit]"
            )
        return data


class TestSelectionExclusion(CatsBaseModel):
    """
    One entry of ``test_selection.excluded``: a test carrying one of ``values`` for ``marker`` does not run.

    Attributes:
        marker: The marker to match, by the name written on the test: ``standard_name`` (the ``@cats``
            keyword), ``cats_requires`` (the decorator), or a name declared in ``reporting.custom_markers``.
            Anything else is a configuration error, never a silent no-op: an exclusion that matches
            nothing would let the tests it was meant to hold back into the run.
        values: The values that exclude a test, compared for equality with the marker's value:
            a string, or a YAML boolean, so ``true`` matches ``@cats(mutating=True)`` and ``"true"``
            does not.
        reason: Why these tests do not run here. Recorded on every excluded test as its ``not_run``
            reason in every report format, so the control stays in the evidence together with the
            decision that kept it out.
    """

    marker: str = Field(min_length=1, description="standard_name, cats_requires, or a marker declared in reporting.custom_markers.")
    values: UniqueList[str | StrictBool] = Field(min_length=1, description="Marker values that exclude a test; equality on the marker's value.")
    reason: str = Field(min_length=1, description="Why these tests do not run here; recorded as each excluded test's not_run reason.")


class CatsTestSelectionSettings(CatsBaseModel):
    """
    Which tests run against this deployment, decided in configuration rather than on the command line.

    A command-line selector (``-k``, ``-m``, ``--deselect``) narrows a run, and a deselected control
    vanishes from the report with no record of why. These keys instead define the run's scope in
    the configuration itself, and every test they hold back is recorded as ``not_run`` with its
    reason, in every report format.

    Attributes:
        provides: The requirements this deployment satisfies: free-form names matching
            ``@cats_requires(...)`` on tests. A test runs only when every requirement it declares is
            provided; otherwise it is ``not_run`` with the gap named. Unioned with what every
            ``pytest_cats_provides`` hook returns, so a test pack can provide its defaults.
        excluded: Tests held back by marker value, each with a reason (see
            :class:`TestSelectionExclusion`). A test matching any entry is ``not_run``. An entry that
            matches no test is reported in the terminal summary and in ``run.selection``: not an
            error, since a ``-k`` run or a renamed test produces it, but usually a sign that the
            exclusion is not doing what was intended.
    """

    provides: UniqueList[Annotated[str, Field(min_length=1)]] = Field(
        default=[], description="Requirements this deployment satisfies; a test requiring one not listed here is not run."
    )
    excluded: list[TestSelectionExclusion] = Field(
        default=[], description="Tests held back by marker value, each with a reason; recorded as not_run."
    )


class CatsConfig(BaseSettings, CatsBaseModel):
    """
    This class defines all CATS config management and ultimately the contents of cats.yaml
      - each subclass attribute defines a yaml key and section.

    See each subclass for greater detail.
    """

    # Every field in this class has a default so CATS can load when a config is not present.
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # No env_settings: config values come from cats.yaml only. The environment
        # surface is CATS_CONFIG (file selection) and the AWS_REGION fallback validator.
        return (
            init_settings,
            UniqueKeyYamlConfigSettingsSource(settings_cls, yaml_file=get_cats_config_filepath(), yaml_file_encoding="utf-8"),
        )

    model_config = SettingsConfigDict(
        extra="forbid",
        case_sensitive=True,  # top-level section names are keys like every other key; REPORTING: is unknown
    )

    cats_deployment_settings: CatsDeploymentSettings = CatsDeploymentSettings()
    global_network_settings: NetworkSettings = NetworkSettings()
    global_account_settings: AccountSettings = AccountSettings()
    accounts_under_test: dict[ValidAccountKeyname, CatsAccountUnderTest] = Field(
        default={}, json_schema_extra=_dict_key_schema(IDENTIFIER_PATTERN, MAX_ACCOUNT_KEYNAME_LENGTH)
    )
    reporting: CatsReportingSettings = CatsReportingSettings()
    test_selection: CatsTestSelectionSettings = CatsTestSelectionSettings()

    @model_validator(mode="after")
    def _validate_excluded_markers(self) -> Self:
        """Every test_selection.excluded marker is one a test can carry: a typo is an error, not an exclusion of nothing."""
        accepted = [*sorted(TEST_SELECTION_MARKER_ALIASES), *self.reporting.custom_markers]
        for index, exclusion in enumerate(self.test_selection.excluded):
            if exclusion.marker in accepted:
                continue
            nearest = get_close_matches(exclusion.marker, accepted, n=1, cutoff=0.6)
            hint = f"; did you mean '{nearest[0]}'?" if nearest else ""
            declared = ", ".join(self.reporting.custom_markers) or "none declared"
            raise ValueError(
                f"test_selection.excluded[{index}].marker '{exclusion.marker}' is not a marker a test can carry: "
                + f"use standard_name, cats_requires, or a name declared in reporting.custom_markers ({declared}){hint}"
            )
        return self

    @model_validator(mode="after")
    def _reject_case_only_keyname_collisions(self) -> Self:
        seen: set[str] = set()
        for keyname in self.accounts_under_test:
            _reject_case_only_collision(seen, keyname, "accounts_under_test keynames")
            seen.add(keyname)
        return self

    @model_validator(mode="before")
    @classmethod
    def _use_aws_region_env_var(cls, data: dict[str, object]) -> dict[str, object]:
        """Fall back to AWS_REGION env var if aws_region not explicitly set."""
        if "AWS_REGION" in os.environ:
            deployment_settings = data.get("cats_deployment_settings", {})
            if isinstance(deployment_settings, dict) and "aws_region" not in deployment_settings:
                deployment_settings["aws_region"] = os.environ["AWS_REGION"]
                data["cats_deployment_settings"] = deployment_settings
        return data

    @model_validator(mode="after")
    def _resolve_hierarchical_settings(self) -> Self:
        """Inherit settings from parent levels where not explicitly set."""
        for account in self.accounts_under_test.values():
            # Account settings: inherit from global where not set
            for field_name in AccountSettings.model_fields:
                if field_name not in account.account_settings.model_fields_set:
                    setattr(account.account_settings, field_name, getattr(self.global_account_settings, field_name))

            # Account network settings: inherit from global where not set
            for field_name in NetworkSettings.model_fields:
                if field_name not in account.network_settings.model_fields_set:
                    setattr(account.network_settings, field_name, getattr(self.global_network_settings, field_name))

            # Target network settings: inherit from account where not set
            for target in account.account_network_under_test.values():
                for field_name in NetworkSettings.model_fields:
                    if field_name not in target.network_settings.model_fields_set:
                        setattr(target.network_settings, field_name, getattr(account.network_settings, field_name))
        return self

    @model_validator(mode="after")
    def _validate_combined_listen_ports(self) -> Self:
        """Reject port lists the deployed security group cannot hold.

        Each configured listen port renders two ingress rules (IPv4 and IPv6) on the CATS EC2
        listener security group; the default AWS quota is 60 inbound rules per group. Runs after
        hierarchical settings resolution because tcp and udp lists can arrive at a
        target from different levels.
        """
        for account_name, account in self.accounts_under_test.items():
            for target_name, target in account.account_network_under_test.items():
                settings = target.network_settings
                combined = len(settings.tcp_listen_ports) + len(settings.udp_listen_ports)
                if combined > MAX_COMBINED_LISTEN_PORTS:
                    raise ValueError(
                        f"Network target '{target_name}' in account '{account_name}' resolves to {combined} "
                        + f"listen ports (tcp_listen_ports + udp_listen_ports); the maximum is {MAX_COMBINED_LISTEN_PORTS}. "
                        + "Each port becomes two security group ingress rules (IPv4 and IPv6) and the default AWS quota "
                        + "is 60 inbound rules per security group."
                    )
        return self

    @model_validator(mode="after")
    def _validate_generated_iam_role_names(self) -> Self:
        """Reject IAM role and Lambda function names that the rendered template cannot create.

        Runs after hierarchical settings resolution because the probe function exists only for
        targets whose effective handlers include lambda. The stack's other generated role names,
        `{prefix}{account}-ec2` and `{prefix}{account}-lambda`, are one per stack and shorter than
        `{prefix}{account}-default-test-role`, so the default test role's check covers them.
        """
        prefix = self.cats_deployment_settings.cfn_stack_prefix
        for account_name, account in self.accounts_under_test.items():
            resource_prefix = f"{prefix}{account_name}"
            role_names = [f"{resource_prefix}-default-test-role"]
            role_names.extend(f"{resource_prefix}-{role_name}" for role_name in account.account_settings.additional_roles)

            for target_name, target in account.account_network_under_test.items():
                if EnabledNetworkHandler.LAMBDA in target.network_settings.enabled_network_handlers:
                    function_name = f"{resource_prefix}-probe-{target_name}"
                    if len(function_name) > MAX_LAMBDA_FUNCTION_NAME_LENGTH:
                        raise ValueError(
                            f"Generated Lambda function name '{function_name}' is {len(function_name)} characters; "
                            + f"Lambda FunctionName has a maximum of {MAX_LAMBDA_FUNCTION_NAME_LENGTH}. "
                            + "Shorten cats_deployment_settings.cfn_stack_prefix, "
                            + f"account key '{account_name}', or target '{target_name}'."
                        )

            for role_name in role_names:
                if len(role_name) > MAX_IAM_ROLE_NAME_LENGTH:
                    raise ValueError(
                        f"Generated IAM role name '{role_name}' is {len(role_name)} characters; "
                        + f"IAM RoleName has a maximum of {MAX_IAM_ROLE_NAME_LENGTH}. "
                        + "Shorten cats_deployment_settings.cfn_stack_prefix, "
                        + f"account key '{account_name}', or the relevant target or role name."
                    )
        return self

    @model_validator(mode="after")
    def _validate_securityhub_region_partition(self) -> Self:
        """Findings are imported through the test role, which lives in the deploy partition; the hub's Region must be there too."""
        securityhub_region = self.reporting.securityhub.aws_region
        if securityhub_region is None:
            return self
        partition = self.cats_deployment_settings.partition
        securityhub_partition = partition_for_region(securityhub_region)
        if securityhub_partition is not None and securityhub_partition != partition:
            deploy_region = self.cats_deployment_settings.aws_region
            partition_source = (
                f"the partition of cats_deployment_settings.aws_region '{deploy_region}'"
                if deploy_region is not None
                else "cats_deployment_settings.partition"
            )
            raise ValueError(
                f"reporting.securityhub.aws_region '{securityhub_region}' belongs to partition '{securityhub_partition}', not '{partition}', "
                + f"{partition_source}; Security Hub findings are imported through the test role in that partition."
            )
        return self

    def effective_securityhub_region(self) -> str | None:
        """The Region the ``securityhub`` format imports into, or None when the format is not configured or reporting is off.

        Deploy (the test role's import grant names this Region's product ARN) and test time (the
        client that imports) both read this, so they cannot disagree.
        """
        if self.reporting.enabled is False or ReportFormat.SECURITYHUB not in self.reporting.report_formats:
            return None
        if self.reporting.securityhub.aws_region is not None:
            return self.reporting.securityhub.aws_region
        return self.require_aws_region()

    def require_aws_region(self) -> str:
        """The deploy Region, or CatsConfigError when cats.yaml leaves it unset and AWS_REGION is not exported.

        Config loading and `cats validate` work without a Region; deploy, show-template, test-time
        session construction and `cats run` call this because they touch AWS.
        """
        if self.cats_deployment_settings.aws_region is None:
            raise CatsConfigError(AWS_REGION_UNSET_MESSAGE)
        return self.cats_deployment_settings.aws_region


_settings: CatsConfig | None = None


class UniqueKeyYamlConfigSettingsSource(YamlConfigSettingsSource):
    """The YAML settings source, reading through the duplicate-key-rejecting loader.

    Only an existing file reaches ``_read_file`` (pydantic-settings skips absent ones), so an
    empty or non-mapping document is a mistake in a file the user wrote, not the no-config case.
    """

    def _read_file(self, file_path: Path | Traversable) -> dict[str, Any]:
        with file_path.open(encoding=self.yaml_file_encoding) as yaml_file:
            loaded: object = load_yaml(yaml_file)
        if loaded is None:
            raise CatsConfigError(
                f"{file_path} is empty: a CATS configuration is a YAML mapping of top-level sections "
                + "(cats_deployment_settings, accounts_under_test, ...). Delete the file to run without one."
            )
        if not isinstance(loaded, dict):
            raise CatsConfigError(
                f"{file_path} must be a YAML mapping of top-level sections (cats_deployment_settings, accounts_under_test, ...), "
                + f"got a {type(loaded).__name__}"
            )
        mapping: dict[str, Any] = loaded
        return mapping


def Settings() -> CatsConfig:
    """Get the current settings, loading from config file on first access.

    Config path resolution (in order of precedence):
    1. Path set via set_cats_config_filepath()
    2. CATS_CONFIG environment variable
    3. ./cats.yaml

    IMPORTANT: Config path must be set before first call to Settings().
    CLI commands must not call Settings() at module import time.

    Raises:
        CatsConfigError: The file is not a valid CATS configuration. The message names the
            file and carries pydantic's report of every failing field. `cats validate`
            formats the same report itself through `CatsConfig.model_validate`.
    """
    global _settings
    if _settings is None:
        try:
            _settings = CatsConfig()
        except ValidationError as e:
            raise CatsConfigError(f"{get_cats_config_filepath()} is not a valid CATS configuration:\n{e}") from e
        except yaml.YAMLError as e:
            raise CatsConfigError(f"{get_cats_config_filepath()} is not valid YAML: {e}") from e
    return _settings


def reset_settings() -> None:
    """Reset settings cache. For testing only."""
    global _settings
    _settings = None


_config_path_lock = threading.RLock()


@contextmanager
def override_config_path(path: Path | None) -> Iterator[Path]:
    """Temporarily point Settings() at a specific cats.yaml, restoring the previous state on exit.

    With None, the existing chain applies (programmatic override, CATS_CONFIG, ./cats.yaml)
    and the settings cache is still reset so the block sees a fresh load. An explicit path
    that is not a file raises CatsConfigError. Reentrant; overlapping callers on other
    threads (e.g. concurrent MCP tool calls) are serialized by the lock.

    The lock is held for the whole block, including any AWS calls made inside it, so
    concurrent MCP tool calls that touch config run one at a time. Correctness over
    throughput: the alternative is one call reading another call's cats.yaml.

    Yields the effective config path for the block.
    """
    with _config_path_lock:
        if path is not None:
            require_config_file(Path(path))
        previous = get_cats_config_override()
        if path is not None:
            set_cats_config_filepath(Path(path))
        reset_settings()
        try:
            yield get_cats_config_filepath()
        finally:
            set_cats_config_filepath(previous)
            reset_settings()


def generate_schema(output_dir: Path | None = None) -> Path:
    """Generate .schema.json. Defaults to config file's directory.

    The schema carries a versioned `$id`: the cats.yaml schema is a covered
    surface of the compatibility contract, and the version ties an exported
    schema file to the package release that produced it. The docs build publishes
    the same document at that URL (overrides/hooks/publish_schema.py).
    """
    target_dir = output_dir or get_default_output_dir()
    output_path = target_dir / ".schema.json"
    schema: dict[str, object] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://awslabs.github.io/cats/schema/v{__version__}.json",
        **CatsConfig.model_json_schema(),
    }
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(schema, f, indent=4)
    return output_path


INIT_PLACEHOLDER_PARENT_ACCOUNT_ID = "123456789012"
INIT_PLACEHOLDER_DEPLOY_ROLE_NAME = "replace-me"
INIT_PLACEHOLDER_ACCOUNT_ID = "234567890123"  # visibly not the parent placeholder: the example is a cross-account setup


def default_yaml_config(
    parent_account_id: str = INIT_PLACEHOLDER_PARENT_ACCOUNT_ID,
    deploy_role_name: str = INIT_PLACEHOLDER_DEPLOY_ROLE_NAME,
    aws_region: str = "us-east-1",
) -> dict[str, Any]:
    """The smallest cats.yaml that validates, as data: the three deployment values, one placeholder account, the report directory.

    Validated here, so `cats init` can refuse a bad argument before it writes any file.

    Raises CatsUsageError when a value would not validate: --aws-region / AWS_REGION /
    --deploy-role-name arrive unvalidated, so this is the caller's argument, not a bug.
    """
    config: dict[str, Any] = {
        "cats_deployment_settings": {"parent_account_id": parent_account_id, "deploy_role_name": deploy_role_name, "aws_region": aws_region},
        "accounts_under_test": {"myaccount": {"account_id": INIT_PLACEHOLDER_ACCOUNT_ID}},
        "reporting": {"report_dir": "reports", "timestamped_files": True},
    }
    try:
        CatsConfig.model_validate(config)  # the file cats init writes always validates
    except ValidationError as e:
        raise CatsUsageError(f"cats init was given a value cats.yaml would not accept:\n{e}") from e
    return config


def create_default_yaml(config: dict[str, Any], output_dir: Path | None = None) -> Path:
    """Write ``config``, the validated result of `default_yaml_config`, as cats.yaml.

    Every other key keeps its default and is absent, so the file is what the user chose or must edit,
    not a dump of every default. Defaults to config file's directory if output_dir not specified.
    """
    target_dir = output_dir or get_default_output_dir()
    output_path = target_dir / "cats.yaml"
    with output_path.open("w", encoding="utf-8") as outfile:
        outfile.write("# yaml-language-server: $schema=./.schema.json\n")
        dump_yaml(config, outfile)
    return output_path


def init_placeholders_left(config: CatsConfig) -> list[str]:
    """Every `cats init` placeholder still in the config, each as '<key> is the placeholder <value> ...'.

    The file validates with them; deploy fails at assume_role. `cats validate` warns so the
    init -> validate -> deploy path gets its signal before the STS error.
    """
    left: list[str] = []
    if config.cats_deployment_settings.parent_account_id == INIT_PLACEHOLDER_PARENT_ACCOUNT_ID:
        left.append(f"cats_deployment_settings.parent_account_id is the placeholder '{INIT_PLACEHOLDER_PARENT_ACCOUNT_ID}' written by cats init")
    if config.cats_deployment_settings.deploy_role_name == INIT_PLACEHOLDER_DEPLOY_ROLE_NAME:
        left.append(f"cats_deployment_settings.deploy_role_name is the placeholder '{INIT_PLACEHOLDER_DEPLOY_ROLE_NAME}' written by cats init")
    for keyname, account in config.accounts_under_test.items():
        if account.account_id == INIT_PLACEHOLDER_ACCOUNT_ID:
            left.append(f"accounts_under_test.{keyname}.account_id is the placeholder '{INIT_PLACEHOLDER_ACCOUNT_ID}' written by cats init")
    return left
