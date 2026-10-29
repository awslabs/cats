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

"""Under pytest-xdist, workers ship their evidence entries to the controller at session
end via workeroutput, and the controller merges them before the writers run. This is the
only path report.json takes on a parallel run, so it gets its own end-to-end test. The
controller also hands each worker the run's RoleSessionName via workerinput, the only path
by which a worker's AssumeRole calls come to carry the run id.
"""

import hashlib
import json
import xml.etree.ElementTree as ET
from typing import Any

import pytest
import yaml

from cats.version import __version__
from tests.utils import make_config


def test_xdist_run_records_every_outcome(pytester: pytest.Pytester) -> None:
    """A parallel run must produce the same evidence a serial run would."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(make_config(accounts={"testaccount": "234567890123"})))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile(
        test_alpha="""
import pytest

def test_passes():
    assert True

def test_fails():
    assert False
""",
        test_beta="""
import pytest

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown exploded")

def test_teardown_errors(broken_teardown):
    assert True

@pytest.fixture
def broken_fixture():
    raise RuntimeError("fixture exploded")

def test_setup_errors(broken_fixture):
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:randomly")

    # assert - pytest counts a teardown error as both a pass (the call) and an error
    result.assert_outcomes(passed=2, failed=1, errors=2)
    entries: list[dict[str, Any]] = json.loads((pytester.path / "report.json").read_text())["tests"]
    by_name = {entry["test_name"]: (entry["result"], entry.get("error_message")) for entry in entries}
    expected = {
        "test_passes": ("passed", None),
        "test_fails": ("failed", "assert False"),
        "test_teardown_errors": ("error", "RuntimeError: teardown exploded"),
        "test_setup_errors": ("error", "RuntimeError: fixture exploded"),
    }
    assert expected == by_name


# moto is started in the conftest, so it is active in the controller (GetCallerIdentity at session end) and in
# every worker (the AssumeRole each test makes). Each worker creates the role in its own moto state.
ASSUMING_TEST = """
import boto3
import json
from types import SimpleNamespace

from cats.resources.session_interact import get_test_session

ROLE_ARN = "arn:aws:iam::123456789012:role/CATS-test-infra-testaccount-default-test-role"
TRUST_ANYONE = json.dumps({{"Version": "2012-10-17", "Statement": [{{"Effect": "Allow", "Principal": {{"AWS": "*"}}, "Action": "sts:AssumeRole"}}]}})


def test_{name}(cats_report):
    boto3.client("iam", region_name="us-east-1").create_role(
        RoleName="CATS-test-infra-testaccount-default-test-role", AssumeRolePolicyDocument=TRUST_ANYONE
    )
    session = get_test_session(ROLE_ARN, "testaccount", "us-east-1")
    cats_report.add_report_metadata({{"assumed_as": session.client("sts").get_caller_identity()["Arn"]}})
    cats_report.add_account(SimpleNamespace(name="testaccount", account_id="234567890123", tags={{}}))
"""


def test_xdist_workers_assume_roles_under_the_run_session_name(pytester: pytest.Pytester) -> None:
    """The run id exists only on the controller; every worker's AssumeRole must still carry it, and the
    controller must name the caller in report.json, junit.xml and the OSCAL document."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"report_formats": ["cats_json", "junit", "oscal_ar"]}
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
from moto import mock_aws

os.environ["CATS_CONFIG"] = "{config_path}"
os.environ["GIT_CEILING_DIRECTORIES"] = "{pytester.path.parent}"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
mock_aws().start()
''')
    pytester.makepyfile(test_alpha=ASSUMING_TEST.format(name="alpha"), test_beta=ASSUMING_TEST.format(name="beta"))
    config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()

    # act
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:randomly")
    result.assert_outcomes(passed=2)
    report = json.loads((pytester.path / "report.json").read_text())
    run_id = report["run"]["run_id"]
    testsuite = ET.parse(pytester.path / "junit.xml").getroot().find("./testsuite")  # noqa: S314 - XML written by this test's own pytest run
    assert testsuite is not None
    junit_properties = {prop.get("name"): prop.get("value") for prop in testsuite.findall("./properties/property")}
    oscal_props = json.loads((pytester.path / "assessment-results.json").read_text())["assessment-results"]["metadata"]["props"]

    # assert
    principal = f"arn:aws:sts::123456789012:assumed-role/CATS-test-infra-testaccount-default-test-role/cats-{run_id}"
    expected = {
        "run": {
            "role_session_name": f"cats-{run_id}",
            "caller_identity": {"account_id": "123456789012", "arn": "arn:aws:sts::123456789012:user/moto"},
        },
        "assumed_as": {"test_alpha": principal, "test_beta": principal},
        "junit": {
            "cats:run_id": run_id,
            "cats:role_session_name": f"cats-{run_id}",
            "cats:caller_identity_arn": "arn:aws:sts::123456789012:user/moto",
            "cats:config_sha256": config_sha256,
        },
        "oscal": [
            {"name": "tool-version", "value": __version__},
            {"name": "cats-run-id", "value": run_id},
            {"name": "cats-role-session-name", "value": f"cats-{run_id}"},
            {"name": "cats-caller-identity-arn", "value": "arn:aws:sts::123456789012:user/moto"},
            {"name": "cats-config-sha256", "value": config_sha256},
        ],
    }
    assert expected == {
        "run": {key: report["run"][key] for key in ("role_session_name", "caller_identity")},
        "assumed_as": {entry["test_name"]: entry["additional_metadata"]["assumed_as"] for entry in report["tests"]},
        "junit": junit_properties,
        "oscal": oscal_props,
    }
