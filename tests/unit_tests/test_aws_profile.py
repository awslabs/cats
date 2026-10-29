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

"""Tests for cats.utils.aws_profile.write_cats_profile."""

import configparser
import os
import stat
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.credentials import AssumeRoleWithWebIdentityProvider, EnvProvider, SharedCredentialProvider
from pydantic import TypeAdapter
from pytest_mock import MockerFixture

from cats.exceptions import CatsConfigError, CatsUsageError
from cats.utils.aws_profile import WriteProfileResult, _splice_section, write_cats_profile
from tests.utils import make_config, write_config

DEFAULT_ROLE_ARN = "arn:aws:iam::111111111111:role/CATS-accA-default-test-role"
READONLY_ROLE_ARN = "arn:aws:iam::111111111111:role/CATS-accA-readonly"


def _role_arn_for(attr: str) -> str:
    return DEFAULT_ROLE_ARN if attr == "default" else f"arn:aws:iam::111111111111:role/CATS-accA-{attr}"


@pytest.fixture
def mock_parent_session(mocker: MockerFixture) -> MagicMock:
    """Patch attributed_session as imported inside cats.utils.aws_profile: this process runs as the named profile `parent`."""
    mock: MagicMock = mocker.patch("cats.utils.aws_profile.attributed_session")
    session = mock.return_value
    session.profile_name = "parent"
    session.get_credentials.return_value.method = SharedCredentialProvider.METHOD
    return mock


@pytest.fixture
def mock_get_account(mocker: MockerFixture, mock_parent_session: MagicMock) -> MagicMock:
    """Patch get_account and Settings as imported inside cats.utils.aws_profile.

    cats.yaml (Settings) knows account `accA` with the additional role `readonly`; acc.role derives
    the ARNs for `default` and `readonly`; acc.aws_region set. override_config_path is a no-op.
    """
    mocker.patch("cats.utils.aws_profile.override_config_path", return_value=nullcontext())
    account_config = MagicMock()
    account_config.account_settings.additional_roles = {"readonly": MagicMock()}
    mocker.patch("cats.utils.aws_profile.Settings").return_value.accounts_under_test = {"accA": account_config}
    mock: MagicMock = mocker.patch("cats.utils.aws_profile.get_account")
    acc = mock.return_value
    acc.aws_region = "us-east-1"
    acc.role = MagicMock(spec=["default", "readonly", "_role_arn_for"])
    acc.role._role_arn_for.side_effect = _role_arn_for
    return mock


def _read_section(path: Path, name: str) -> dict[str, str]:
    parser = configparser.RawConfigParser()
    parser.read(path)
    return dict(parser[name])


def test_default_profile_name_is_cats_account_role(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"

    # act
    result = write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    expected = {
        "profile_name": "cats-accA-default",
        "profile_file": str(profile_file),
        "role_arn": DEFAULT_ROLE_ARN,
        "parent": "source_profile = parent",
    }
    assert result == expected


def test_custom_profile_name_is_used(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"

    # act
    write_cats_profile("accA", "default", "cats-custom", profile_file, None)

    # assert
    expected = {
        "role_arn": DEFAULT_ROLE_ARN,
        "source_profile": "parent",
        "role_session_name": "cats-profile",
        "region": "us-east-1",
    }
    assert _read_section(profile_file, "profile cats-custom") == expected


def test_custom_profile_file_is_written(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "elsewhere" / "config"

    # act
    result = write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert (result["profile_file"], profile_file.exists()) == (str(profile_file), True)


def test_repeated_write_leaves_the_file_untouched(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - the stanza never changes, so a second call has nothing to write: no temp file, no replace
    profile_file = tmp_path / "config"
    write_cats_profile("accA", "default", None, profile_file, None)
    before = profile_file.stat()

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    after = profile_file.stat()
    assert ((after.st_mtime_ns, after.st_ino), os.listdir(tmp_path)) == ((before.st_mtime_ns, before.st_ino), ["config"])


def test_unrelated_sections_are_preserved(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"
    profile_file.write_text("[profile other]\nregion = eu-west-2\noutput = json\n")

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    expected = {"region": "eu-west-2", "output": "json"}
    assert _read_section(profile_file, "profile other") == expected


def test_new_config_file_has_mode_0600(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert stat.S_IMODE(profile_file.stat().st_mode) == 0o600


def test_missing_parent_directory_is_created(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "nested" / "dir" / "config"

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert profile_file.parent.is_dir()


def test_non_cats_prefix_is_rejected(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"

    # act / assert
    with pytest.raises(CatsUsageError, match="profile_name must start with 'cats-'"):
        write_cats_profile("accA", "default", "default", profile_file, None)


def test_named_role_writes_that_roles_arn(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"

    # act
    write_cats_profile("accA", "readonly", None, profile_file, None)

    # assert
    assert _read_section(profile_file, "profile cats-accA-readonly")["role_arn"] == READONLY_ROLE_ARN


def test_unknown_role_is_a_usage_error_before_any_aws_call(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - cats.yaml names only `readonly` under additional_roles
    profile_file = tmp_path / "config"

    # act
    with pytest.raises(CatsUsageError, match=r"Role 'nope' is not a name under additional_roles .* Available roles: \['default', 'readonly'\]"):
        write_cats_profile("accA", "nope", None, profile_file, None)

    # assert - refused from the config alone: no account was built (no AssumeRole) and nothing was written
    expected = (False, False)
    assert expected == (mock_get_account.called, profile_file.exists())


def test_unknown_account_is_a_config_error_before_any_aws_call(mock_get_account: MagicMock, tmp_path: Path) -> None:
    """CatsConfigError, the class get_account raises for an unknown account, so callers see one class for one condition."""
    # arrange
    profile_file = tmp_path / "config"

    # act
    with pytest.raises(CatsConfigError, match=r"Account 'nope' is not a keyname under accounts_under_test .* Available: \['accA'\]"):
        write_cats_profile("nope", "default", None, profile_file, None)

    # assert
    expected = (False, False)
    assert expected == (mock_get_account.called, profile_file.exists())


def test_non_config_file_is_refused_untouched(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - a misled caller points profile_file at something that is not an AWS config file
    profile_file = tmp_path / "notes.txt"
    original = "shopping list\n- eggs\n"
    profile_file.write_text(original)

    # act
    with pytest.raises(CatsUsageError, match=f"{profile_file} is not an AWS config file; nothing written."):
        write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert profile_file.read_text() == original


def test_ini_shaped_file_that_is_not_an_aws_config_is_refused_untouched(mock_get_account: MagicMock, tmp_path: Path) -> None:
    """A .gitconfig is INI-shaped; splicing `[profile cats-...]` into it would make `git config --list` fail with 'bad config line'."""
    # arrange
    profile_file = tmp_path / ".gitconfig"
    original = "[user]\n\tname = Foo\n\temail = foo@example.com\n"
    profile_file.write_text(original)

    # act
    with pytest.raises(CatsUsageError, match=f"{profile_file} is not an AWS config file; nothing written."):
        write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert profile_file.read_text() == original


def test_environment_credentials_name_credential_source(mock_get_account: MagicMock, mock_parent_session: MagicMock, tmp_path: Path) -> None:
    # arrange - this process holds keys from AWS_ACCESS_KEY_ID and friends
    mock_parent_session.return_value.get_credentials.return_value.method = EnvProvider.METHOD
    profile_file = tmp_path / "config"

    # act
    result = write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    expected = ("credential_source = Environment", "Environment")
    assert (result["parent"], _read_section(profile_file, "profile cats-accA-default")["credential_source"]) == expected


def test_web_identity_from_environment_writes_the_parent_stanza_first(
    mock_get_account: MagicMock, mock_parent_session: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # arrange - an EKS IRSA pod: the parent identity exists only as two environment variables
    mock_parent_session.return_value.get_credentials.return_value.method = AssumeRoleWithWebIdentityProvider.METHOD
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::222222222222:role/pod")
    monkeypatch.delenv("AWS_ROLE_SESSION_NAME", raising=False)
    profile_file = tmp_path / "config"

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    expected = (
        "[profile cats-parent-accA]\nrole_arn = arn:aws:iam::222222222222:role/pod\nweb_identity_token_file = /var/run/secrets/token\n"
        + "\n"
        + "[profile cats-accA-default]\n"
        + f"role_arn = {DEFAULT_ROLE_ARN}\nsource_profile = cats-parent-accA\nrole_session_name = cats-profile\nregion = us-east-1\n"
    )
    assert profile_file.read_text() == expected


def test_profile_name_colliding_with_the_parent_stanza_is_refused(
    mock_get_account: MagicMock, mock_parent_session: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # arrange - web-identity source, and a --profile-name equal to the parent stanza CATS must write
    mock_parent_session.return_value.get_credentials.return_value.method = AssumeRoleWithWebIdentityProvider.METHOD
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::222222222222:role/pod")
    profile_file = tmp_path / "config"

    # act / assert - refused by name, and nothing is written
    with pytest.raises(CatsUsageError, match="collides with the parent profile"):
        write_cats_profile("accA", "default", "cats-parent-accA", profile_file, None)
    assert profile_file.exists() is False


# =============================================================================
# The file is the user's: only the sections CATS owns change
# =============================================================================

CATS_SECTION = (
    f"[profile cats-accA-default]\nrole_arn = {DEFAULT_ROLE_ARN}\n"
    + "source_profile = parent\nrole_session_name = cats-profile\nregion = us-east-1\n"
)


def test_comments_and_formatting_outside_the_section_are_kept_byte_for_byte(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - a hand-maintained file: header comment, compact `key=value`, an inline comment, an `s3 =` block
    profile_file = tmp_path / "config"
    original = "# work profiles\n[profile work]\nregion=eu-west-2  ; rotated monthly\ns3 =\n  max_concurrent_requests = 20\n"
    profile_file.write_text(original)

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert profile_file.read_text() == original + "\n" + CATS_SECTION


def test_existing_section_is_replaced_in_place_and_its_neighbors_kept(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"
    profile_file.write_text(
        "[profile before]\nregion=us-west-2\n\n[profile cats-accA-default]\nrole_arn = OLD\nregion = old-1\n\n[profile after]\nregion=ap-south-1\n"
    )

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert profile_file.read_text() == "[profile before]\nregion=us-west-2\n\n" + CATS_SECTION + "\n[profile after]\nregion=ap-south-1\n"


def test_duplicate_cats_sections_collapse_to_one(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - two definitions of the same section make the file unreadable to the AWS CLI; CATS owns this one, so it repairs it
    profile_file = tmp_path / "config"
    profile_file.write_text(
        "[profile cats-accA-default]\nrole_arn = OLD1\n\n[profile other]\nregion=us-east-2\n\n[profile cats-accA-default]\nrole_arn = OLD2\n"
    )

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert profile_file.read_text() == CATS_SECTION + "\n[profile other]\nregion=us-east-2\n"


def test_sections_after_the_replaced_one_keep_their_compact_spacing(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - a file with no blank lines between sections; only the line after CATS's own section may gain one
    profile_file = tmp_path / "config"
    profile_file.write_text(
        "[profile a]\nregion=us-east-1\n[profile cats-accA-default]\nrole_arn = OLD\n[profile b]\nregion=us-east-2\n[profile c]\nregion=eu-west-1\n"
    )

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    expected = "[profile a]\nregion=us-east-1\n" + CATS_SECTION + "\n[profile b]\nregion=us-east-2\n[profile c]\nregion=eu-west-1\n"
    assert profile_file.read_text() == expected


def test_file_without_trailing_newline_gets_the_section_on_its_own_lines(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange
    profile_file = tmp_path / "config"
    profile_file.write_text("[profile other]\nregion=us-east-2")

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert profile_file.read_text() == "[profile other]\nregion=us-east-2\n\n" + CATS_SECTION


def test_existing_file_keeps_its_mode(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - a user who chose 0644 must not find their file silently 0600 after a CATS write
    profile_file = tmp_path / "config"
    profile_file.write_text("[profile other]\nregion=us-east-2\n")
    profile_file.chmod(0o644)

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert stat.S_IMODE(profile_file.stat().st_mode) == 0o644


def test_symlinked_config_file_is_written_through_the_link(mock_get_account: MagicMock, tmp_path: Path) -> None:
    # arrange - a dotfiles setup: ~/.aws/config -> ~/dotfiles/aws-config
    target = tmp_path / "dotfiles" / "aws-config"
    target.parent.mkdir()
    target.write_text("[profile other]\nregion=us-east-2\n")
    link = tmp_path / "config"
    link.symlink_to(target)

    # act
    result = write_cats_profile("accA", "default", None, link, None)

    # assert - the link survives, the target holds the section, and the caller is told the real path
    expected = (True, "[profile other]\nregion=us-east-2\n\n" + CATS_SECTION, str(target.resolve()))
    assert (link.is_symlink(), target.read_text(), result["profile_file"]) == expected


def test_tilde_in_profile_file_is_expanded(mock_get_account: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange - a literal "~" directory in the cwd was the previous outcome
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    # act
    result = write_cats_profile("accA", "default", None, Path("~/.aws/config"), None)

    # assert
    expected = (str((tmp_path / ".aws" / "config").resolve()), True, False)
    assert (result["profile_file"], (tmp_path / ".aws" / "config").is_file(), (tmp_path / "~").exists()) == expected


def test_default_profile_file_is_the_home_config_file(mock_get_account: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.setenv("HOME", str(tmp_path))

    # act
    result = write_cats_profile("accA", "default", None, None, None)

    # assert
    assert result["profile_file"] == str((tmp_path / ".aws" / "config").resolve())


def test_write_profile_result_builds_a_pydantic_type_adapter() -> None:
    """WriteProfileResult is a typing_extensions.TypedDict, so pydantic (via FastMCP) can build its schema on Python < 3.12.

    typing.TypedDict raises PydanticUserError here on 3.11; this guards the cats_write_profile MCP tool's return annotation.
    """
    # arrange
    expected_fields = {"profile_name", "profile_file", "role_arn", "parent"}

    # act
    schema = TypeAdapter(WriteProfileResult).json_schema()

    # assert
    assert set(schema["required"]) == expected_fields


# A profile name is written into a `[profile ...]` header of a file whose `credential_process`
# lines the AWS CLI and SDKs execute. The MCP tool takes the name from an agent, so a name that
# could close the header and open a section of its own is refused before anything is written.
INJECTED_PROFILE_NAME = "cats-x]\n[default]\ncredential_process = /tmp/evil\n[profile cats-y"


@pytest.mark.parametrize(
    "profile_name",
    [
        INJECTED_PROFILE_NAME,
        "cats-x]",
        "cats-x\n[default]",
        "cats-x\r[default]",
        "cats-x y",
        "cats-",
        "cats-x=1",
    ],
)
def test_profile_name_that_cannot_be_a_header_is_rejected_before_writing(mock_get_account: MagicMock, tmp_path: Path, profile_name: str) -> None:
    # arrange
    profile_file = tmp_path / "config"
    profile_file.write_text("[default]\nregion = us-east-1\n")

    # act
    with pytest.raises(CatsUsageError, match=r"profile_name .* may contain only"):
        write_cats_profile("accA", "default", profile_name, profile_file, None)

    # assert - the file is exactly what it was
    assert profile_file.read_text() == "[default]\nregion = us-east-1\n"


@pytest.mark.parametrize("profile_name", ["cats-accA-default", "cats-my.team@corp", "cats-A_b-9"])
def test_profile_name_with_profile_characters_is_accepted(mock_get_account: MagicMock, tmp_path: Path, profile_name: str) -> None:
    # arrange
    profile_file = tmp_path / "config"

    # act
    write_cats_profile("accA", "default", profile_name, profile_file, None)

    # assert
    assert _read_section(profile_file, f"profile {profile_name}")["role_arn"] == DEFAULT_ROLE_ARN


@pytest.mark.parametrize("role", ["__class__", "_role_arn_for", "readonly\n[default]", "read only", "", "9lives"])
def test_role_that_is_not_a_keyname_is_rejected_before_lookup(mock_get_account: MagicMock, tmp_path: Path, role: str) -> None:
    # arrange
    profile_file = tmp_path / "config"

    # act
    with pytest.raises(CatsUsageError, match="is not a role name"):
        write_cats_profile("accA", role, None, profile_file, None)

    # assert - the role handler was never consulted and nothing was written
    mock_get_account.return_value.role._role_arn_for.assert_not_called()
    assert profile_file.exists() is False


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("profile cats-x]\n[default]", {"role_arn": "arn"}),
        ("profile cats-x", {"role_arn": "arn\ncredential_process = /tmp/evil"}),
        ("profile cats-x", {"role_arn": "arn\r\n[default]"}),
        ("profile cats-x", {"bad key": "arn"}),
        ("profile cats-x", {"[default]": "arn"}),
    ],
)
def test_splice_section_refuses_anything_that_escapes_its_line(name: str, values: dict[str, str]) -> None:
    """The last check before the user's file is written, independent of what the callers validated."""
    # act / assert
    with pytest.raises(ValueError, match="cannot"):
        _splice_section("[default]\nregion = us-east-1\n", name, values)


def test_missing_parent_directory_is_created_private(mock_get_account: MagicMock, tmp_path: Path) -> None:
    """A new ~/.aws is 0700, as the AWS CLI creates it: the directory holds credentials files."""
    # arrange
    profile_file = tmp_path / "home" / ".aws" / "config"

    # act
    write_cats_profile("accA", "default", None, profile_file, None)

    # assert
    assert stat.S_IMODE(profile_file.parent.stat().st_mode) == 0o700


def test_tilde_in_config_path_is_expanded_for_the_account_too(mock_get_account: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The membership check and get_account must see the same file: `--config '~/x.yaml'` arrives unexpanded from a quoting shell."""
    # arrange
    monkeypatch.setenv("HOME", str(tmp_path))
    profile_file = tmp_path / "config"

    # act
    write_cats_profile("accA", "default", None, profile_file, "~/controls/cats.yaml")

    # assert
    mock_get_account.assert_called_once_with(tmp_path / "controls" / "cats.yaml", "accA")


def test_role_defined_only_in_global_account_settings_passes_the_config_check(
    mock_parent_session: MagicMock, mocker: MockerFixture, tmp_path: Path
) -> None:
    """Against a real cats.yaml: a role declared under global_account_settings is a role of every account, as SessionHandler sees it."""
    # arrange - only get_account is mocked; Settings reads the file
    cats_yaml = tmp_path / "cats.yaml"
    write_config(cats_yaml, make_config(account_name="accA", additional_roles={"readonly": {"managed_policies": ["ReadOnlyAccess"]}}))
    mock_get_account = mocker.patch("cats.utils.aws_profile.get_account")
    mock_get_account.return_value.aws_region = "us-east-1"
    mock_get_account.return_value.role = MagicMock(spec=["default", "readonly", "_role_arn_for"])
    mock_get_account.return_value.role._role_arn_for.side_effect = _role_arn_for
    profile_file = tmp_path / "config"

    # act
    write_cats_profile("accA", "readonly", None, profile_file, cats_yaml)

    # assert
    assert _read_section(profile_file, "profile cats-accA-readonly")["role_arn"] == READONLY_ROLE_ARN
