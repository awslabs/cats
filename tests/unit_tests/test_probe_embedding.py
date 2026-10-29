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

"""The single probe is embedded once and both transports run exactly that source.

Replaces test_handler_parity.py: with one probe file feeding the Lambda ZipFile and
the SSM document heredoc through the same FindInMap key, parity is structural. These
tests pin the structure, the size budgets, and the Fn::Sub safety of the embedded files.
"""

from pathlib import Path
from typing import Any

import boto3
import pytest
from pydantic import ValidationError

from cats.cfn.render import LAMBDA_ZIPFILE_LIMIT, _without_license_header, render_template
from cats.cfn.resolve import ResolvedAccountSettings, resolve_account
from cats.config import CatsConfig
from tests.utils import load_cfn_template, make_cats_config, make_config

ACCOUNT_NAME = "testaccount"
EC2_SERVER_DIR = Path(__file__).parents[2] / "cats" / "cfn" / "ec2_server"
TEMPLATES_DIR = Path(__file__).parents[2] / "cats" / "cfn" / "templates"
EC2_USERDATA_LIMIT = 16384
PROBE_MAPPING_KEY = ["CatsEmbeddedCode", "code", "probe"]


def _render(config: CatsConfig) -> dict[str, Any]:
    account = config.accounts_under_test[ACCOUNT_NAME]
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    settings = ResolvedAccountSettings(
        additional_roles={},
        default_test_role_managed_policies=[],
        default_test_role_inline_policies={},
        fixture_scope="session",
        allow_shell=False,
    )
    return load_cfn_template(render_template(resolved, config.cats_deployment_settings, settings, securityhub_region=None))


@pytest.fixture
def both_handlers_template(mock_aws_infra: dict[str, str]) -> dict[str, Any]:
    """One target with lambda+ec2: both transports present."""
    config = make_cats_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        enabled_network_handlers=["lambda", "ec2"],
    )
    return _render(config)


def _resources_of_type(template: dict[str, Any], resource_type: str) -> dict[str, Any]:
    return {name: r for name, r in template["Resources"].items() if r["Type"] == resource_type}


# =============================================================================
# One source, embedded once
# =============================================================================


def test_mapping_holds_the_probe_source_verbatim_minus_license_header(both_handlers_template: dict[str, Any]) -> None:
    # arrange
    expected = _without_license_header((EC2_SERVER_DIR / "probe.py").read_text())

    # act
    embedded = both_handlers_template["Mappings"]["CatsEmbeddedCode"]["code"]["probe"]

    # assert
    assert embedded == expected


def test_embedded_probe_is_under_the_lambda_zipfile_limit(both_handlers_template: dict[str, Any]) -> None:
    # act
    size = len(both_handlers_template["Mappings"]["CatsEmbeddedCode"]["code"]["probe"])

    # assert
    assert size <= LAMBDA_ZIPFILE_LIMIT, f"embedded probe is {size} chars, ZipFile limit is {LAMBDA_ZIPFILE_LIMIT}"


def test_lambda_zipfile_and_ssm_heredoc_resolve_the_same_mapping_key(both_handlers_template: dict[str, Any]) -> None:
    # act
    zipfile = both_handlers_template["Resources"]["ProbeLambdaprivateSubnet"]["Properties"]["Code"]["ZipFile"]
    run_command = both_handlers_template["Resources"]["CatsProbeDocument"]["Properties"]["Content"]["mainSteps"][0]["inputs"]["runCommand"][0]
    heredoc_variables = run_command["Sub"][1]

    # assert
    expected = {"lambda": {"FindInMap": PROBE_MAPPING_KEY}, "ssm": {"Probe": {"FindInMap": PROBE_MAPPING_KEY}}}
    assert {"lambda": zipfile, "ssm": heredoc_variables} == expected


def test_ssm_document_runs_the_probe_from_a_quoted_heredoc_on_stdin(both_handlers_template: dict[str, Any]) -> None:
    # act
    run_command = both_handlers_template["Resources"]["CatsProbeDocument"]["Properties"]["Content"]["mainSteps"][0]["inputs"]["runCommand"][0]

    # assert - quoting the terminator keeps the body verbatim (no shell expansion of the probe)
    expected = "python3 - <<'CATSPROBE'\n${Probe}\nCATSPROBE\n"
    assert run_command["Sub"][0] == expected


def test_ssm_document_parameters(both_handlers_template: dict[str, Any]) -> None:
    # act
    parameters = both_handlers_template["Resources"]["CatsProbeDocument"]["Properties"]["Content"]["parameters"]

    # assert - allowedValues makes SSM reject a bad protocol at send_command; TargetMessage defaults empty (None in the probe)
    expected = {
        "Protocol": {"type": "String", "interpolationType": "ENV_VAR", "allowedValues": ["http", "https", "tcp", "udp"]},
        "TargetUrl": {"type": "String", "interpolationType": "ENV_VAR"},
        "TargetPort": {"type": "String", "interpolationType": "ENV_VAR"},
        "TargetMessage": {"type": "String", "interpolationType": "ENV_VAR", "default": ""},
    }
    assert parameters == expected


def test_ssm_document_is_named_probe(both_handlers_template: dict[str, Any]) -> None:
    # act
    name = both_handlers_template["Resources"]["CatsProbeDocument"]["Properties"]["Name"]

    # assert
    assert name == "CATS-test-infra-testaccount-Probe"


def test_test_role_may_send_only_the_probe_document(both_handlers_template: dict[str, Any]) -> None:
    # act
    statements = both_handlers_template["Resources"]["CatsTestRolePolicy"]["Properties"]["PolicyDocument"]["Statement"]
    document_arns = next(statement for statement in statements if statement["Sid"] == "SsmSendCommandDocuments")["Resource"]

    # assert
    expected = [{"Sub": "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:document/CATS-test-infra-testaccount-Probe"}]
    assert document_arns == expected


# =============================================================================
# Fn::Sub safety of every embedded file
# =============================================================================


@pytest.mark.parametrize(
    "path",
    [
        EC2_SERVER_DIR / "probe.py",
        EC2_SERVER_DIR / "udpserver.py",
        EC2_SERVER_DIR / "tcpserver.py",
        EC2_SERVER_DIR / "tcpserver.service",
        EC2_SERVER_DIR / "udpserver.service",
        TEMPLATES_DIR / "userdata.sh.j2",
    ],
    ids=lambda p: str(p.name),
)
def test_embedded_file_contains_no_fn_sub_variable_syntax(path: Path) -> None:
    # arrange - every embedded file reaches the instance or the Lambda through Fn::Sub, which would try to resolve any `${...}`
    source = path.read_text()

    # act & assert
    assert "${" not in source


# =============================================================================
# Resource cardinality: one Lambda per target, one document per account, no EventInvokeConfig
# =============================================================================


def test_one_lambda_function_per_lambda_enabled_target(mock_aws_infra: dict[str, str]) -> None:
    # arrange - two lambda targets, one ec2-only target
    config = make_cats_config(
        network_targets={
            "alpha": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]},
            "beta": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"]]},
            "gamma": {
                "vpc_id": mock_aws_infra["VPC2"],
                "subnet_ids": [mock_aws_infra["SUBNET2_A"]],
                "network_settings": {"enabled_network_handlers": ["ec2"]},
            },
        },
        enabled_network_handlers=["lambda", "ec2"],
    )

    # act
    functions = _resources_of_type(_render(config), "AWS::Lambda::Function")

    # assert
    expected = {
        "ProbeLambdaalpha": "CATS-test-infra-testaccount-probe-alpha",
        "ProbeLambdabeta": "CATS-test-infra-testaccount-probe-beta",
    }
    assert {name: r["Properties"]["FunctionName"] for name, r in functions.items()} == expected


def test_no_event_invoke_config_is_rendered(both_handlers_template: dict[str, Any]) -> None:
    # act - every CATS invoke is RequestResponse; EventInvokeConfig governs async retries only
    configs = _resources_of_type(both_handlers_template, "AWS::Lambda::EventInvokeConfig")

    # assert
    assert configs == {}


def test_one_ssm_document_per_ec2_enabled_account(mock_aws_infra: dict[str, str]) -> None:
    # arrange - two ec2 targets share the account's single document; the instance is chosen at send_command
    config = make_cats_config(
        network_targets={
            "alpha": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]},
            "beta": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_B"]]},
        },
        enabled_network_handlers=["ec2"],
    )

    # act
    documents = _resources_of_type(_render(config), "AWS::SSM::Document")

    # assert
    assert list(documents) == ["CatsProbeDocument"]


def test_lambda_only_account_renders_no_ssm_document_but_keeps_the_probe_mapping(mock_aws_infra: dict[str, str]) -> None:
    # arrange
    config = make_cats_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        enabled_network_handlers=["lambda"],
    )

    # act
    template = _render(config)

    # assert
    expected = {"documents": {}, "probe_embedded": True}
    result = {
        "documents": _resources_of_type(template, "AWS::SSM::Document"),
        "probe_embedded": "probe" in template["Mappings"]["CatsEmbeddedCode"]["code"],
    }
    assert result == expected


def test_no_handlers_is_rejected_before_rendering(mock_aws_infra: dict[str, str]) -> None:
    """A declared target exists to be probed: an empty handler list is rejected by the config, never rendered."""
    # arrange
    config_dict = make_config(
        network_targets={"privateSubnet": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
    )
    config_dict["global_network_settings"]["enabled_network_handlers"] = []

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(config_dict)

    # assert
    expected = [("global_network_settings", "enabled_network_handlers")]
    assert [error["loc"] for error in exc_info.value.errors()] == expected


def test_one_probe_lambda_name_output_per_lambda_target(both_handlers_template: dict[str, Any]) -> None:
    # act
    lambda_outputs = {k: v for k, v in both_handlers_template["Outputs"].items() if "Lambda" in k}

    # assert
    expected = {"ProbeLambdaNameprivateSubnet": {"Value": {"Ref": "ProbeLambdaprivateSubnet"}}}
    assert lambda_outputs == expected


# =============================================================================
# Userdata budget (hygiene: userdata is unchanged by the probe design)
# =============================================================================


def test_resolved_ec2_userdata_is_under_the_limit(both_handlers_template: dict[str, Any]) -> None:
    # arrange - resolve Fn::Sub by hand: the shared script from the mapping, worst-case-length values for the two Refs
    code = both_handlers_template["Mappings"]["CatsEmbeddedCode"]["code"]
    sub = both_handlers_template["Resources"]["Ec2ListenerprivateSubnet"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    prefix, variables = sub[0], sub[1]

    # act
    resolved = prefix.replace("${Userdata}", code[variables["Userdata"]["FindInMap"][2]])
    resolved = resolved.replace("${CatsResponseBucket}", "x" * 63)  # 63 = max S3 bucket name length
    resolved = resolved.replace("${CatsUdpReceiptToken}", "f" * 32)  # the token's AllowedPattern is 32 hex characters

    # assert
    size = len(resolved.encode("utf-8"))
    assert ("${" in resolved, size <= EC2_USERDATA_LIMIT) == (False, True), (
        f"resolved userdata is {size:,} bytes, EC2 limit is {EC2_USERDATA_LIMIT:,}"
    )
