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

"""Integration tests: minimal config (no network targets)."""

from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from cats.cfn.deploy import DeployResult, get_stack_name, get_stack_status
from cats.cli import cats_cli
from cats.cli.infra import _start_deploy_account
from cats.exceptions import CatsVersionMismatchError
from cats.report_run import ResultCounts
from cats.resources.account_under_test import AccountUnderTest
from cats.version import __version__
from tests.integration.conftest import INTEGRATION_TEST_PACK
from tests.integration.constants import ACCOUNT_ID, REGION, skip_wrong_account, stack_prefix
from tests.integration.reporting_helpers import junit_properties, run_nested_cats_session
from tests.utils import deploy_and_destroy

if TYPE_CHECKING:
    from botocore.awsrequest import AWSPreparedRequest

STACK_PREFIX = stack_prefix("min")
ACCOUNT_KEYNAME = "minimal"


@pytest.fixture(scope="module")
def deployed_minimal_stack(
    tmp_path_factory: pytest.TempPathFactory,
    minimal_config: dict[str, Any],
) -> Generator[str | list[str], None, None]:
    """Deploy minimal stack once for all tests in this module."""
    yield from deploy_and_destroy(tmp_path_factory, minimal_config, ACCOUNT_KEYNAME, REGION, "minimal")


@skip_wrong_account
def test_minimal_stack_creates_successfully(deployed_minimal_stack: str) -> None:
    """Minimal stack deploys with CREATE_COMPLETE status."""
    # arrange
    session = boto3.Session(region_name=REGION)
    stack_name = get_stack_name(STACK_PREFIX, deployed_minimal_stack)
    expected_status = "CREATE_COMPLETE"

    # act
    status = get_stack_status(session, stack_name, REGION)

    # assert
    assert status == expected_status


@skip_wrong_account
def test_minimal_account_session_works(deployed_minimal_stack: str) -> None:
    """AccountUnderTest session can call STS GetCallerIdentity."""
    # arrange
    account = AccountUnderTest(ACCOUNT_KEYNAME)

    # act
    identity = account.session.client("sts").get_caller_identity()

    # assert
    assert identity["Account"] == ACCOUNT_ID


@skip_wrong_account
def test_real_call_through_the_test_role_carries_both_user_agent_tokens(deployed_minimal_stack: str) -> None:
    """A real request from the deployed test role identifies CATS and this suite's test pack.

    The header is read at send time and the request is allowed through, so the assertion is on
    what AWS actually received, not on session configuration. The pack token comes from
    ``pytest_cats_test_pack`` in this suite's conftest.
    """
    # arrange
    account = AccountUnderTest(ACCOUNT_KEYNAME)
    client = account.session.client("sts")
    sent_user_agents: list[str] = []

    def record(request: "AWSPreparedRequest", **kwargs: object) -> None:
        header = request.headers["User-Agent"]
        sent_user_agents.append(header.decode("utf-8") if isinstance(header, bytes) else header)

    client.meta.events.register_first("before-send.sts.GetCallerIdentity", record)
    expected_tokens = [f"AwsCats/{__version__}", INTEGRATION_TEST_PACK.user_agent_token]

    # act
    identity = client.get_caller_identity()

    # assert
    assert identity["Account"] == ACCOUNT_ID
    assert expected_tokens == [token for token in sent_user_agents[0].split(" ") if token.startswith(("AwsCats/", "AwsCatsPack/"))]


@skip_wrong_account
def test_minimal_account_has_no_network_targets(deployed_minimal_stack: str) -> None:
    """AccountUnderTest with no network config has no network targets."""
    # arrange
    account = AccountUnderTest(ACCOUNT_KEYNAME)

    # assert
    assert account.available_network_targets == []


@skip_wrong_account
def test_stack_has_cats_tags(deployed_minimal_stack: str) -> None:
    """Deployed stack has cats:managed, cats:account-keyname, cats:version tags."""
    # arrange
    session = boto3.Session(region_name=REGION)
    cfn = session.client("cloudformation", region_name=REGION)
    stack_name = get_stack_name(STACK_PREFIX, deployed_minimal_stack)
    expected_tags = {
        "cats:managed": "true",
        "cats:account-keyname": ACCOUNT_KEYNAME,
        "cats:version": __version__,
    }

    # act
    response = cfn.describe_stacks(StackName=stack_name)
    tags = {t["Key"]: t["Value"] for t in response["Stacks"][0].get("Tags", [])}

    # assert
    assert tags == expected_tags


@skip_wrong_account
def test_repeat_deploy_with_unchanged_config_is_noop(deployed_minimal_stack: str) -> None:
    """A second deploy of an unchanged config performs no update: status stays CREATE_COMPLETE."""
    # arrange
    runner = CliRunner(env={"COLUMNS": "200"})
    session = boto3.Session(region_name=REGION)
    stack_name = get_stack_name(STACK_PREFIX, deployed_minimal_stack)

    # act
    result = runner.invoke(cats_cli, ["infra", "deploy", ACCOUNT_KEYNAME])
    status = get_stack_status(session, stack_name, REGION)

    # assert
    assert result.exit_code == 0
    assert status == "CREATE_COMPLETE"


@skip_wrong_account
def test_account_deployed_version_matches_installed(deployed_minimal_stack: str) -> None:
    """AccountUnderTest reads the deploying release back from the stack tag."""
    # arrange
    account = AccountUnderTest(ACCOUNT_KEYNAME)

    # assert
    assert account.deployed_version == __version__


@skip_wrong_account
def test_account_from_other_release_raises_version_mismatch(deployed_minimal_stack: str, mocker: MockerFixture) -> None:
    """A library upgrade after deploy is refused at construction, before any resource is touched."""
    # arrange
    mocker.patch("cats.resources.account_under_test.__version__", "0.0.0-other")
    stack_name = get_stack_name(STACK_PREFIX, deployed_minimal_stack)
    expected_error = (
        f"CATS stack '{stack_name}' in target account '{ACCOUNT_KEYNAME}' was deployed by CATS {__version__} "
        + "but CATS 0.0.0-other is installed. Deployed listeners and handlers may not match this library. "
        + f"Recreate it: cats infra destroy {ACCOUNT_KEYNAME} && cats infra deploy {ACCOUNT_KEYNAME} "
        + "(set CATS_SKIP_VERSION_CHECK=1 to bypass this check)"
    )

    # act
    with pytest.raises(CatsVersionMismatchError) as exc_info:
        AccountUnderTest(ACCOUNT_KEYNAME)

    # assert
    assert str(exc_info.value) == expected_error


@skip_wrong_account
def test_deploy_from_other_release_is_refused_and_stack_untouched(deployed_minimal_stack: str, mocker: MockerFixture) -> None:
    """Deploy over a stack from another release fails without updating it: the version tag is unchanged."""
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "0.0.0-other")
    session = boto3.Session(region_name=REGION)
    cfn = session.client("cloudformation", region_name=REGION)
    stack_name = get_stack_name(STACK_PREFIX, deployed_minimal_stack)
    expected = DeployResult(
        account_name=ACCOUNT_KEYNAME,
        success=False,
        error=(
            f"Stack {stack_name} was deployed by CATS {__version__} but CATS 0.0.0-other is installed. "
            + "Updating in place would not replace the deployed EC2 listeners or SSM documents. "
            + f"Recreate it: cats infra destroy {ACCOUNT_KEYNAME} && cats infra deploy {ACCOUNT_KEYNAME}"
        ),
    )

    # act
    result = _start_deploy_account(ACCOUNT_KEYNAME)
    tags = {t["Key"]: t["Value"] for t in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Tags", [])}

    # assert
    assert result == expected
    assert tags["cats:version"] == __version__


@skip_wrong_account
def test_reporting_cats_json_and_junit_from_a_real_session(deployed_minimal_stack: str, minimal_config: dict[str, Any], tmp_path: Path) -> None:
    """A nested session over this stack: report.json and junit.xml agree on the run, the account and every writer's outcome."""
    # arrange
    test_source = '''
from cats import AccountUnderTest, cats


@cats(standard_name="SOC2-CC6.6", control_ids=["CC6.6"])
def test_sts_through_the_test_role(minimal_account: AccountUnderTest) -> None:
    """The deployed test role answers GetCallerIdentity."""
    assert minimal_account.session.client("sts").get_caller_identity()["Account"] == minimal_account.account_id
'''

    # act
    run = run_nested_cats_session(minimal_config, ["cats_json", "junit"], test_source, tmp_path)
    assert run.returncode == 0, run.summary()
    report = run.report()
    run_id = report["run"]["run_id"]
    testsuite = run.junit_testsuite()
    testcase = testsuite.find("./testcase")
    assert testcase is not None

    # assert
    expected_counts: ResultCounts = {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0}
    expected = {
        "accounts": [{"account_name": ACCOUNT_KEYNAME, "account_id": ACCOUNT_ID}],
        "counts": expected_counts,
        "deployment": {"aws_region": REGION, "partition": "aws", "cfn_stack_prefix": STACK_PREFIX},
        "outputs": [f"report-{run_id}.json", "report.json", f"junit-{run_id}.xml", "junit.xml"],
        "writers": [
            {"format": "cats_json", "file": f"report-{run_id}.json", "status": "ok", "detail": ""},
            {"format": "junit", "file": f"junit-{run_id}.xml", "status": "ok", "detail": ""},
        ],
        "files": sorted(["report.json", f"report-{run_id}.json", "junit.xml", f"junit-{run_id}.xml"]),
        "entry": {"standards": ["SOC2-CC6.6"], "accounts_in_test": [{"account_name": ACCOUNT_KEYNAME, "account_id": ACCOUNT_ID, "tags": {}}]},
        "junit_run_id": run_id,
        "testcase_properties": {
            "cats:standard": "SOC2-CC6.6",
            "cats:account": f"{ACCOUNT_KEYNAME} ({ACCOUNT_ID})",
            "cats:description": "The deployed test role answers GetCallerIdentity.",
        },
    }
    assert expected == {
        "accounts": report["run"]["accounts"],
        "counts": report["run"]["counts"],
        "deployment": {key: report["run"][key] for key in ("aws_region", "partition", "cfn_stack_prefix")},
        "outputs": report["run"]["outputs"],
        "writers": report["run"]["writers"],
        "files": run.file_names(),
        "entry": {key: report["tests"][0][key] for key in ("standards", "accounts_in_test")},
        "junit_run_id": junit_properties(testsuite)["cats:run_id"],
        "testcase_properties": junit_properties(testcase),
    }
