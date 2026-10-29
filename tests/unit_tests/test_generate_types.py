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

"""Tests for cats.cli.generate_types module."""

import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import pytest
from pytest_mock import MockerFixture
from syrupy.assertion import SnapshotAssertion
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.cli.generate_types import TEMPLATE, _to_class_name, generate_types
from cats.exceptions import CatsConfigError
from cats.resources.account_under_test import AccountUnderTest
from cats.resources.network_interact import NetworkHandler
from example_cats_project.cats_types import CatsAccountProtocol, NetworkTargetProtocol
from tests.utils import make_config, normalize_config_path, write_config


def _static_check_account_conforms_to_protocol(account: AccountUnderTest) -> CatsAccountProtocol:
    """mypy --strict gate: AccountUnderTest must structurally satisfy the generated CatsAccountProtocol.

    Never called at runtime. Fails type checking if the generated protocol drifts from
    the runtime class - e.g. wrong variance on network_handlers or a missing property.
    """
    return account


def _static_check_handler_conforms_to_protocol(handler: NetworkHandler) -> NetworkTargetProtocol:
    """mypy --strict gate: NetworkHandler must structurally satisfy the generated NetworkTargetProtocol.

    Never called at runtime. Fails type checking if the protocol declares a member the
    runtime class lacks or types differently. A member missing from the protocol passes
    here; test_network_target_protocol_covers_the_handlers_public_surface catches that.
    """
    return handler


def test_to_class_name_simple() -> None:
    assert _to_class_name("audit") == "AuditAccount"


def test_to_class_name_with_underscores() -> None:
    assert _to_class_name("shared_services") == "SharedServicesAccount"


def test_to_class_name_with_hyphens() -> None:
    assert _to_class_name("log-archive") == "LogArchiveAccount"


def test_keynames_mapping_to_one_class_are_a_config_error(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """my_account, my__account and MyAccount all PascalCase to MyAccountAccount: a mypy no-redef and a silent last-wins."""
    # arrange
    write_config(cats_yaml, make_config(accounts={"my_account": "111111111111", "my__account": "222222222222", "MyAccount": "333333333333"}))
    monkeypatch.chdir(tmp_path)
    expected_message = (
        "account keynames 'MyAccount', 'my__account', 'my_account' all map to the generated class 'MyAccountAccount' "
        + "(the keyname in PascalCase with '_' and '-' removed, plus 'Account'); rename one so each account gets its own class."
    )

    # act
    with pytest.raises(CatsConfigError) as exc_info:
        generate_types()

    # assert
    assert (str(exc_info.value), (tmp_path / "cats_types.py").exists()) == (expected_message, False)


def test_generates_single_account(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_py: SnapshotAssertion) -> None:
    """Single account with network target generates correct output."""
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            network_targets={"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"]}},
        ),
    )
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    output = normalize_config_path((tmp_path / "cats_types.py").read_text())
    assert output == snapshot_py


def test_generates_with_additional_roles(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_py: SnapshotAssertion) -> None:
    """Additional roles appear in CatsRoleHandler."""
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="myaccount",
            additional_roles={"readonly": {"managed_policies": ["ReadOnlyAccess"]}},
        ),
    )
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    output = normalize_config_path((tmp_path / "cats_types.py").read_text())
    assert output == snapshot_py


def test_protocol_matches_account_under_test() -> None:
    """Generated Protocol should have same base attributes as AccountUnderTest."""
    # Get attributes from __init__ signature + properties
    # arrange
    init_params = set(inspect.signature(AccountUnderTest.__init__).parameters.keys()) - {"self"}
    properties = {name for name, val in inspect.getmembers(AccountUnderTest) if isinstance(val, property)}
    expected_init_params = {"name"}
    expected_properties = {"session", "role", "available_network_targets", "network_handlers", "tags", "udp_receipt_token"}

    # assert
    assert init_params == expected_init_params, f"AccountUnderTest.__init__ changed: {init_params}"
    assert properties == expected_properties, f"AccountUnderTest properties changed: {properties}"


def test_account_class_subclasses_base_protocol(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-account protocols inherit from CatsAccountProtocol, so functions typed as CatsAccountProtocol accept any account."""
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            network_targets={"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"]}},
        ),
    )
    monkeypatch.chdir(tmp_path)
    generate_types()

    # act
    spec = importlib.util.spec_from_file_location("generated_cats_types", tmp_path / "cats_types.py")
    assert spec is not None
    assert spec.loader is not None
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)

    # assert
    expected_bases = (generated.CatsAccountProtocol, Protocol)
    assert generated.AuditAccount.__bases__ == expected_bases


def test_generates_with_no_handlers(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_py: SnapshotAssertion) -> None:
    """No enabled handlers omits lambda methods from Protocol."""
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            network_targets={"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"]}},
            enabled_network_handlers=[],
        ),
    )
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    output = normalize_config_path((tmp_path / "cats_types.py").read_text())
    assert output == snapshot_py


def test_generates_with_tags(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_py: SnapshotAssertion) -> None:
    """Tags on accounts and networks appear in docstrings."""
    # arrange
    config = make_config(account_name="prod")
    config["accounts_under_test"]["prod"]["tags"] = {"environment": "production", "pci": None}
    config["accounts_under_test"]["prod"]["account_network_under_test"] = {
        "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"], "tags": {"zone": "internal", "restricted": None}},
        "public": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-789"], "tags": {}},
    }
    write_config(cats_yaml, config)
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    output = normalize_config_path((tmp_path / "cats_types.py").read_text())
    assert output == snapshot_py


def test_shell_method_in_protocol_when_allow_shell_true(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any account with allow_shell: true makes shell() appear in NetworkTargetProtocol."""
    # arrange
    config = make_config(
        account_name="audit",
        network_targets={
            "private": {
                "vpc_id": "vpc-123",
                "subnet_ids": ["subnet-456"],
            },
        },
        account_settings={"allow_shell": True},
    )
    write_config(cats_yaml, config)
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    output = (tmp_path / "cats_types.py").read_text()
    expected_signature = "def shell(self, command: str | list[str]) -> ShellResult: ..."
    expected_import = "from cats.constants import ShellResult"
    assert expected_signature in output
    assert expected_import in output


def test_shell_method_absent_when_allow_shell_false(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No target with allow_shell: true means shell() is absent from the Protocol."""
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            network_targets={"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"]}},
        ),
    )
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    output = (tmp_path / "cats_types.py").read_text()
    assert "def shell(" not in output
    assert "ShellResult" not in output


def test_generate_types_hints_at_the_types_extra_when_stubs_are_absent(
    cats_yaml: Path,
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(account_name="myaccount"))
    mocker.patch("cats.cli.config._boto3_stubs_installed", return_value=False)
    expected_hint = "For typed boto3 clients (`.client('sts')` autocomplete and mypy), install the stubs extra: pip install 'aws-cats[types]'"

    # act
    result = cli_runner.invoke(cats_cli, ["generate-types", "--output-dir", str(tmp_path)])

    # assert
    assert (result.exit_code, expected_hint in result.stdout.splitlines()) == (0, True)


def test_generate_types_prints_no_hint_when_stubs_are_installed(
    cats_yaml: Path,
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """The dev environment carries boto3-stubs, so the real check exercises the installed branch."""
    # arrange
    write_config(cats_yaml, make_config(account_name="myaccount"))

    # act
    result = cli_runner.invoke(cats_cli, ["generate-types", "--output-dir", str(tmp_path)])

    # assert
    expected_lines = 1  # only the "Generated <path>" line
    assert (result.exit_code, len(result.stdout.strip().splitlines())) == (0, expected_lines)


def test_generate_types_without_a_config_file_exits_2_and_writes_nothing(cats_yaml: Path, cli_runner: CliRunner, tmp_path: Path) -> None:
    """An absent cats.yaml loads as an empty configuration; a cats_types.py with no account classes would be the wrong answer, not a result."""
    # arrange - CATS_CONFIG names a file nobody wrote

    # act
    result = cli_runner.invoke(cats_cli, ["generate-types", "--output-dir", str(tmp_path)])

    # assert
    assert (result.exit_code, result.stderr, (tmp_path / "cats_types.py").exists()) == (2, f"FAIL Config file not found: {cats_yaml}\n", False)


# =============================================================================
# User text lands inside generated docstrings and must never break the file
# =============================================================================


def test_tag_with_triple_quotes_and_backslash_still_compiles(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag value is user text; three quotes would end the docstring and a backslash starts an escape."""
    # arrange
    config = make_config(account_name="prod")
    config["accounts_under_test"]["prod"]["tags"] = {"note": 'ends with """ and C:\\Users\\path'}
    config["accounts_under_test"]["prod"]["account_network_under_test"] = {
        "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"], "tags": {"k": 'v"""'}},
    }
    write_config(cats_yaml, config)
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()
    namespace: dict[str, object] = {}
    exec(compile((tmp_path / "cats_types.py").read_text(), "cats_types.py", "exec"), namespace)  # noqa: S102 - the generated file is the thing under test

    # assert - the text survives, verbatim, inside the docstring
    prod_account = namespace["ProdAccount"]
    assert isinstance(prod_account, type)
    docstring = prod_account.__doc__
    assert docstring is not None
    assert ('note: ends with """ and C:\\Users\\path' in docstring, 'private [tags: k=v"""]' in docstring) == (True, True)


def test_windows_config_path_in_the_module_docstring_compiles() -> None:
    """`C:\\Users` holds a `\\U`, which a non-raw string reads as a unicode escape."""
    # arrange
    account = SimpleNamespace(account_id="111111111111", tags={}, account_network_under_test={})

    # act
    output = TEMPLATE.render(
        config_path="C:\\Users\\me\\cats.yaml",
        role_names=[],
        accounts={"prod": account},
        to_class_name=_to_class_name,
        enabled_handlers=["ec2"],
        allow_shell=False,
    )
    namespace: dict[str, object] = {}
    exec(compile(output, "cats_types.py", "exec"), namespace)  # noqa: S102 - the generated file is the thing under test

    # assert
    expected = "Account fixture types generated from C:\\Users\\me\\cats.yaml.\n\nGenerated by `cats generate-types`. Do not edit manually.\n"
    assert namespace["__doc__"] == expected


def _protocol_methods(output: str, class_name: str, prefix: str = "") -> list[str]:
    """The method names declared on one generated Protocol class, in file order."""
    body = output.split(f"class {class_name}(Protocol):")[1].split("\n\n\nclass ")[0]
    return [line.split("(")[0].removeprefix("    def ") for line in body.splitlines() if line.startswith(f"    def {prefix}")]


def test_lambda_methods_appear_when_only_a_target_enables_lambda(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The global block says ec2 only; a target override adds lambda, so the protocol must carry the lambda methods."""
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            network_targets={
                "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"], "network_settings": {"enabled_network_handlers": ["ec2", "lambda"]}}
            },
        ),
    )
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    expected = ["lambda_http_call", "lambda_https_call", "lambda_tcp_call", "lambda_udp_call"]
    assert _protocol_methods((tmp_path / "cats_types.py").read_text(), "NetworkTargetProtocol", "lambda_") == expected


def test_role_declared_on_one_account_appears_on_the_role_handler(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Roles come from every account's resolved settings, not only global_account_settings."""
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            network_targets={"private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"]}},
            account_settings={"additional_roles": {"auditor": {"managed_policies": ["SecurityAudit"]}}},
        ),
    )
    monkeypatch.chdir(tmp_path)

    # act
    generate_types()

    # assert
    assert _protocol_methods((tmp_path / "cats_types.py").read_text(), "CatsRoleHandler") == ["default", "auditor"]


def test_network_target_protocol_covers_the_handlers_public_surface(cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With every handler and shell enabled, the protocol names each public member of NetworkHandler.

    The static gate above only proves the runtime class satisfies the protocol; a member
    left off the template (as ``name`` once was) still passes it. This closes that gap.
    """
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            network_targets={
                "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-456"], "network_settings": {"enabled_network_handlers": ["ec2", "lambda"]}}
            },
            account_settings={"allow_shell": True},
        ),
    )
    monkeypatch.chdir(tmp_path)
    generate_types()
    handler_public_members = {name for name, _ in inspect.getmembers(NetworkHandler) if not name.startswith("_")} | {"name", "tags"}

    # act
    spec = importlib.util.spec_from_file_location("generated_cats_types", tmp_path / "cats_types.py")
    assert spec is not None
    assert spec.loader is not None
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)
    protocol = generated.NetworkTargetProtocol
    protocol_public_members = {name for name in vars(protocol) if not name.startswith("_")} | set(protocol.__annotations__)

    # assert
    assert protocol_public_members == handler_public_members


def test_roles_named_after_template_identifiers_do_not_break_the_generated_module(
    cats_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`property` and `Session` are valid role names; the generated class must still decorate and type the roles after them.

    `property` breaks at import time; `Session` only under mypy (class scope resolves the role before the import), so both are checked.
    """
    # arrange
    write_config(
        cats_yaml,
        make_config(
            account_name="audit",
            account_settings={
                "additional_roles": {
                    "property": {"managed_policies": ["SecurityAudit"]},
                    "Session": {"managed_policies": ["SecurityAudit"]},
                    "readonly": {"managed_policies": ["ReadOnlyAccess"]},
                }
            },
        ),
    )
    monkeypatch.chdir(tmp_path)
    generate_types()
    generated_file = tmp_path / "cats_types.py"

    # act
    spec = importlib.util.spec_from_file_location("generated_cats_types", generated_file)
    assert spec is not None
    assert spec.loader is not None
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)
    # cwd is tmp_path, so mypy sees no pyproject.toml; a persistent cache keeps every run after the first warm.
    cache_dir = Path(__file__).resolve().parents[2] / ".mypy_cache" / "generated-types"
    mypy = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mypy", "--strict", "--cache-dir", str(cache_dir), str(generated_file)],
        capture_output=True,
        text=True,
        check=False,
    )

    # assert - every role is a property on the handler, and the file type-checks
    handler = generated.CatsRoleHandler
    expected = ((True, True, True, True), 0)
    assert expected == (
        tuple(isinstance(vars(handler)[name], property) for name in ("default", "property", "Session", "readonly")),
        mypy.returncode,
    ), mypy.stdout
