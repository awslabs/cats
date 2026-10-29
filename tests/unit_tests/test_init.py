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

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.cli.validate import validate_config


def test_init_without_args_exits_0(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # act
    result = cli_runner.invoke(cats_cli, ["init"])

    # assert
    expected_exit_code = 0
    assert result.exit_code == expected_exit_code


def test_init_creates_cats_yaml(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # act
    cli_runner.invoke(cats_cli, ["init"])

    # assert
    assert cats_yaml.exists() is True


def test_init_reports_every_file_it_creates_once(cats_yaml: Path, cli_runner: CliRunner) -> None:
    """One `OK Created` line per file, in the order they are written, and no second list of the same names."""
    # arrange
    project = cats_yaml.parent

    # act
    result = cli_runner.invoke(cats_cli, ["init"])

    # assert - the only lines naming a created file are these four; the old "Created: a, b, c" summary is gone
    expected = [
        f"OK Created {project / 'pytest.ini'}",
        f"OK Created {project / '.schema.json'}",
        f"OK Created {project / 'cats.yaml'}",
        f"OK Created example test at {project / 'tests' / 'example_test.py'}",
    ]
    assert [line for line in result.stdout.splitlines() if line.startswith(("OK ", "Created"))] == expected


def _expected_file(parent_account_id: str = "123456789012", deploy_role_name: str = "replace-me", aws_region: str = "us-east-1") -> str:
    """The minimal file cats init writes: the three deployment values, one placeholder account, the report directory."""
    return f"""# yaml-language-server: $schema=./.schema.json
cats_deployment_settings:
  parent_account_id: '{parent_account_id}'
  deploy_role_name: {deploy_role_name}
  aws_region: {aws_region}
accounts_under_test:
  myaccount:
    account_id: '234567890123'
reporting:
  report_dir: reports
  timestamped_files: true
"""


def test_init_with_parent_account_id_sets_value(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # arrange
    expected = _expected_file(parent_account_id="111122223333")

    # act
    cli_runner.invoke(cats_cli, ["init", "--parent-account-id", "111122223333"])

    # assert
    assert cats_yaml.read_text() == expected


def test_init_with_deploy_role_name_sets_value(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # arrange
    expected = _expected_file(deploy_role_name="AWSControlTowerExecution")

    # act
    cli_runner.invoke(cats_cli, ["init", "--deploy-role-name", "AWSControlTowerExecution"])

    # assert
    assert cats_yaml.read_text() == expected


def test_init_with_aws_region_sets_value(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # arrange
    expected = _expected_file(aws_region="eu-west-1")

    # act
    cli_runner.invoke(cats_cli, ["init", "--aws-region", "eu-west-1"])

    # assert
    assert cats_yaml.read_text() == expected


def test_init_without_aws_region_writes_the_environment_region(cats_yaml: Path, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS_REGION is the one environment fallback; init makes the choice visible in the file rather than in a hidden default."""
    # arrange
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    expected = _expected_file(aws_region="eu-west-1")

    # act
    cli_runner.invoke(cats_cli, ["init"])

    # assert
    assert cats_yaml.read_text() == expected


def test_init_without_flags_writes_the_minimum_that_validates(cats_yaml: Path, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """No nulls, no empty lists, no defaults spelled out: every key in the file is one the user chose or must edit."""
    # arrange
    monkeypatch.delenv("AWS_REGION", raising=False)

    # act
    cli_runner.invoke(cats_cli, ["init"])

    # assert
    assert cats_yaml.read_text() == _expected_file()


def test_init_output_is_valid(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # arrange
    cli_runner.invoke(cats_cli, ["init"])

    # act
    valid, message, _ = validate_config(cats_yaml)

    # assert
    assert (valid, message) == (True, "Valid")


def test_validate_warns_about_each_init_placeholder(cats_yaml: Path, cli_runner: CliRunner) -> None:
    """The file validates, and every placeholder cats init wrote is named on stderr: deploy fails at assume_role otherwise."""
    # arrange
    cli_runner.invoke(cats_cli, ["init"])
    expected_stderr = (
        "WARN cats_deployment_settings.parent_account_id is the placeholder '123456789012' written by cats init\n"
        + "WARN cats_deployment_settings.deploy_role_name is the placeholder 'replace-me' written by cats init\n"
        + "WARN accounts_under_test.myaccount.account_id is the placeholder '234567890123' written by cats init\n"
    )

    # act
    result = cli_runner.invoke(cats_cli, ["validate", "-c", str(cats_yaml)])

    # assert
    assert (result.exit_code, result.stderr) == (0, expected_stderr)


def test_validate_stays_silent_when_the_placeholders_are_replaced(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # arrange
    cli_runner.invoke(cats_cli, ["init", "--parent-account-id", "111122223333", "--deploy-role-name", "AWSControlTowerExecution"])
    cats_yaml.write_text(cats_yaml.read_text().replace("234567890123", "999999999999"))

    # act
    result = cli_runner.invoke(cats_cli, ["validate", "-c", str(cats_yaml)])

    # assert
    assert (result.exit_code, result.stderr) == (0, "")


def test_init_succeeds_on_an_ascii_stdout(tmp_path: Path) -> None:
    """A non-UTF-8 stdout gets exit 0 and every file: the banner carries no glyph it cannot encode."""
    # arrange
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "ascii"
    env.pop("CATS_CONFIG", None)

    # act
    result = subprocess.run(  # noqa: S603 - argv is a fixed command plus pytest's own tmp_path
        ["uv", "run", "cats", "init", "-o", str(tmp_path)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )

    # assert
    expected = (0, ["OK Created example test", "CATS init complete!"])
    assert expected == (result.returncode, [line[:23] for line in result.stdout.splitlines() if line.startswith(("OK Created example", "CATS init"))])


def test_init_when_cats_yaml_exists_is_a_noop(cats_yaml: Path, cli_runner: CliRunner) -> None:
    """Re-running init must never destroy a hand-edited config."""
    # arrange
    hand_edited = "accounts_under_test: {}\n"
    cats_yaml.write_text(hand_edited)

    # act
    result = cli_runner.invoke(cats_cli, ["init"])

    # assert
    expected = (0, hand_edited, f"{cats_yaml} already exists, nothing written. Use --force to overwrite.\n")
    assert expected == (result.exit_code, cats_yaml.read_text(), result.stdout)


def test_init_with_force_overwrites_existing_cats_yaml(cats_yaml: Path, cli_runner: CliRunner) -> None:
    # arrange
    cats_yaml.write_text("accounts_under_test: {}\n")

    # act
    result = cli_runner.invoke(cats_cli, ["init", "--force"])

    # assert
    expected_first_line = "# yaml-language-server: $schema=./.schema.json"
    assert (0, expected_first_line) == (result.exit_code, cats_yaml.read_text().splitlines()[0])


def test_init_with_force_says_it_overwrote_the_config(cats_yaml: Path, cli_runner: CliRunner) -> None:
    """A file that existed before the run was replaced, not created."""
    # arrange
    cats_yaml.write_text("accounts_under_test: {}\n")

    # act
    result = cli_runner.invoke(cats_cli, ["init", "--force"])

    # assert
    assert [line for line in result.stdout.splitlines() if line.endswith("cats.yaml")] == [f"OK Overwrote {cats_yaml}"]


def test_init_with_force_says_it_overwrote_a_schema_that_existed(cats_yaml: Path, cli_runner: CliRunner) -> None:
    """The same verb rule for the schema: a second init over a previous one replaces both files and says so for both."""
    # arrange
    cats_yaml.write_text("accounts_under_test: {}\n")
    schema = cats_yaml.parent / ".schema.json"
    schema.write_text("{}\n")

    # act
    result = cli_runner.invoke(cats_cli, ["init", "--force"])

    # assert
    expected_lines = [f"OK Overwrote {schema}", f"OK Overwrote {cats_yaml}"]
    assert [line for line in result.stdout.splitlines() if line.endswith((".schema.json", "cats.yaml"))] == expected_lines


def test_init_creates_missing_output_dir(tmp_path: Path, cli_runner: CliRunner) -> None:
    """-o pointing at a directory that does not exist must create it, not traceback."""
    # arrange
    missing_dir = tmp_path / "missing" / "nested"

    # act
    result = cli_runner.invoke(cats_cli, ["init", "-o", str(missing_dir)])

    # assert
    expected = (0, True)
    assert expected == (result.exit_code, (missing_dir / "cats.yaml").exists())


def test_init_with_unreadable_pyproject_writes_nothing_and_exits_2(tmp_path: Path, cli_runner: CliRunner) -> None:
    """An unreadable pyproject.toml stops init before it writes anything."""
    # arrange
    (tmp_path / "pyproject.toml").write_text('[project\nname = "controls"\n')

    # act
    result = cli_runner.invoke(cats_cli, ["init", "-o", str(tmp_path)])

    # assert
    expected = (2, ["pyproject.toml"])
    assert expected == (result.exit_code, sorted(path.name for path in tmp_path.iterdir()))


def test_init_with_an_invalid_aws_region_is_a_usage_error_and_writes_nothing(tmp_path: Path, cli_runner: CliRunner) -> None:
    """--aws-region reaches CatsConfig unvalidated; it is refused before the first write, so a failed init leaves no file."""
    # arrange
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "controls"\n', encoding="utf-8")

    # act
    result = cli_runner.invoke(cats_cli, ["init", "-o", str(tmp_path), "--aws-region", "US-EAST-1"])

    # assert - exit 2, only the user's untouched pyproject.toml remains: no pytest pin appended, no .schema.json, no cats.yaml
    expected = (2, ["pyproject.toml"], '[project]\nname = "controls"\n', "FAIL cats init was given a value cats.yaml would not accept:")
    actual = (
        result.exit_code,
        sorted(path.name for path in tmp_path.iterdir()),
        (tmp_path / "pyproject.toml").read_text(encoding="utf-8"),
        result.output.splitlines()[0],
    )
    assert actual == expected


def test_init_with_an_invalid_deploy_role_name_is_a_usage_error_and_writes_nothing(tmp_path: Path, cli_runner: CliRunner) -> None:
    # act
    result = cli_runner.invoke(cats_cli, ["init", "-o", str(tmp_path), "--deploy-role-name", "arn:aws:iam::111111111111:role/x"])

    # assert
    expected: tuple[int, list[str], str] = (2, [], "FAIL cats init was given a value cats.yaml would not accept:")
    assert (result.exit_code, sorted(path.name for path in tmp_path.iterdir()), result.output.splitlines()[0]) == expected


def test_init_with_a_bad_argument_does_not_create_the_output_directory(tmp_path: Path, cli_runner: CliRunner) -> None:
    """-o names a directory that does not exist yet; a failed init must not leave an empty one behind."""
    # arrange
    new_dir = tmp_path / "controls"

    # act
    result = cli_runner.invoke(cats_cli, ["init", "-o", str(new_dir), "--aws-region", "US-EAST-1"])

    # assert
    assert (result.exit_code, new_dir.exists()) == (2, False)


def test_init_help_names_the_key_each_flag_sets() -> None:
    """Every `cats init` flag is named after the cats.yaml key it writes, and its help says so."""
    # arrange
    cli_runner_narrow = CliRunner(env={"COLUMNS": "120", "NO_COLOR": "1"})
    expected_output = """\
                                                                                                                        
 Usage: root init [OPTIONS]                                                                                             
                                                                                                                        
 Bootstrap a new cats.yaml configuration with other basic files required.                                               
                                                                                                                        
 Safe to re-run: when cats.yaml already exists this is a no-op unless --force is given.                                 
                                                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --parent-account-id          <str>   Sets cats_deployment_settings.parent_account_id. The 12-digit AWS account ID    │
│                                      trusted to assume test roles, where CATS will execute tests from. CATS creates  │
│                                      test roles in declared target accounts under test that trust this account.      │
│                                      Written as the placeholder 123456789012 when omitted.                           │
│ --deploy-role-name           <str>   Sets cats_deployment_settings.deploy_role_name. The name of an IAM role that    │
│                                      already exists in your target accounts, which 'cats infra' commands will assume │
│                                      to deploy test infrastructure. Common examples: AWSControlTowerExecution,       │
│                                      OrganizationAccountAccessRole. Written as the placeholder replace-me when       │
│                                      omitted.                                                                        │
│ --aws-region                 <str>   Sets cats_deployment_settings.aws_region. The Region CATS deploys its stacks    │
│                                      into and connects its test sessions to. When omitted, the AWS_REGION            │
│                                      environment variable is written if set, else the placeholder us-east-1.         │
│ --output-dir         -o      <path>  Directory for generated files. Created if missing. Defaults to config file's    │
│                                      directory.                                                                      │
│ --force                              Overwrite an existing cats.yaml.                                                │
│ --help                               Show this message and exit.                                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯

"""  # noqa: W291, W293

    # act
    result = cli_runner_narrow.invoke(cats_cli, ["init", "--help"])

    # assert
    assert result.output == expected_output
