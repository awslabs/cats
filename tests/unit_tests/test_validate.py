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

"""Tests for cats.cli.validate module."""

import os
import subprocess
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.cli.validate import validate_config
from tests.utils import make_config, write_isolated_yaml


def test_unset_region_is_valid(isolated_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config loading and validation work without a Region; only commands that touch AWS require one."""
    # arrange
    monkeypatch.delenv("AWS_REGION", raising=False)
    config = make_config()
    del config["cats_deployment_settings"]["aws_region"]
    write_isolated_yaml(isolated_yaml, config)

    # act
    valid, _, _ = validate_config(isolated_yaml)

    # assert
    assert valid is True


def test_missing_file_returns_invalid(tmp_path: Path) -> None:
    # arrange
    missing_path = tmp_path / "nonexistent.yaml"
    expected_message = f"Config file not found: {missing_path}"

    # act
    valid, message, _ = validate_config(missing_path)

    # assert
    assert valid is False
    assert message == expected_message


def test_empty_file_returns_invalid(isolated_yaml: Path) -> None:
    # arrange
    isolated_yaml.write_text("")
    expected_message = "Config file is empty"

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_invalid_yaml_syntax_returns_invalid(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, "x: [unclosed")
    expected_message = rf"""YAML syntax error: while parsing a flow sequence
  in "{isolated_yaml}", line 1, column 4
expected ',' or ']', but got '<stream end>'
  in "{isolated_yaml}", line 1, column 13"""

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_invalid_account_id_returns_invalid(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, {"cats_deployment_settings": {"parent_account_id": "bad"}})
    expected_message = r"""1 validation error for CatsConfig
cats_deployment_settings.parent_account_id
  String should match pattern '^[0-9]{12}$' [type=string_pattern_mismatch, input_value='bad', input_type=str]
    For further information visit https://errors.pydantic.dev/2.13/v/string_pattern_mismatch"""

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_without_input_the_report_keeps_pydantics_layout_minus_the_value(isolated_yaml: Path) -> None:
    """Same title, count, location and message lines as pydantic's own report; no input_value, input_type or docs URL."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"accounts_under_test": {"prod": {"account_id": "not-an-account"}}})
    expected_message = r"""1 validation error for CatsConfig
accounts_under_test.prod.account_id
  String should match pattern '^[0-9]{12}$' [type=string_pattern_mismatch]"""

    # act
    valid, message, _ = validate_config(isolated_yaml, include_input=False)

    # assert
    assert valid is False
    assert message == expected_message


def test_unknown_field_returns_invalid(isolated_yaml: Path) -> None:
    """A top-level key with no near match is reported as unknown, without a guess."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"unknown_field": "value"})
    expected_message = """1 validation error for CatsConfig
  unknown key 'unknown_field' [type=unknown_key, input_value={'unknown_field': 'value'}, input_type=dict]"""

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_unknown_key_names_the_nearest_known_key(isolated_yaml: Path) -> None:
    """`region` under cats_deployment_settings is the most common first-run typo; the hint saves a trip to the schema."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"cats_deployment_settings": {"region": "us-east-1"}})
    expected_message = """1 validation error for CatsConfig
cats_deployment_settings
  unknown key 'region', did you mean 'aws_region'? [type=unknown_key, input_value={'region': 'us-east-1'}, input_type=dict]"""

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_unknown_nested_key_names_the_nearest_known_key(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(
        isolated_yaml,
        {"accounts_under_test": {"prod": {"account_id": "111111111111", "account_networks_under_test": {"private": {"vpc_id": "vpc-123"}}}}},
    )
    expected_message = """1 validation error for CatsConfig
accounts_under_test.prod
  unknown key 'account_networks_under_test', did you mean 'account_network_under_test'? [type=unknown_key, input_value={'account_id': '111111111... {'vpc_id': 'vpc-123'}}}, input_type=dict]"""  # noqa: E501 - pydantic prints the message on one line

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_several_unknown_keys_are_all_named(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, {"account_under_test": {}, "cats_deployment_setting": {}})
    expected_message = """1 validation error for CatsConfig
  unknown key 'account_under_test', did you mean 'accounts_under_test'?; unknown key 'cats_deployment_setting', did you mean 'cats_deployment_settings'? [type=unknown_key, input_value={'account_under_test': {}...deployment_setting': {}}, input_type=dict]"""  # noqa: E501 - pydantic prints the message on one line

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_list_root_document_returns_invalid(isolated_yaml: Path) -> None:
    """A YAML file whose root is a list passes the syntax gate; validate reports it in one line."""
    # arrange
    isolated_yaml.write_text("- accounts_under_test\n")
    expected_message = "Config file must be a YAML mapping of top-level sections (cats_deployment_settings, accounts_under_test, ...), got a list"

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_scalar_root_document_returns_invalid(isolated_yaml: Path) -> None:
    # arrange
    isolated_yaml.write_text("just a string\n")
    expected_message = "Config file must be a YAML mapping of top-level sections (cats_deployment_settings, accounts_under_test, ...), got a str"

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_directory_path_returns_invalid(tmp_path: Path) -> None:
    """'not found' would send the user looking for a typo in a path that exists."""
    # arrange
    expected_message = f"Config path is a directory, expected a cats.yaml file: {tmp_path}"

    # act
    valid, message, _ = validate_config(tmp_path)

    # assert
    assert valid is False
    assert message == expected_message


def test_removed_junit_enabled_true_names_the_replacement(isolated_yaml: Path) -> None:
    """A config from before junit became a report format is told the fix, not just 'extra input'."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"reporting": {"junit_enabled": True}})
    expected_message = """1 validation error for CatsConfig
reporting
  Value error, reporting.junit_enabled has been removed: junit.xml is the 'junit' report format. Delete junit_enabled and add junit to reporting.report_formats, e.g. report_formats: [cats_json, junit] [type=value_error, input_value={'junit_enabled': True}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/value_error"""  # noqa: E501 - pydantic prints the message on one line

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_removed_junit_enabled_false_says_delete_it(isolated_yaml: Path) -> None:
    """Older `cats init` files carry junit_enabled: false; that user is told to delete the key, not to add a format they never wanted."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"reporting": {"junit_enabled": False}})
    expected_message = """1 validation error for CatsConfig
reporting
  Value error, reporting.junit_enabled has been removed: delete it. junit.xml is the optional 'junit' report format; add junit to reporting.report_formats only if you want the file. [type=value_error, input_value={'junit_enabled': False}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/value_error"""  # noqa: E501 - pydantic prints the message on one line

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_securityhub_format_with_defaults_returns_valid(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, {"reporting": {"report_formats": ["cats_json", "securityhub"]}})

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert (valid, message) == (True, "Valid")


def test_securityhub_region_must_look_like_a_region(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, {"reporting": {"report_formats": ["securityhub"], "securityhub": {"aws_region": "EU-WEST-1"}}})
    expected_message = r"""1 validation error for CatsConfig
reporting.securityhub.aws_region
  String should match pattern '^[a-z]{2}[a-z0-9-]*-[0-9]+$' [type=string_pattern_mismatch, input_value='EU-WEST-1', input_type=str]
    For further information visit https://errors.pydantic.dev/2.13/v/string_pattern_mismatch"""

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_securityhub_failed_severity_must_be_an_asff_label(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, {"reporting": {"report_formats": ["securityhub"], "securityhub": {"failed_severity": "SEVERE"}}})
    expected_message = """1 validation error for CatsConfig
reporting.securityhub.failed_severity
  Input should be 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW' or 'INFORMATIONAL' [type=enum, input_value='SEVERE', input_type=str]
    For further information visit https://errors.pydantic.dev/2.13/v/enum"""

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert valid is False
    assert message == expected_message


def test_valid_config_returns_valid(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, make_config(accounts={"myaccount": "234567890123"}))

    # act
    valid, _, _ = validate_config(isolated_yaml)

    # assert
    assert valid is True


def test_valid_minimal_config_returns_valid(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, {})

    # act
    valid, _, _ = validate_config(isolated_yaml)

    # assert
    assert valid is True


def _targets(count: int, tcp_ports: list[int]) -> dict[str, dict[str, object]]:
    return {
        f"target{i}": {
            "vpc_id": "/cats/vpc",
            "subnet_ids": ["/cats/subnet"],
            "network_settings": {"tcp_listen_ports": tcp_ports, "udp_listen_ports": []},
        }
        for i in range(count)
    }


def test_oversized_template_returns_invalid_without_aws(isolated_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four both-handler targets at the 30-port cap render past the TemplateBody limit; validate says so from placeholders alone."""
    # arrange - SSM paths for every id, no credentials, no Region
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
    config = make_config(network_targets=_targets(4, list(range(8000, 8030))), enabled_network_handlers=["ec2", "lambda"])
    del config["cats_deployment_settings"]["aws_region"]
    write_isolated_yaml(isolated_yaml, config)
    expected_message = (
        "Template for 'testaccount' renders to 55,995 bytes; CloudFormation's TemplateBody limit is 51,200. "
        + "Reduce network targets, listen ports or enabled network handlers in this account, "
        + "or split its targets across a second account entry with the same account_id."
    )

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert (valid, message) == (False, expected_message)


def test_five_targets_return_valid_when_the_template_fits(isolated_yaml: Path) -> None:
    """There is no count cap on network targets; the rendered size is the bound."""
    # arrange
    write_isolated_yaml(isolated_yaml, make_config(network_targets=_targets(5, [8080])))

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert (valid, message) == (True, "Valid")


def test_network_targets_without_a_parent_account_return_valid(isolated_yaml: Path) -> None:
    """parent_account_id is required by deploy and show-template only; the size gate measures it at a placeholder."""
    # arrange
    config = make_config(network_targets=_targets(1, [8080]))
    del config["cats_deployment_settings"]["parent_account_id"]
    write_isolated_yaml(isolated_yaml, config)

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert (valid, message) == (True, "Valid")


def test_cli_exits_0_for_valid_config(isolated_yaml: Path, cli_runner: CliRunner) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, make_config(accounts={"myaccount": "234567890123"}))

    # act
    result = cli_runner.invoke(cats_cli, ["validate", "-c", str(isolated_yaml)])

    # assert
    expected_output = f"OK {isolated_yaml} is valid, {isolated_yaml.parent}/.schema.json regenerated\n"
    assert result.exit_code == 0
    assert result.stdout == expected_output


def test_cli_exits_2_for_invalid_config_with_the_report_on_stderr(isolated_yaml: Path, cli_runner: CliRunner) -> None:
    """Exit 2 is the caller's configuration; the report is a diagnostic (stderr) printed as text, so pydantic's [type=...] survives."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"unknown_field": "value"})
    expected_stderr = rf"""FAIL {isolated_yaml} is invalid, {isolated_yaml.parent}/.schema.json regenerated

1 validation error for CatsConfig
  unknown key 'unknown_field' [type=unknown_key, input_value={{'unknown_field': 'value'}}, input_type=dict]
"""

    # act
    result = cli_runner.invoke(cats_cli, ["validate", "-c", str(isolated_yaml)])

    # assert
    assert (result.exit_code, result.stdout, result.stderr) == (2, "", expected_stderr)


def test_cli_invalid_config_via_env_var_no_crash(isolated_yaml: Path) -> None:
    """Regression test: constants.py calls Settings() at import time, so an invalid config must not crash the CLI."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"unknown_field": "value"})
    env = os.environ.copy()
    env["CATS_CONFIG"] = str(isolated_yaml)

    # act
    result = subprocess.run(
        ["uv", "run", "cats", "validate"],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )

    # assert
    expected_stderr = f"""FAIL {isolated_yaml} is invalid, {isolated_yaml.parent}/.schema.json regenerated

1 validation error for CatsConfig
  unknown key 'unknown_field' [type=unknown_key, input_value={{'unknown_field': 'value'}}, input_type=dict]
"""
    assert (result.returncode, result.stdout, result.stderr) == (2, "", expected_stderr)


def test_explicit_path_ignores_invalid_working_directory_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit path validates that file alone: no merge with ./cats.yaml underneath."""
    # arrange
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "cats.yaml").write_text("unknown_field: value\n")
    explicit = tmp_path / "other" / "cats.yaml"
    explicit.parent.mkdir()
    write_isolated_yaml(explicit, make_config(accounts={"myaccount": "234567890123"}))

    # act (scoped so the CATS plugin's own teardown never reads the invalid ./cats.yaml)
    with monkeypatch.context() as m:
        m.chdir(cwd)
        m.delenv("CATS_CONFIG", raising=False)
        valid, message, _ = validate_config(explicit)

    # assert
    assert (valid, message) == (True, "Valid")


def test_cli_exits_2_when_the_schema_file_cannot_be_written(isolated_yaml: Path, cli_runner: CliRunner, mocker: MockerFixture) -> None:
    """A read-only project directory is the caller's environment: reported as a failure, not a PermissionError traceback."""
    # arrange
    write_isolated_yaml(isolated_yaml, make_config(accounts={"myaccount": "234567890123"}))
    mocker.patch("cats.cli.config.generate_schema", side_effect=PermissionError(13, "Permission denied", str(isolated_yaml.parent / ".schema.json")))

    # act
    result = cli_runner.invoke(cats_cli, ["validate", "-c", str(isolated_yaml)])

    # assert
    expected = (2, f"FAIL Could not write the schema file: [Errno 13] Permission denied: '{isolated_yaml.parent}/.schema.json'\n")
    assert (result.exit_code, result.stderr) == expected


def test_cli_does_not_blame_the_schema_when_cats_yaml_itself_cannot_be_read(
    isolated_yaml: Path, cli_runner: CliRunner, mocker: MockerFixture
) -> None:
    """Only the schema write is reported as a schema failure; an unreadable cats.yaml keeps its own error."""
    # arrange
    write_isolated_yaml(isolated_yaml, make_config(accounts={"myaccount": "234567890123"}))
    mocker.patch("cats.cli.config.validate_config", side_effect=PermissionError(13, "Permission denied", str(isolated_yaml)))

    # act
    result = cli_runner.invoke(cats_cli, ["validate", "-c", str(isolated_yaml)])

    # assert - the PermissionError about cats.yaml propagates, and nothing is reported as a CATS failure
    expected = (PermissionError, str(isolated_yaml), "")
    assert expected == (type(result.exception), getattr(result.exception, "filename", None), result.stderr)
