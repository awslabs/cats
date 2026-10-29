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

"""Tests for cats.report_writers.securityhub.import_findings: per-account import through the test role.

moto 5.2.3 implements batch_import_findings and get_findings (update-in-place by Id included)
but does not raise InvalidAccessException for a disabled Security Hub and does not enforce the
100-findings-per-call limit, so the happy paths use moto and batching and every failure path
use a mocked client with ClientError side effects.
"""

from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import boto3
import pytest
from boto3.session import Session
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from moto import mock_aws
from pytest_mock import MockerFixture

from cats.cfn.session import BOTO_RETRY_CONFIG
from cats.config import SecurityHubSeverity
from cats.exceptions import CatsInfrastructureError
from cats.report_writers.securityhub import AccountFindings, FindingContext, SecurityHubDelivery, build_account_findings, import_findings
from cats.reporting import AccountReportDetail, ReportEntry
from tests.unit_tests.test_securityhub_build_findings import CONTEXT, make_entry

if TYPE_CHECKING:
    from mypy_boto3_securityhub.client import SecurityHubClient

REGION = "eu-west-1"
PROD_ID = "123456789012"  # moto's account id: findings land only when AwsAccountId matches the caller
PROD = AccountReportDetail(account_name="prod", account_id=PROD_ID, tags={})
DEV = AccountReportDetail(account_name="dev", account_id="222222222222", tags={})


@pytest.fixture
def securityhub() -> Generator["SecurityHubClient", None, None]:
    """A moto Security Hub in eu-west-1, enabled, with the fake session import_findings will be handed."""
    with mock_aws():
        client: SecurityHubClient = boto3.Session(region_name=REGION).client("securityhub")
        client.enable_security_hub()
        yield client


def moto_session(role_arn: str, account_name: str, region: str) -> Session:
    """Stands in for get_test_session: moto answers every client the session creates."""
    _ = role_arn, account_name
    return boto3.Session(region_name=region)


def findings_for(entries: list[ReportEntry], context: FindingContext = CONTEXT) -> list[AccountFindings]:
    accounts, _ = build_account_findings(entries, context, None)
    return accounts


MakeSession = Callable[[str, str, str], Session]


def fake_client(mocker: MockerFixture, *responses: dict[str, Any] | ClientError | BotoCoreError) -> tuple[MagicMock, MakeSession]:
    """A session whose securityhub client answers batch_import_findings with the given responses or exceptions.

    Returns the client mock (to assert on the calls) and a make_session standing in for get_test_session.
    The session mock is reachable as ``client.session`` for tests that assert on how the client was built.
    """
    client = mocker.MagicMock()
    client.batch_import_findings.side_effect = list(responses)
    session = mocker.MagicMock()
    session.client.return_value = client
    client.session = session

    def make_session(role_arn: str, account_name: str, region: str) -> Session:
        fake: Session = session
        return fake

    return client, make_session


def accepted(count: int) -> dict[str, Any]:
    return {"FailedCount": 0, "SuccessCount": count, "FailedFindings": []}


@pytest.fixture
def retry_clock(mocker: MockerFixture) -> dict[str, float]:
    """The writer's time.monotonic and time.sleep under the test's control: each sleep advances the clock by its argument."""
    state = {"now": 0.0}
    mocker.patch("cats.report_writers.securityhub.time.monotonic", side_effect=lambda: state["now"])
    mocker.patch("cats.report_writers.securityhub.time.sleep", side_effect=lambda seconds: state.__setitem__("now", state["now"] + seconds))
    return state


def client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "BatchImportFindings")


def without_timestamp(delivery: SecurityHubDelivery) -> dict[str, Any]:
    """The delivery minus delivered_at, which is the wall clock."""
    return {key: value for key, value in delivery.items() if key != "delivered_at"}


# --- moto: findings land and update in place ---


def test_findings_land_with_compliance_record_state_and_product_fields(securityhub: "SecurityHubClient") -> None:
    # arrange
    accounts = findings_for([make_entry("failed", accounts=[PROD], error_message="open")])

    # act
    [delivery] = import_findings(accounts, CONTEXT, make_session=moto_session)
    [landed] = securityhub.get_findings()["Findings"]

    # assert
    expected = (
        {"status": "imported", "findings_imported": 1, "detail": None},
        {"Status": "FAILED", "RelatedRequirements": ["SOC2-CC6.6"]},
        "ACTIVE",
        "failed",
        "open",
    )
    assert expected == (
        without_timestamp(delivery),
        landed["Compliance"],
        landed["RecordState"],
        landed["ProductFields"]["cats/result"],
        landed["ProductFields"]["cats/error_message"],
    )


def test_a_later_run_updates_the_same_finding_instead_of_adding_one(securityhub: "SecurityHubClient") -> None:
    """Same nodeid, same account: the pass archives the failure, and Security Hub holds one finding."""
    # arrange
    later = FindingContext(**{**CONTEXT.__dict__, "run_id": "20260925T080000Z", "updated_at": "2026-09-25T08:00:00.000Z"})
    first_run = findings_for([make_entry("failed", accounts=[PROD], error_message="open")])
    second_run = findings_for([make_entry("passed", accounts=[PROD])], later)

    # act
    import_findings(first_run, CONTEXT, make_session=moto_session)
    import_findings(second_run, later, make_session=moto_session)
    landed = securityhub.get_findings()["Findings"]

    # assert
    expected = [(first_run[0].findings[0]["Id"], "PASSED", "ARCHIVED", "20260925T080000Z")]
    assert expected == [(f["Id"], f["Compliance"]["Status"], f["RecordState"], f["ProductFields"]["cats/run_id"]) for f in landed]


def test_archived_passes_stay_queryable_as_evidence(securityhub: "SecurityHubClient") -> None:
    # arrange
    accounts = findings_for([make_entry("passed", accounts=[PROD])])

    # act
    import_findings(accounts, CONTEXT, make_session=moto_session)
    archived = securityhub.get_findings(Filters={"RecordState": [{"Value": "ARCHIVED", "Comparison": "EQUALS"}]})["Findings"]

    # assert
    expected = ["CATS"]
    assert expected == [finding["ProductName"] for finding in archived]


# --- batching ---


def test_findings_are_sent_in_batches_of_one_hundred(mocker: MockerFixture) -> None:
    # arrange
    entries = [make_entry("passed", accounts=[PROD], nodeid=f"tests/test_many.py::test_{i}") for i in range(250)]
    client, make_session = fake_client(mocker, accepted(100), accepted(100), accepted(50))

    # act
    [delivery] = import_findings(findings_for(entries), CONTEXT, make_session=make_session)

    # assert
    expected = ([100, 100, 50], {"status": "imported", "findings_imported": 250, "detail": None})
    assert expected == ([len(call.kwargs["Findings"]) for call in client.batch_import_findings.call_args_list], without_timestamp(delivery))


def test_client_is_built_with_adaptive_retries(mocker: MockerFixture) -> None:
    """~1,000 calls per account at the 100k-test scale hit a rate-limited API; botocore backs off for us."""
    # arrange
    client, make_session = fake_client(mocker, accepted(1))

    # act
    import_findings(findings_for([make_entry(accounts=[PROD])]), CONTEXT, make_session=make_session)

    # assert
    expected = ("securityhub", BOTO_RETRY_CONFIG)
    assert expected == (client.session.client.call_args.args[0], client.session.client.call_args.kwargs["config"])


def test_no_accounts_makes_no_calls(mocker: MockerFixture) -> None:
    # arrange
    make_session = mocker.MagicMock()

    # act
    deliveries = import_findings([], CONTEXT, make_session=make_session)

    # assert
    expected: tuple[list[SecurityHubDelivery], int] = ([], 0)
    assert expected == (deliveries, make_session.call_count)


# --- failure paths, one per row of the design's table ---


def test_security_hub_not_enabled_skips_the_account(mocker: MockerFixture) -> None:
    # arrange
    _, make_session = fake_client(mocker, client_error("InvalidAccessException", "Account 123456789012 is not subscribed to AWS Security Hub"))

    # act
    [delivery] = import_findings(findings_for([make_entry(accounts=[PROD])]), CONTEXT, make_session=make_session)

    # assert
    expected = {
        "status": "not_enabled",
        "findings_imported": 0,
        "detail": "InvalidAccessException: Account 123456789012 is not subscribed to AWS Security Hub",
    }
    assert expected == without_timestamp(delivery)


def test_missing_grant_is_retried_for_the_window_then_names_both_causes(mocker: MockerFixture, retry_clock: dict[str, float]) -> None:
    """Denied for the whole window: the stack predates the format, or an update's grant has not converged; both are named."""
    # arrange - every attempt is denied
    client, make_session = fake_client(mocker)
    client.batch_import_findings.side_effect = client_error("AccessDeniedException", "User is not authorized")

    # act
    [delivery] = import_findings(findings_for([make_entry(accounts=[PROD])]), CONTEXT, make_session=make_session)

    # assert - attempts at 0, 3, ..., 30 s: eleven, then the diagnosis
    expected = {
        "status": "access_denied",
        "findings_imported": 0,
        "detail": "Test role lacks securityhub:BatchImportFindings after 30s of retries: the stack was deployed before the securityhub format "
        + "was enabled (redeploy: cats infra deploy prod), or an update that added it has not converged in IAM yet (run again).",
    }
    assert (without_timestamp(delivery), client.batch_import_findings.call_count) == (expected, 11)


def test_grant_converging_during_the_window_imports(mocker: MockerFixture, retry_clock: dict[str, float]) -> None:
    """A stack update that added the securityhub grant: denied once right after the deploy, allowed on the next attempt."""
    # arrange
    client, make_session = fake_client(mocker, client_error("AccessDeniedException", "User is not authorized"), accepted(1))

    # act
    [delivery] = import_findings(findings_for([make_entry(accounts=[PROD])]), CONTEXT, make_session=make_session)

    # assert - one 3 s wait between the two attempts
    expected = {"status": "imported", "findings_imported": 1, "detail": None}
    assert (without_timestamp(delivery), client.batch_import_findings.call_count, retry_clock["now"]) == (expected, 2, 3.0)


def test_other_errors_are_not_retried(mocker: MockerFixture, retry_clock: dict[str, float]) -> None:
    # arrange - InvalidAccessException is "Security Hub is not enabled", which no wait changes
    client, make_session = fake_client(mocker, client_error("InvalidAccessException", "Account is not subscribed to AWS Security Hub"))

    # act
    [delivery] = import_findings(findings_for([make_entry(accounts=[PROD])]), CONTEXT, make_session=make_session)

    # assert
    assert (delivery["status"], client.batch_import_findings.call_count, retry_clock["now"]) == ("not_enabled", 1, 0.0)


def test_any_other_api_error_fails_the_account_with_the_code_and_message(mocker: MockerFixture) -> None:
    """After botocore's retries are spent, the first batch's outcome stands and later batches are not attempted."""
    # arrange
    entries = [make_entry("passed", accounts=[PROD], nodeid=f"tests/test_many.py::test_{i}") for i in range(150)]
    client, make_session = fake_client(mocker, accepted(100), client_error("InternalException", "Internal server error"))

    # act
    [delivery] = import_findings(findings_for(entries), CONTEXT, make_session=make_session)

    # assert
    expected = (2, {"status": "failed", "findings_imported": 100, "detail": "InternalException: Internal server error"})
    assert expected == (client.batch_import_findings.call_count, without_timestamp(delivery))


def test_rejected_findings_fail_the_account_naming_the_first_three(mocker: MockerFixture) -> None:
    # arrange
    entries = [make_entry("passed", accounts=[PROD], nodeid=f"tests/test_many.py::test_{i}") for i in range(5)]
    accounts = findings_for(entries)
    ids = [finding["Id"] for finding in accounts[0].findings]
    rejected = [{"Id": ids[i], "ErrorCode": "InvalidInput", "ErrorMessage": f"bad {i}"} for i in range(4)]
    _, make_session = fake_client(mocker, {"FailedCount": 4, "SuccessCount": 1, "FailedFindings": rejected})

    # act
    [delivery] = import_findings(accounts, CONTEXT, make_session=make_session)

    # assert
    expected = {
        "status": "failed",
        "findings_imported": 1,
        "detail": f"4 of 5 findings rejected: {ids[0]}: InvalidInput bad 0; {ids[1]}: InvalidInput bad 1; {ids[2]}: InvalidInput bad 2; and 1 more",
    }
    assert expected == without_timestamp(delivery)


def test_unassumable_test_role_fails_the_account_with_the_existing_remediation(mocker: MockerFixture) -> None:
    """get_test_session already names cats infra deploy <account>; the delivery carries that text verbatim."""
    # arrange
    error = CatsInfrastructureError("Failed to assume test role 'arn:aws:iam::123456789012:role/CATS-test-infra-prod-default-test-role' ...")
    make_session = mocker.MagicMock(side_effect=error)

    # act
    [delivery] = import_findings(findings_for([make_entry(accounts=[PROD])]), CONTEXT, make_session=make_session)

    # assert
    expected = {"status": "failed", "findings_imported": 0, "detail": str(error)}
    assert expected == without_timestamp(delivery)


def test_one_account_failing_does_not_stop_the_others(mocker: MockerFixture, retry_clock: dict[str, float]) -> None:
    # arrange - prod is denied for the whole retry window; dev imports
    accounts = findings_for([make_entry("failed", accounts=[PROD, DEV], error_message="open")])
    prod_client, prod_make_session = fake_client(mocker)
    prod_client.batch_import_findings.side_effect = client_error("AccessDeniedException")
    sessions = {
        "prod": prod_make_session("", "prod", REGION),
        "dev": fake_client(mocker, accepted(1))[1]("", "dev", REGION),
    }

    def make_session(role_arn: str, account_name: str, region: str) -> Session:
        return sessions[account_name]

    # act
    deliveries = import_findings(accounts, CONTEXT, make_session=make_session)

    # assert
    expected = ["access_denied", "imported"]
    assert expected == [delivery["status"] for delivery in deliveries]


def test_each_account_is_imported_through_its_own_test_role(mocker: MockerFixture) -> None:
    # arrange
    accounts = findings_for([make_entry("passed", accounts=[PROD, DEV])])
    _, make_session = fake_client(mocker, accepted(1), accepted(1))
    spy = mocker.MagicMock(side_effect=make_session)

    # act
    import_findings(accounts, CONTEXT, make_session=spy)

    # assert
    expected = {
        ("arn:aws:iam::123456789012:role/CATS-test-infra-prod-default-test-role", "prod", "eu-west-1"),
        ("arn:aws:iam::222222222222:role/CATS-test-infra-dev-default-test-role", "dev", "eu-west-1"),
    }
    assert expected == {call.args for call in spy.call_args_list}


def test_failed_severity_reaches_the_imported_finding(securityhub: "SecurityHubClient") -> None:
    # arrange
    context = FindingContext(**{**CONTEXT.__dict__, "failed_severity": SecurityHubSeverity.CRITICAL})

    # act
    import_findings(findings_for([make_entry("failed", accounts=[PROD])], context), context, make_session=moto_session)
    [landed] = securityhub.get_findings()["Findings"]

    # assert
    expected = ({"Label": "CRITICAL"}, {"Label": "CRITICAL"})
    assert expected == (landed["Severity"], landed["FindingProviderFields"]["Severity"])


def test_environment_failure_during_import_fails_the_account_instead_of_escaping(mocker: MockerFixture) -> None:
    """A botocore failure that is not an API error (endpoint unreachable) is a recorded delivery status, not an exception."""
    # arrange
    entries = [make_entry("passed", accounts=[PROD])]
    _, make_session = fake_client(mocker, EndpointConnectionError(endpoint_url="https://securityhub.eu-west-1.amazonaws.com"))

    # act
    [delivery] = import_findings(findings_for(entries), CONTEXT, make_session=make_session)

    # assert
    expected = {
        "status": "failed",
        "findings_imported": 0,
        "detail": 'Could not connect to the endpoint URL: "https://securityhub.eu-west-1.amazonaws.com"',
    }
    assert expected == without_timestamp(delivery)
