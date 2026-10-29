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

"""How a run identifies itself in AWS: the caller CATS ran as, and the RoleSessionName its test roles are assumed under.

The parallel run's path (workerinput, every worker's AssumeRole, the three report formats) is
in test_pytest_plugin_xdist.py; this file covers the pieces and the single-process cases.
"""

import json
from unittest.mock import MagicMock

import pytest
import yaml
from botocore.exceptions import NoCredentialsError
from moto import mock_aws
from pytest_mock import MockerFixture

from cats.pytest_plugin import _assumed_a_role, _caller_identity
from cats.reporting import AccountReportDetail, ReportEntry
from tests.utils import make_config


def make_entry(accounts: list[AccountReportDetail]) -> ReportEntry:
    return {
        "result": "passed",
        "accounts_in_test": accounts,
        "test_name": "test_ok",
        "nodeid": "tests/test_a.py::test_ok",
        "test_docstring": None,
        "standards": [],
        "standard_metadata": {},
        "custom_markers": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": None,
        "duration_s": 0.1,
        "recorded_at": "2026-09-24T10:53:17.000000Z",
    }


# =============================================================================
# _caller_identity
# =============================================================================


def test_caller_identity_names_the_principal_cats_runs_as() -> None:
    # act
    with mock_aws():
        identity = _caller_identity()

    # assert
    expected = {"account_id": "123456789012", "arn": "arn:aws:sts::123456789012:user/moto"}
    assert expected == identity


def test_caller_identity_is_null_with_a_warning_when_sts_cannot_answer(mocker: MockerFixture) -> None:
    """The report is still written: not knowing who ran CATS must never cost the evidence of what ran."""
    # arrange
    environment_session = MagicMock()
    environment_session.client.return_value.get_caller_identity.side_effect = NoCredentialsError()
    mocker.patch("cats.pytest_plugin.attributed_session", return_value=environment_session)

    # act
    with pytest.warns(UserWarning, match=r"^GetCallerIdentity failed, so run\.caller_identity is null: Unable to locate credentials$"):
        identity = _caller_identity()

    # assert
    assert identity is None


# =============================================================================
# _assumed_a_role
# =============================================================================


def test_a_test_that_used_an_account_means_a_role_was_assumed() -> None:
    # arrange
    entries = [make_entry([]), make_entry([AccountReportDetail(account_name="prod", account_id="111111111111", tags={})])]

    # act
    assumed = _assumed_a_role(entries)

    # assert
    assert assumed is True


def test_no_test_used_an_account_means_no_role_was_assumed() -> None:
    """The run made no AWS API call, and recording who ran it must not add one."""
    # act
    assumed = _assumed_a_role([make_entry([]), make_entry([])])

    # assert
    assert assumed is False


# =============================================================================
# The session name outside a reported run
# =============================================================================


def test_reporting_disabled_leaves_the_default_session_name(pytester: pytest.Pytester) -> None:
    """With reporting.enabled: false there is no run id to be found by, so the process keeps the cats-cli default."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"enabled": False}
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile(f'''
from pathlib import Path
from cats.resources.session_interact import current_role_session_name

def test_records_the_session_name():
    Path("{pytester.path / "session_name.txt"}").write_text(current_role_session_name())
''')

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=1)

    # assert
    assert (pytester.path / "session_name.txt").read_text() == "cats-cli"


# =============================================================================
# GetCallerIdentity failing inside a session
# =============================================================================


def test_run_whose_caller_cannot_be_identified_still_exits_clean(pytester: pytest.Pytester) -> None:
    """A test used an account, so the controller asks STS who ran CATS; STS cannot answer. The evidence is written,
    caller_identity is null, the run warns once, and the exit status is the tests' own."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"report_formats": ["cats_json"]}
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
from botocore.exceptions import NoCredentialsError

import cats.pytest_plugin

os.environ["CATS_CONFIG"] = "{config_path}"


def no_identity():
    raise NoCredentialsError()


cats.pytest_plugin.attributed_session = no_identity
''')
    pytester.makepyfile("""
from types import SimpleNamespace


def test_used_an_account(cats_report):
    cats_report.add_account(SimpleNamespace(name="testaccount", account_id="234567890123", tags={}))
""")

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    run = json.loads((pytester.path / "report.json").read_text())["run"]

    # assert
    expected = (0, None, [{"account_name": "testaccount", "account_id": "234567890123"}], 1)
    warning_lines = [line for line in result.outlines if "GetCallerIdentity failed, so run.caller_identity is null" in line]
    assert expected == (result.ret, run["caller_identity"], run["accounts"], len(warning_lines))
