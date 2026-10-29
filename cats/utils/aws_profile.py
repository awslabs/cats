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

"""Write an AWS profile that lets the user's own AWS tools act as a CATS test role.

Shared by `cats run write-profile` (CLI) and `cats_write_profile` (MCP). The profile names the
role and the identity to assume it from (`role_arn` plus `source_profile` or
`credential_source`); it never contains a credential. The AWS CLI and every SDK assume the
role themselves and refresh it, so the profile is written once and never expires.
"""

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from boto3.session import Session
from botocore.credentials import (
    AssumeRoleProvider,
    AssumeRoleWithWebIdentityProvider,
    ConfigProvider,
    ContainerProvider,
    EnvProvider,
    InstanceMetadataProvider,
    ProcessProvider,
    SharedCredentialProvider,
    SSOProvider,
)
from typing_extensions import TypedDict  # TypedDict must come from typing_extensions: pydantic rejects typing.TypedDict on Python < 3.12

from cats.config import Settings, override_config_path
from cats.exceptions import CatsConfigError, CatsUsageError
from cats.resources import get_account
from cats.resources.session_interact import PROFILE_ROLE_SESSION_NAME
from cats.user_agent import attributed_session


class WriteProfileResult(TypedDict):
    """The record `write_cats_profile` returns and both `cats run write-profile` and `cats_write_profile` emit."""

    profile_name: str
    profile_file: str
    role_arn: str
    parent: str  # the `source_profile = ...` or `credential_source = ...` line that names the parent identity


_SECTION_HEADER = re.compile(r"^\s*\[([^\]]*)\]\s*$")
# The section names an AWS config file may hold (the AWS CLI's `config` file format). Every real
# config passes; a .gitconfig (`[user]`), .pypirc (`[pypi]`), tox.ini (`[tox]`) or pip.conf
# (`[global]`) is INI-shaped too but names none of these, and is refused untouched.
_AWS_CONFIG_SECTION = re.compile(r"^(default|profile .+|sso-session .+|services .+|plugins|preview)$")
# `key = value`, `key=value`, `key =` (an empty value), and the indented sub-key form the AWS
# CLI uses under `s3 =`. A key has no whitespace and may not open a section, a comment or an
# assignment, so `export X=y` is not one.
_KEY_VALUE = re.compile(r"^\s*[^\s\[#;=][^\s=]*\s*=")

# The name becomes a `[profile ...]` header in a file whose `credential_process` lines the AWS CLI
# and SDKs execute, so anything that could close the header or start a new line is refused.
_PROFILE_NAME = re.compile(r"cats-[A-Za-z0-9_.@-]+")

# The keyname pattern from cats.config; it also keeps `_`-prefixed handler attributes out of reach.
_ROLE_ATTRIBUTE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

_LINE_BREAK = re.compile(r"[\r\n]")
_INI_KEY = re.compile(r"[A-Za-z0-9_]+")

# Ambient credential methods an AWS profile names with `credential_source`.
_CREDENTIAL_SOURCE_BY_METHOD: dict[str, str] = {
    EnvProvider.METHOD: "Environment",
    InstanceMetadataProvider.METHOD: "Ec2InstanceMetadata",
    ContainerProvider.METHOD: "EcsContainer",
}
# Ambient credential methods that come from a named profile, which `source_profile` names.
# An SSO profile and a chained assume-role profile are both accepted as a source profile.
_PROFILE_METHODS = frozenset(
    {SharedCredentialProvider.METHOD, ConfigProvider.METHOD, SSOProvider.METHOD, AssumeRoleProvider.METHOD, ProcessProvider.METHOD}
)


@dataclass(frozen=True)
class _ParentSource:
    """How a written profile reaches the identity this process runs as."""

    key: str
    """`source_profile` or `credential_source`."""
    value: str
    parent_profile: tuple[str, dict[str, str]] | None = None
    """A profile to write before the role's, when the parent exists only in this process's environment."""

    @property
    def line(self) -> str:
        return f"{self.key} = {self.value}"


def _parent_source(session: Session, account: str) -> _ParentSource:
    """Name, in profile terms, the identity ``session`` resolved from this process's environment.

    The mapping follows botocore's own provider classes, so every way CATS can hold credentials
    is either expressible or refused by name. Web identity configured by environment (EKS IRSA,
    OIDC CI) has no `credential_source` value, so the parent is materialized as a profile of its
    own, `cats-parent-<account>`, carrying the role and token file the environment names.
    """
    credentials = session.get_credentials()
    if credentials is None:
        raise CatsUsageError("No AWS credentials resolved in this environment, so there is no identity for the profile to assume the role from.")
    method = credentials.method
    if method in _PROFILE_METHODS:
        return _ParentSource("source_profile", session.profile_name)
    if method in _CREDENTIAL_SOURCE_BY_METHOD:
        return _ParentSource("credential_source", _CREDENTIAL_SOURCE_BY_METHOD[method])
    if method == AssumeRoleWithWebIdentityProvider.METHOD:
        token_file = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        role_arn = os.environ.get("AWS_ROLE_ARN")
        if token_file is None or role_arn is None:
            return _ParentSource("source_profile", session.profile_name)
        parent_name = f"cats-parent-{account}"
        values = {"role_arn": role_arn, "web_identity_token_file": token_file}
        session_name = os.environ.get("AWS_ROLE_SESSION_NAME")
        if session_name is not None:
            values["role_session_name"] = session_name
        return _ParentSource("source_profile", parent_name, (parent_name, values))
    raise CatsUsageError(
        f"This process holds AWS credentials from '{method}', which an AWS profile cannot name as its source. "
        + "Run with a named profile (AWS_PROFILE), environment variables, an instance role or a container role."
    )


def _is_aws_config_text(text: str) -> bool:
    """True when the text is an AWS config file: only AWS section headers, each followed by `key = value` lines, plus blanks and comments.

    An empty text passes (the file does not exist yet). A key before any header fails: that is
    the shape of a `.env` file, and the AWS CLI rejects it too. A header that is not one of
    the AWS config section names fails, however well-formed the rest is.
    """
    in_section = False
    for line in text.splitlines():
        if line.strip() == "" or line.lstrip().startswith(("#", ";")):
            continue
        header = _SECTION_HEADER.match(line)
        if header is not None:
            if _AWS_CONFIG_SECTION.match(header.group(1).strip()) is None:
                return False
            in_section = True
            continue
        if in_section is False or _KEY_VALUE.match(line) is None:
            return False
    return True


def _splice_section(existing: str, name: str, values: dict[str, str]) -> str:
    """Return `existing` with the `[name]` section replaced by `values`, every other byte untouched.

    The file is the user's: comments, spacing and the other sections are copied through as
    they are. Every occurrence of `[name]` is removed (a duplicate makes the whole file
    unreadable to the AWS CLI) and the new section is written where the first one was,
    followed by a blank line, or appended if there was none.

    Raises ValueError if `name`, a key or a value could escape its own line and so write a header
    or key the caller did not ask for. Callers validate first; this is the last check before the write.
    """
    if _LINE_BREAK.search(name) is not None or "]" in name or "[" in name:
        raise ValueError(f"section name {name!r} cannot be written as an INI header")
    for key, value in values.items():
        if _INI_KEY.fullmatch(key) is None:
            raise ValueError(f"{key!r} cannot be written as an INI key")
        if _LINE_BREAK.search(value) is not None:
            raise ValueError(f"the value of {key!r} cannot span lines")
    section = [f"[{name}]\n"] + [f"{key} = {value}\n" for key, value in values.items()]
    kept: list[str] = []
    inserted = False
    skipping = False
    for line in existing.splitlines(keepends=True):
        header = _SECTION_HEADER.match(line)
        if header is not None:
            leaving_own_section = skipping
            skipping = header.group(1).strip() == name
            if skipping and inserted is False:
                kept.extend(section)
                inserted = True
            if leaving_own_section and skipping is False and kept[-1].strip() != "":
                kept.append("\n")
        if skipping is False:
            kept.append(line)
    if skipping is True:
        # The file ended inside a removed duplicate; the blank line that separated it is now trailing.
        while len(kept) > 0 and kept[-1].strip() == "":
            kept.pop()
    if inserted is False:
        if len(kept) > 0 and kept[-1].endswith("\n") is False:
            kept[-1] += "\n"
        if len(kept) > 0 and kept[-1].strip() != "":
            kept.append("\n")
        kept.extend(section)
    return "".join(kept)


def _replace_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically. A new file gets mode 0600 and a new parent directory 0700; existing ones keep their modes."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".cats-profile-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def write_cats_profile(
    account: str,
    role: str,
    profile_name: str | None,
    profile_file: Path | None,
    config_path: str | Path | None,
) -> WriteProfileResult:
    """Write a profile that assumes ACCOUNT's ROLE from this process's identity; return what was written.

    - profile_name defaults to `cats-<account>-<role>`. Must start with `cats-` — prevents
      clobbering user-maintained profiles.
    - profile_file defaults to `~/.aws/config`; `~` is expanded and a symlink is followed, so the
      file the AWS CLI reads is the one written and the returned `profile_file` is that real path.
      The file must be an AWS config file (or not exist yet): anything else is refused untouched.
      A new file gets mode 0600; an existing file keeps its mode, its comments and every section
      other than the ones CATS owns. A file that already holds the stanza is not rewritten.
    - role='default' is the CATS-managed test role; any other value is a name under
      `additional_roles` (matching `acc.role.<role>`).
    - The account and role names are checked against cats.yaml before the account is built, so an
      unknown one is refused before any AWS call. The account is then built, which assumes the role:
      a stack that is not deployed, another release's stack, or a role this identity cannot assume
      all fail here, before any write.

    Returns `profile_name`, the real `profile_file`, the `role_arn` written and the `parent` line
    (`source_profile = ...` or `credential_source = ...`) that names the identity it assumes from.
    """
    if _ROLE_ATTRIBUTE.fullmatch(role) is None:
        raise CatsUsageError(f"role {role!r} is not a role name: use 'default' or a keyname under additional_roles in cats.yaml.")
    resolved_name = profile_name if profile_name is not None else f"cats-{account}-{role}"
    if not resolved_name.startswith("cats-"):
        raise CatsUsageError(f"profile_name must start with 'cats-' (got '{resolved_name}') to prevent clobbering user profiles.")
    if _PROFILE_NAME.fullmatch(resolved_name) is None:
        raise CatsUsageError(
            f"profile_name {resolved_name!r} may contain only letters, digits and '_', '.', '@', '-' after the 'cats-' prefix: "
            + "it is written as a section header of the AWS config file."
        )

    requested_file = profile_file if profile_file is not None else Path("~") / ".aws" / "config"
    resolved_file = requested_file.expanduser().resolve()

    # Config only, before any AWS call. CatsConfigError for the account, as get_account raises; CatsUsageError for --role.
    config_file = Path(config_path).expanduser() if config_path is not None else None
    with override_config_path(config_file):
        accounts = Settings().accounts_under_test
        if account not in accounts:
            raise CatsConfigError(f"Account '{account}' is not a keyname under accounts_under_test in cats.yaml. Available: {sorted(accounts)}")
        role_names = sorted(accounts[account].account_settings.additional_roles)
    if role != "default" and role not in role_names:
        raise CatsUsageError(
            f"Role '{role}' is not a name under additional_roles in cats.yaml for account '{account}'. Available roles: {['default', *role_names]}"
        )

    acc = get_account(config_file, account)
    if role != "default":
        getattr(acc.role, role)  # the role is known to exist; this assumes it, so one this identity cannot assume fails here
    role_arn = acc.role._role_arn_for(role)
    parent = _parent_source(attributed_session(), account)

    stanzas: dict[str, dict[str, str]] = {}
    if parent.parent_profile is not None:
        parent_name, parent_values = parent.parent_profile
        if parent_name == resolved_name:
            # The web-identity path writes two stanzas; a profile_name equal to the parent's would make
            # `_splice_section` strip the just-written parent and leave the role stanza sourcing a profile
            # that no longer exists.
            raise CatsUsageError(
                f"profile_name '{resolved_name}' collides with the parent profile CATS must write for this "
                + f"web-identity source ('{parent_name}'); choose a different --profile-name."
            )
        stanzas[f"profile {parent_name}"] = parent_values
    stanzas[f"profile {resolved_name}"] = {
        "role_arn": role_arn,
        parent.key: parent.value,
        "role_session_name": PROFILE_ROLE_SESSION_NAME,
        "region": acc.aws_region,
    }

    existing_text = resolved_file.read_text(encoding="utf-8") if resolved_file.exists() else ""
    if _is_aws_config_text(existing_text) is False:
        raise CatsUsageError(f"{resolved_file} is not an AWS config file; nothing written.")
    new_text = existing_text
    for name, values in stanzas.items():
        new_text = _splice_section(new_text, name, values)
    if new_text != existing_text:
        _replace_file(resolved_file, new_text)

    return WriteProfileResult(profile_name=resolved_name, profile_file=str(resolved_file), role_arn=role_arn, parent=parent.line)
