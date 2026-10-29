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

"""Tests for cats.cfn.deploy module."""

import logging
import re
from collections.abc import Generator
from typing import Any, Literal
from unittest.mock import MagicMock, call

import boto3
import pytest
import typer
from botocore.exceptions import ClientError, WaiterError
from moto import mock_aws
from pytest_mock import MockerFixture

from cats.cfn.deploy import (
    CATS_ACCOUNT_KEYNAME_TAG_KEY,
    CATS_VERSION_TAG_KEY,
    UDP_RECEIPT_TOKEN_PARAMETER_KEY,
    DeployResult,
    PendingStack,
    StackState,
    delete_dead_stack,
    empty_cats_bucket,
    generate_udp_receipt_token,
    get_stack_name,
    get_stack_state,
    get_stack_status,
    poll_pending_stacks,
    read_deployed_version,
    start_deploy,
    start_destroy,
    version_deploy_error,
)
from cats.cfn.events import StackEvent, StackPoller
from cats.cfn.readiness import GATE_PROBE_EVENT, READINESS_TIMEOUT_SECONDS
from cats.version import __version__
from tests.utils import FAKE_ASSUMED_CREDENTIALS, RoleProbeMocks, patch_role_probe

REGION = "us-east-1"
SIMPLE_TEMPLATE = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  TestBucket:
    Type: AWS::S3::Bucket
"""


@pytest.fixture
def aws_session() -> Generator[boto3.Session, None, None]:
    """Provide a moto-mocked boto3 session."""
    with mock_aws():
        yield boto3.Session(region_name=REGION)


@pytest.fixture
def mock_session() -> MagicMock:
    """boto3 Session mock whose client() returns one shared mock for every AWS service."""
    return MagicMock()


@pytest.fixture
def mock_cfn(mock_session: MagicMock) -> MagicMock:
    """AWS client mock returned by mock_session.client() - stands in for both CFN and S3."""
    cfn: MagicMock = mock_session.client.return_value
    return cfn


def call_names(mock_client: MagicMock) -> list[str]:
    """Every API call made on the client mock, in the order they were made."""
    return [name for name, _args, _kwargs in mock_client.method_calls]


# =============================================================================
# get_stack_name
# =============================================================================


def test_get_stack_name_returns_concatenated_string() -> None:
    # arrange
    prefix = "CATS-test-infra-"
    account_name = "myaccount"
    expected = "CATS-test-infra-myaccount"

    # act
    result = get_stack_name(prefix, account_name)

    # assert
    assert result == expected


def test_get_stack_name_maps_underscores_to_hyphens() -> None:
    """Keynames allow `_` (valid Python identifiers); CFN stack names forbid it."""
    # arrange
    prefix = "CATS-test-infra-"
    account_name = "shared_services"
    expected = "CATS-test-infra-shared-services"

    # act
    result = get_stack_name(prefix, account_name)

    # assert
    assert result == expected


# =============================================================================
# _empty_cats_bucket
# =============================================================================


def test_empty_cats_bucket_does_nothing_when_no_bucket_resource(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-stack"
    cfn = aws_session.client("cloudformation", region_name=REGION)
    cfn.create_stack(StackName=stack_name, TemplateBody=SIMPLE_TEMPLATE)

    # act - should not raise
    empty_cats_bucket(aws_session, stack_name, REGION)

    # assert - no exception means success


def test_empty_cats_bucket_deletes_objects(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-stack"
    bucket_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  CatsResponseBucket:
    Type: AWS::S3::Bucket
"""
    cfn = aws_session.client("cloudformation", region_name=REGION)
    cfn.create_stack(StackName=stack_name, TemplateBody=bucket_template)

    # Get the bucket name from stack resources
    resources = cfn.list_stack_resources(StackName=stack_name)
    bucket_name = next(r["PhysicalResourceId"] for r in resources["StackResourceSummaries"] if r["LogicalResourceId"] == "CatsResponseBucket")

    # Put some objects in the bucket
    s3 = aws_session.client("s3", region_name=REGION)
    s3.put_object(Bucket=bucket_name, Key="test-key-1", Body=b"data")
    s3.put_object(Bucket=bucket_name, Key="test-key-2", Body=b"data")

    # act
    empty_cats_bucket(aws_session, stack_name, REGION)

    # assert
    objects = s3.list_objects_v2(Bucket=bucket_name)
    assert objects.get("KeyCount", 0) == 0


def test_empty_cats_bucket_does_nothing_when_bucket_has_no_physical_id(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - a bucket that failed before creation is listed without a PhysicalResourceId
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": [{"LogicalResourceId": "CatsResponseBucket"}]}
    expected = ["list_stack_resources"]

    # act
    empty_cats_bucket(mock_session, "test-stack", REGION)

    # assert - no S3 call was attempted with a missing bucket name
    assert expected == call_names(mock_cfn)


def test_empty_cats_bucket_access_denied_is_a_warning(mock_session: MagicMock, mock_cfn: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    """Objects left behind make the stack's delete stop at DELETE_FAILED on the bucket; the cause is said up front, not buried at DEBUG."""
    # arrange
    mock_cfn.list_stack_resources.return_value = {
        "StackResourceSummaries": [{"LogicalResourceId": "CatsResponseBucket", "PhysicalResourceId": "cats-responses-111111111111"}]
    }
    mock_cfn.get_paginator.return_value.paginate.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "ListObjectsV2"
    )
    expected = (
        "Could not empty the response bucket for test-stack; expect the stack to land in DELETE_FAILED: "
        + "An error occurred (AccessDenied) when calling the ListObjectsV2 operation: Access Denied"
    )

    # act
    with caplog.at_level(logging.WARNING, logger="cats.cfn.deploy"):
        empty_cats_bucket(mock_session, "test-stack", REGION)

    # assert
    assert [(r.levelname, r.message) for r in caplog.records] == [("WARNING", expected)]


def test_empty_cats_bucket_missing_bucket_stays_quiet(mock_session: MagicMock, mock_cfn: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    # arrange - the bucket is already gone: nothing to empty, nothing to warn about
    mock_cfn.list_stack_resources.return_value = {
        "StackResourceSummaries": [{"LogicalResourceId": "CatsResponseBucket", "PhysicalResourceId": "cats-responses-111111111111"}]
    }
    mock_cfn.get_paginator.return_value.paginate.side_effect = ClientError({"Error": {"Code": "NoSuchBucket", "Message": "gone"}}, "ListObjectsV2")

    # act
    with caplog.at_level(logging.WARNING, logger="cats.cfn.deploy"):
        empty_cats_bucket(mock_session, "test-stack", REGION)

    # assert
    assert caplog.records == []


def test_empty_cats_bucket_asserts_owner_on_every_s3_call(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    """Both the list and the destructive delete assert CATS owns the bucket (the session's own identity owns the stack)."""
    # arrange
    owner = "123456789012"
    bucket = "cats-responses-111111111111"
    mock_cfn.get_caller_identity.return_value = {"Account": owner}
    mock_cfn.list_stack_resources.return_value = {
        "StackResourceSummaries": [{"LogicalResourceId": "CatsResponseBucket", "PhysicalResourceId": bucket}]
    }
    mock_cfn.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "receipt-1"}]}]
    expected = {"list_owner": owner, "delete_owner": owner}

    # act
    empty_cats_bucket(mock_session, "test-stack", REGION)

    # assert
    result = {
        "list_owner": mock_cfn.get_paginator.return_value.paginate.call_args.kwargs["ExpectedBucketOwner"],
        "delete_owner": mock_cfn.delete_objects.call_args.kwargs["ExpectedBucketOwner"],
    }
    assert expected == result


# =============================================================================
# get_stack_status
# =============================================================================


def test_get_stack_status_returns_status_when_stack_exists(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-stack"
    cfn = aws_session.client("cloudformation", region_name=REGION)
    cfn.create_stack(StackName=stack_name, TemplateBody=SIMPLE_TEMPLATE)
    expected = "CREATE_COMPLETE"

    # act
    result = get_stack_status(aws_session, stack_name, REGION)

    # assert
    assert result == expected


def test_get_stack_status_returns_none_when_stack_not_exists(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "nonexistent-stack"

    # act
    result = get_stack_status(aws_session, stack_name, REGION)

    # assert
    assert result is None


# =============================================================================
# read_deployed_version
# =============================================================================


def test_read_deployed_version_returns_tag_value() -> None:
    # arrange
    stack = existing_stack("CREATE_COMPLETE", version="0.99.0")["Stacks"][0]
    expected = "0.99.0"

    # act
    result = read_deployed_version(stack)

    # assert
    assert expected == result


def test_read_deployed_version_returns_none_when_tag_absent() -> None:
    # arrange
    stack = existing_stack("CREATE_COMPLETE", version=None)["Stacks"][0]

    # act
    result = read_deployed_version(stack)

    # assert
    assert result is None


def test_read_deployed_version_returns_none_when_stack_has_no_tags_key() -> None:
    # arrange
    stack: dict[str, Any] = {"StackStatus": "CREATE_COMPLETE"}

    # act
    result = read_deployed_version(stack)  # type: ignore[arg-type]

    # assert
    assert result is None


# =============================================================================
# version_deploy_error
# =============================================================================


def test_version_deploy_error_names_both_releases_on_mismatch(mocker: MockerFixture) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    expected = (
        "Stack CATS-prod was deployed by CATS 0.99.0 but CATS 1.0.0 is installed. "
        + "Updating in place would not replace the deployed EC2 listeners or SSM documents. "
        + "Recreate it: cats infra destroy prod && cats infra deploy prod"
    )

    # act
    result = version_deploy_error("CATS-prod", "prod", "0.99.0")

    # assert
    assert expected == result


def test_version_deploy_error_explains_missing_tag(mocker: MockerFixture) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    expected = (
        "Stack CATS-prod has no cats:version tag, so CATS cannot confirm it matches installed CATS 1.0.0. "
        + "It was not created by 'cats infra deploy'. "
        + "Recreate it: cats infra destroy prod && cats infra deploy prod"
    )

    # act
    result = version_deploy_error("CATS-prod", "prod", None)

    # assert
    assert expected == result


# =============================================================================
# get_stack_state
# =============================================================================


def test_get_stack_state_reads_version_from_tags(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", version="0.99.0")
    expected = StackState(status="CREATE_COMPLETE", cats_managed=True, has_udp_receipt_token=True, version="0.99.0", account_keyname=None)

    # act
    result = get_stack_state(mock_session, "test-stack", REGION)

    # assert
    assert expected == result


def test_get_stack_state_version_is_none_when_tag_absent(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", cats_managed=False, has_token=False, version=None)
    expected = StackState(status="CREATE_COMPLETE", cats_managed=False, has_udp_receipt_token=False, version=None, account_keyname=None)

    # act
    result = get_stack_state(mock_session, "test-stack", REGION)

    # assert
    assert expected == result


def test_get_stack_state_returns_none_when_stack_not_exists(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "nonexistent-stack"

    # act
    result = get_stack_state(aws_session, stack_name, REGION)

    # assert
    assert result is None


# =============================================================================
# delete_dead_stack
# =============================================================================


def test_delete_dead_stack_empties_bucket_before_deleting(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - objects left in the response bucket make CloudFormation refuse the deletion
    stack_name = "test-stack"
    mock_cfn.list_stack_resources.return_value = {
        "StackResourceSummaries": [{"LogicalResourceId": "CatsResponseBucket", "PhysicalResourceId": "cats-responses-abc"}]
    }
    mock_cfn.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "receipt"}]}]
    expected = ["list_stack_resources", "get_caller_identity", "get_paginator", "delete_objects", "delete_stack", "get_waiter"]

    # act
    delete_dead_stack(mock_session, mock_cfn, stack_name, REGION)

    # assert
    assert expected == call_names(mock_cfn)


def test_delete_dead_stack_waits_up_to_twenty_minutes(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - Lambda ENI detachment keeps deletions running well past five minutes
    stack_name = "test-stack"
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = [
        mocker.call("stack_delete_complete"),
        mocker.call().wait(StackName=stack_name, WaiterConfig={"Delay": 5, "MaxAttempts": 240}),
    ]

    # act
    delete_dead_stack(mock_session, mock_cfn, stack_name, REGION)

    # assert
    assert expected == mock_cfn.get_waiter.mock_calls


# =============================================================================
# start_deploy
# =============================================================================


def existing_stack(
    status: str, cats_managed: bool = True, has_token: bool = True, version: str | None = __version__, account_keyname: str | None = None
) -> dict[str, Any]:
    """describe_stacks response for one stack. has_token=False models a pre-token CATS; version=None a stack CATS did not create."""
    tags = [{"Key": "cats:managed", "Value": "true"}] if cats_managed else [{"Key": "Name", "Value": "someone-elses-stack"}]
    if version is not None:
        tags.append({"Key": CATS_VERSION_TAG_KEY, "Value": version})
    if account_keyname is not None:
        tags.append({"Key": CATS_ACCOUNT_KEYNAME_TAG_KEY, "Value": account_keyname})
    parameters = [{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "ParameterValue": "0" * 32}] if has_token else []
    return {"Stacks": [{"StackStatus": status, "Tags": tags, "Parameters": parameters}]}


def stack_mutations(mock_cfn: MagicMock) -> list[str]:
    """The create/update/delete calls made on the CFN client, in the order they were made."""
    mutations = ("create_stack", "update_stack", "delete_stack")
    return [name for name in call_names(mock_cfn) if name in mutations]


def test_start_deploy_new_stack_returns_pending(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-start-deploy-new"
    account_name = "test-account"

    # act
    result = start_deploy(aws_session, stack_name, SIMPLE_TEMPLATE, REGION, account_name)

    # assert
    assert isinstance(result, PendingStack)
    assert result.account_name == account_name
    assert result.stack_name == stack_name


def test_start_deploy_no_updates_returns_a_completed_pending_stack_for_the_gate() -> None:
    """A re-run with nothing to change is still gated: its roles and listeners are proven ready before 'deployed'."""
    # arrange
    mock_session = MagicMock()
    mock_cfn = MagicMock()
    mock_session.client.return_value = mock_cfn
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.update_stack.side_effect = ClientError(
        {"Error": {"Code": "ValidationError", "Message": "No updates are to be performed"}},
        "UpdateStack",
    )
    readonly_arn = "arn:aws:iam::111111111111:role/test-stack-readonly"

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name, additional_role_arns=[readonly_arn])

    # assert - already complete at the stack's current status, so polling runs the gate without reading events
    assert isinstance(result, PendingStack)
    expected_poller = StackPoller(mock_cfn, stack_name)
    expected_poller.final_status = "CREATE_COMPLETE"
    expected = PendingStack(
        account_name=account_name,
        stack_name=stack_name,
        poller=expected_poller,
        operation="update",
        ec2=mock_cfn,
        lambda_client=mock_cfn,
        region=REGION,
        additional_role_arns=[readonly_arn],
    )
    assert (result, result.poller.is_complete, result.poller.fetch_new_events()) == (expected, True, [])


def test_start_deploy_invalid_template_returns_failure_result(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-start-deploy-invalid"
    account_name = "test-account"
    invalid_template = "not valid yaml: {"

    # act
    result = start_deploy(aws_session, stack_name, invalid_template, REGION, account_name)

    # assert
    assert isinstance(result, DeployResult)
    assert result.success is False
    assert result.error is not None


def test_start_deploy_disable_rollback_passes_do_nothing() -> None:
    # arrange
    mock_session = MagicMock()
    mock_cfn = MagicMock()
    mock_session.client.return_value = mock_cfn
    stack_name = "test-stack"
    account_name = "test-account"

    # Stack doesn't exist
    describe_error = ClientError(
        {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
        "DescribeStacks",
    )
    mock_cfn.describe_stacks.side_effect = describe_error
    mock_cfn.create_stack.return_value = {"StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test-stack/guid"}

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name, disable_rollback=True)

    # assert
    mock_cfn.create_stack.assert_called_once()
    call_kwargs = mock_cfn.create_stack.call_args.kwargs
    expected_on_failure = "DO_NOTHING"
    assert call_kwargs["OnFailure"] == expected_on_failure


def test_start_deploy_default_rollback_passes_rollback() -> None:
    # arrange
    mock_session = MagicMock()
    mock_cfn = MagicMock()
    mock_session.client.return_value = mock_cfn
    stack_name = "test-stack"
    account_name = "test-account"

    # Stack doesn't exist
    describe_error = ClientError(
        {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
        "DescribeStacks",
    )
    mock_cfn.describe_stacks.side_effect = describe_error
    mock_cfn.create_stack.return_value = {"StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test-stack/guid"}

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    mock_cfn.create_stack.assert_called_once()
    call_kwargs = mock_cfn.create_stack.call_args.kwargs
    expected_on_failure = "ROLLBACK"
    assert call_kwargs["OnFailure"] == expected_on_failure


def test_start_deploy_rollback_complete_stack_recreates_instead_of_updating(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - a failed create leaves the stack in ROLLBACK_COMPLETE, which CFN refuses to update
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = ["delete_stack", "create_stack"]

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_rollback_complete_stack_returns_pending(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = PendingStack(
        account_name=account_name,
        stack_name=stack_name,
        poller=StackPoller(mock_cfn, stack_name),
        operation="create",
        ec2=mock_cfn,
        lambda_client=mock_cfn,
        region=REGION,
    )

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_dead_stack_reports_deletion_to_on_notice(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - the Live display hides log records, so the CLI is told directly
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    notices: list[str] = []
    expected = ["Deleting unrecoverable stack test-stack (ROLLBACK_COMPLETE) before recreating"]

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name, on_notice=notices.append)

    # assert
    assert expected == notices


def test_start_deploy_create_failed_stack_recreates_instead_of_updating(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - a failed create with disable_rollback leaves the stack in CREATE_FAILED
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_FAILED")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = ["delete_stack", "create_stack"]

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name, disable_rollback=True)

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_rollback_failed_stack_recreates_instead_of_updating(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - the rollback of a failed create can itself fail, leaving ROLLBACK_FAILED
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_FAILED")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = ["delete_stack", "create_stack"]

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_delete_failed_stack_recreates_instead_of_updating(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - a destroy blocked by lingering Lambda ENIs leaves DELETE_FAILED behind
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("DELETE_FAILED")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = ["delete_stack", "create_stack"]

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_delete_in_progress_stack_waits_then_creates(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - a destroy whose waiter timed out is still running, and blocks update_stack
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("DELETE_IN_PROGRESS")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    expected = ["describe_stacks", "get_waiter", "create_stack"]

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert - waited for the running deletion, deleted nothing itself, then created
    assert expected == call_names(mock_cfn)


def test_start_deploy_update_rollback_failed_stack_returns_manual_recovery_error(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - this stack holds resources from an earlier successful deploy
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("UPDATE_ROLLBACK_FAILED")
    expected = DeployResult(
        account_name=account_name,
        success=False,
        error=(
            "Stack test-stack is UPDATE_ROLLBACK_FAILED and cannot be deployed. "
            + "It holds resources from an earlier successful deploy, so CATS will not delete it. "
            + "Run 'aws cloudformation continue-update-rollback --stack-name test-stack --region us-east-1' "
            + "using credentials for account test-account, then deploy again."
        ),
    )

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_review_in_progress_stack_returns_manual_recovery_error(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - an unexecuted change set created this stack shell
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("REVIEW_IN_PROGRESS")
    expected = DeployResult(
        account_name=account_name,
        success=False,
        error=(
            "Stack test-stack is REVIEW_IN_PROGRESS and cannot be deployed. "
            + "A change set created it and never executed it. Run 'cats infra destroy test-account', then deploy again."
        ),
    )

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_untagged_dead_stack_returns_not_managed_error(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - a name collision with a stack CATS does not own
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE", cats_managed=False)
    expected = DeployResult(
        account_name=account_name,
        success=False,
        error=(
            "Stack test-stack is ROLLBACK_COMPLETE and cannot be updated, but it is not tagged "
            + "cats:managed=true so CATS will not delete it. "
            + "Delete it yourself, or deploy with a different cfn_stack_prefix."
        ),
    )

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_untagged_dead_stack_deletes_nothing(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE", cats_managed=False)
    expected: list[str] = []

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_healthy_stack_updates_in_place(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    expected = ["update_stack"]

    # act
    start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_untagged_healthy_stack_returns_version_unknown_error(
    mocker: MockerFixture,
    mock_session: MagicMock,
    mock_cfn: MagicMock,
) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", cats_managed=False, version=None)
    expected = DeployResult(
        account_name=account_name,
        success=False,
        error=(
            "Stack test-stack has no cats:version tag, so CATS cannot confirm it matches installed CATS 1.0.0. "
            + "It was not created by 'cats infra deploy'. "
            + "Recreate it: cats infra destroy test-account && cats infra deploy test-account"
        ),
    )

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_healthy_stack_from_other_release_returns_recreate_error(
    mocker: MockerFixture,
    mock_session: MagicMock,
    mock_cfn: MagicMock,
) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("UPDATE_COMPLETE", version="0.99.0")
    expected = DeployResult(
        account_name=account_name,
        success=False,
        error=(
            "Stack test-stack was deployed by CATS 0.99.0 but CATS 1.0.0 is installed. "
            + "Updating in place would not replace the deployed EC2 listeners or SSM documents. "
            + "Recreate it: cats infra destroy test-account && cats infra deploy test-account"
        ),
    )

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_healthy_stack_from_other_release_mutates_nothing(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", version="0.99.0")
    expected: list[str] = []

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_dead_stack_from_other_release_still_recreates(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE", version="0.99.0")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = ["delete_stack", "create_stack"]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_deleting_stack_from_other_release_still_waits_then_creates(
    mocker: MockerFixture,
    mock_session: MagicMock,
    mock_cfn: MagicMock,
) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    mock_cfn.describe_stacks.return_value = existing_stack("DELETE_IN_PROGRESS", version="0.99.0")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    expected = ["create_stack"]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == stack_mutations(mock_cfn)


def test_start_deploy_tags_stack_with_installed_version(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.__version__", "1.0.0")
    mock_cfn.describe_stacks.side_effect = ClientError(
        {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
        "DescribeStacks",
    )
    expected = [
        {"Key": "cats:managed", "Value": "true"},
        {"Key": "cats:account-keyname", "Value": "test-account"},
        {"Key": "cats:version", "Value": "1.0.0"},
    ]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == mock_cfn.create_stack.call_args.kwargs["Tags"]


def test_start_deploy_update_carries_the_same_tags(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    """The update path tags the stack like the create path."""
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    expected = [
        {"Key": "cats:managed", "Value": "true"},
        {"Key": "cats:account-keyname", "Value": "test-account"},
        {"Key": "cats:version", "Value": __version__},
    ]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == mock_cfn.update_stack.call_args.kwargs["Tags"]


@pytest.mark.parametrize("status", ["CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_IN_PROGRESS", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"])
def test_start_deploy_stack_mid_operation_is_refused_with_instructions(status: str, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    """A --all re-run or a CI retry meets a stack still moving; the refusal says what is happening and what to do."""
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack(status)
    expected = DeployResult(
        account_name="test-account",
        success=False,
        error=f"Stack test-stack is {status}: another operation is still running on it. Wait for it to finish "
        + "('cats infra status test-account' shows the state), then deploy again.",
    )

    # act
    result = start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert (result, stack_mutations(mock_cfn)) == (expected, [])


def test_start_deploy_retries_a_throttled_first_describe(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    """Throttling on the first describe reaches the retry decorator instead of being swallowed into a failed result."""
    # arrange
    mocker.patch("cats.cfn.retry.time.sleep")
    mock_cfn.describe_stacks.side_effect = [
        ClientError({"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "DescribeStacks"),
        ClientError({"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}, "DescribeStacks"),
    ]

    # act
    result = start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert (isinstance(result, PendingStack), stack_mutations(mock_cfn)) == (True, ["create_stack"])


def test_start_deploy_dead_stack_delete_error_returns_failure_result(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE")
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    mock_cfn.delete_stack.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform: cloudformation:DeleteStack"}},
        "DeleteStack",
    )
    expected_error = "An error occurred (AccessDenied) when calling the DeleteStack operation: not authorized to perform: cloudformation:DeleteStack"
    expected = DeployResult(account_name=account_name, success=False, error=expected_error)

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_dead_stack_deletion_never_completes_returns_failure_result(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - the delete waiter gives up rather than hanging forever
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE")
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    mock_cfn.get_waiter.return_value.wait.side_effect = WaiterError(
        name="stack_delete_complete",
        reason="Max attempts exceeded",
        last_response={},
    )
    expected = DeployResult(
        account_name=account_name,
        success=False,
        error="Waiter stack_delete_complete failed: Max attempts exceeded",
    )

    # act
    result = start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert
    assert expected == result


def test_start_deploy_interrupt_while_deleting_propagates_exit(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - Ctrl+C reaches this code as typer.Exit, which subclasses RuntimeError
    stack_name = "test-stack"
    account_name = "test-account"
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE")
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    mock_cfn.get_waiter.return_value.wait.side_effect = typer.Exit(130)
    expected_exit_code = 130

    # act
    with pytest.raises(typer.Exit) as exc_info:
        start_deploy(mock_session, stack_name, "template", REGION, account_name)

    # assert - an interrupt must not be reported as a failed deployment
    assert exc_info.value.exit_code == expected_exit_code


# =============================================================================
# UDP receipt token parameter
# =============================================================================


def test_generate_udp_receipt_token_matches_template_allowed_pattern() -> None:
    # arrange - the template parameter declares AllowedPattern ^[0-9a-f]{32}$
    allowed_pattern = r"^[0-9a-f]{32}$"

    # act
    token = generate_udp_receipt_token()

    # assert
    assert re.fullmatch(allowed_pattern, token) is not None


def test_start_deploy_create_passes_generated_token_parameter(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.generate_udp_receipt_token", return_value="a" * 32)
    mock_cfn.describe_stacks.side_effect = ClientError(
        {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
        "DescribeStacks",
    )
    expected = [{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "ParameterValue": "a" * 32}]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == mock_cfn.create_stack.call_args.kwargs["Parameters"]


def test_start_deploy_update_carries_token_forward_with_use_previous_value(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    expected = [{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "UsePreviousValue": True}]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == mock_cfn.update_stack.call_args.kwargs["Parameters"]


def test_start_deploy_pre_token_stack_gets_fresh_token_value(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange - no previous value exists for UsePreviousValue
    mocker.patch("cats.cfn.deploy.generate_udp_receipt_token", return_value="b" * 32)
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", has_token=False)
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    expected = [{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "ParameterValue": "b" * 32}]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == mock_cfn.update_stack.call_args.kwargs["Parameters"]


def test_start_deploy_recreated_dead_stack_gets_fresh_token(mocker: MockerFixture, mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.generate_udp_receipt_token", return_value="c" * 32)
    mock_cfn.describe_stacks.return_value = existing_stack("ROLLBACK_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    expected = [{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "ParameterValue": "c" * 32}]

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "test-account")

    # assert
    assert expected == mock_cfn.create_stack.call_args.kwargs["Parameters"]


# =============================================================================
# start_destroy
# =============================================================================


def test_start_destroy_absent_stack_returns_success_result(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "nonexistent-stack"
    account_name = "test-account"

    # act
    result = start_destroy(aws_session, stack_name, REGION, account_name)

    # assert
    assert isinstance(result, DeployResult)
    assert result.success is True
    assert result.already_absent is True


def test_start_destroy_existing_stack_returns_pending(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-start-destroy-existing"
    account_name = "test-account"
    cfn = aws_session.client("cloudformation", region_name=REGION)
    cfn.create_stack(StackName=stack_name, TemplateBody=SIMPLE_TEMPLATE)

    # act
    result = start_destroy(aws_session, stack_name, REGION, account_name)

    # assert
    assert isinstance(result, PendingStack)
    assert result.account_name == account_name


def test_start_destroy_never_reads_stack_outputs(mock_session: MagicMock, mock_cfn: MagicMock, mocker: MockerFixture) -> None:
    """A 1.0 client must destroy a stack deployed by an older CATS.

    Destroy identifies the stack by describe_stacks (StackId only), finds the bucket via
    list_stack_resources, and calls delete_stack. It never parses Outputs, whose keys are
    the part of the stack shape that changes between releases (e.g. ProbeLambdaName).
    """
    # arrange - describe_stacks returns a pre-1.0 output shape; the test passes only if nobody looks at it
    mock_cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackId": "arn:aws:cloudformation:us-east-1:111111111111:stack/CATS-old/abc",
                "Outputs": [{"OutputKey": "HttpLambdaNameprivate", "OutputValue": "CATS-old-http-private"}],
            }
        ]
    }
    mock_cfn.list_stack_resources.return_value = {"StackResourceSummaries": []}
    mocker.patch("cats.cfn.deploy.StackPoller.seeded")
    expected_calls = ["describe_stacks", "list_stack_resources", "delete_stack"]

    # act
    result = start_destroy(mock_session, "CATS-old", REGION, "old-account")

    # assert
    assert isinstance(result, PendingStack)
    assert [name for name in call_names(mock_cfn) if not name.startswith("describe_stacks().")] == expected_calls


# =============================================================================
# poll_pending_stacks
# =============================================================================


def test_poll_pending_stacks_single_stack_returns_result(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-poll-single"
    account_name = "test-account"
    pending_result = start_deploy(aws_session, stack_name, SIMPLE_TEMPLATE, REGION, account_name)
    assert isinstance(pending_result, PendingStack)
    pending = {account_name: pending_result}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.1)

    # assert
    assert len(results) == 1
    assert account_name in results
    assert results[account_name].success is True


def test_poll_pending_stacks_multiple_stacks_returns_all_results(aws_session: boto3.Session) -> None:
    # arrange
    pending: dict[str, PendingStack] = {}
    for i in range(2):
        name = f"account{i}"
        stack_name = f"test-poll-multi-{i}"
        result = start_deploy(aws_session, stack_name, SIMPLE_TEMPLATE, REGION, name)
        assert isinstance(result, PendingStack)
        pending[name] = result

    # act
    results = poll_pending_stacks(pending, poll_interval=0.1)

    # assert
    assert len(results) == 2
    assert results["account0"].success is True
    assert results["account1"].success is True


def test_poll_pending_stacks_calls_on_event_callback(aws_session: boto3.Session) -> None:
    # arrange
    stack_name = "test-poll-callback"
    account_name = "test-account"
    pending_result = start_deploy(aws_session, stack_name, SIMPLE_TEMPLATE, REGION, account_name)
    assert isinstance(pending_result, PendingStack)
    pending = {account_name: pending_result}
    received_events: list[tuple[str, StackEvent]] = []

    def on_event(name: str, event: StackEvent) -> None:
        received_events.append((name, event))

    # act
    poll_pending_stacks(pending, on_event=on_event, poll_interval=0.1)

    # assert
    assert len(received_events) > 0
    assert all(name == account_name for name, _ in received_events)


def test_poll_pending_stacks_timeout_returns_failure(mocker: MockerFixture) -> None:
    # arrange
    account_name = "test-account"
    mock_poller = mocker.MagicMock()
    mock_poller.fetch_new_events.return_value = []
    mock_poller.is_complete = False  # Never completes

    pending = {
        account_name: PendingStack(
            account_name=account_name,
            stack_name="test-stack",
            poller=mock_poller,
            operation="create",
        )
    }

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01, timeout=0.02)

    # assert
    assert len(results) == 1
    assert results[account_name].success is False
    assert "Timeout" in (results[account_name].error or "")


def _stack_does_not_exist() -> ClientError:
    return ClientError({"Error": {"Code": "ValidationError", "Message": "Stack with id test-stack does not exist"}}, "DescribeStackEvents")


def test_poll_pending_stacks_vanished_stack_is_success_for_a_delete(mocker: MockerFixture) -> None:
    # arrange - the stack is gone, which is what a delete was for
    mock_poller = mocker.MagicMock()
    mock_poller.fetch_new_events.side_effect = _stack_does_not_exist()
    pending = {"acc": PendingStack(account_name="acc", stack_name="test-stack", poller=mock_poller, operation="delete")}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert results == {"acc": DeployResult(account_name="acc", success=True)}


@pytest.mark.parametrize("operation", ["create", "update"])
def test_poll_pending_stacks_vanished_stack_is_failure_for_create_and_update(mocker: MockerFixture, operation: Literal["create", "update"]) -> None:
    # arrange - a create or update whose stack disappears mid-poll was deleted from under us; reporting success would hide that
    mock_poller = mocker.MagicMock()
    mock_poller.fetch_new_events.side_effect = _stack_does_not_exist()
    pending = {"acc": PendingStack(account_name="acc", stack_name="test-stack", poller=mock_poller, operation=operation)}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    expected_error = (
        f"Stack test-stack disappeared during {operation}: An error occurred (ValidationError) when calling the "
        + "DescribeStackEvents operation: Stack with id test-stack does not exist"
    )
    assert results == {"acc": DeployResult(account_name="acc", success=False, error=expected_error)}


# =============================================================================
# poll_pending_stacks: the readiness gate
# =============================================================================


def _complete_poller(status: str, outputs: list[dict[str, str]]) -> MagicMock:
    """A poller whose stack is already at `status`, with `outputs` on the stack CloudFormation describes."""
    poller = MagicMock()
    poller.fetch_new_events.return_value = []
    poller.is_complete = True
    poller.final_status = status
    poller.cfn_client.describe_stacks.return_value = {"Stacks": [{"StackStatus": status, "Outputs": outputs}]}
    return poller


def _readiness_ec2(console: str = "cloud-init[1644]: cats listeners ready\n") -> MagicMock:
    """An EC2 client whose one listener's serial console reads `console` (boto3 hands Output back decoded)."""
    ec2 = MagicMock()
    ec2.get_console_output.return_value = {"InstanceId": "i-0aaa", "Output": console}
    return ec2


def _pending(
    poller: MagicMock,
    ec2: MagicMock,
    account_name: str = "acc",
    stack_name: str = "test-stack",
    additional_role_arns: list[str] | None = None,
    lambda_client: MagicMock | None = None,
) -> PendingStack:
    """The PendingStack start_deploy returns for a create: an EC2 client, a Lambda client, the region, and the additional roles to probe."""
    return PendingStack(
        account_name=account_name,
        stack_name=stack_name,
        poller=poller,
        operation="create",
        ec2=ec2,
        lambda_client=lambda_client,
        region=REGION,
        additional_role_arns=[] if additional_role_arns is None else additional_role_arns,
    )


LISTENER_OUTPUTS = [{"OutputKey": "Ec2InstanceIdprivate", "OutputValue": "i-0aaa"}]
TEST_ROLE_ARN = "arn:aws:iam::111111111111:role/test-stack-default-test-role"
READONLY_ROLE_ARN = "arn:aws:iam::111111111111:role/test-stack-readonly"
TEST_ROLE_OUTPUTS = [{"OutputKey": "TestRoleArn", "OutputValue": TEST_ROLE_ARN}]
PROBE_LAMBDA_NAME = "test-stack-probe-private"
LAMBDA_OUTPUTS = [{"OutputKey": "ProbeLambdaNameprivate", "OutputValue": PROBE_LAMBDA_NAME}]
# Lambda's wording when it cannot yet assume a freshly created execution role.
LAMBDA_CANNOT_BE_ASSUMED = ClientError(
    {"Error": {"Code": "AccessDeniedException", "Message": "The role defined for the function cannot be assumed by Lambda."}}, "Invoke"
)


@pytest.fixture
def role_probe(mocker: MockerFixture) -> RoleProbeMocks:
    """The role gate's STS and CloudFormation clients; every probe succeeds unless a test says otherwise."""
    return patch_role_probe(mocker, "test-stack")


def test_poll_pending_stacks_create_with_listeners_succeeds_only_after_they_are_ready(mocker: MockerFixture) -> None:
    # arrange
    mocker.patch("cats.cfn.deploy.time.sleep")
    ec2 = _readiness_ec2()
    poller = _complete_poller("CREATE_COMPLETE", LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, ec2)}
    notices: list[tuple[str, str]] = []

    # act
    results = poll_pending_stacks(pending, on_notice=lambda name, message: notices.append((name, message)), poll_interval=0.01)

    # assert
    expected_notices = [("acc", "EC2 listener i-0aaa (private) ready")]
    expected_calls = [call.get_console_output(InstanceId="i-0aaa", Latest=True)]
    assert (results, notices, ec2.method_calls) == ({"acc": DeployResult(account_name="acc", success=True)}, expected_notices, expected_calls)


def test_poll_pending_stacks_listener_that_never_becomes_ready_fails_the_deploy(mocker: MockerFixture) -> None:
    # arrange - the stack is CREATE_COMPLETE; its listener's user data reports the unbound port on the console
    mocker.patch("cats.cfn.deploy.time.sleep")
    mocker.patch("cats.cfn.readiness.time.monotonic", return_value=100.0)
    ec2 = _readiness_ec2(console="cloud-init[1644]: cats listeners not bound: tcp/8080\ncloud-init[1644]: x tcpserver.service - CATS TCP listener\n")
    poller = _complete_poller("CREATE_COMPLETE", LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, ec2)}
    expected_error = (
        "Stack test-stack reached CREATE_COMPLETE but its listeners are not ready. "
        + "EC2 listener i-0aaa (private) is not ready after 0s: its user data found listener ports still unbound. Console:\n"
        + "  cloud-init[1644]: cats listeners not bound: tcp/8080\n"
        + "  cloud-init[1644]: x tcpserver.service - CATS TCP listener "
        + "Recreate it: cats infra destroy acc && cats infra deploy acc"
    )

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert results == {"acc": DeployResult(account_name="acc", success=False, error=expected_error)}


def test_poll_pending_stacks_stack_without_listeners_is_gated_on_its_test_role(role_probe: RoleProbeMocks) -> None:
    # arrange - no network targets: nothing to boot, but the test role must be assumable and authorized before 'deployed'
    ec2 = _readiness_ec2()
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS)
    pending = {"acc": _pending(poller, ec2)}
    notices: list[tuple[str, str]] = []

    # act
    results = poll_pending_stacks(pending, on_notice=lambda name, message: notices.append((name, message)), poll_interval=0.01)

    # assert
    expected_sts_calls = [call.assume_role(RoleArn=TEST_ROLE_ARN, RoleSessionName="cats-infra")]
    expected_notices = [("acc", "Test role test-stack-default-test-role authorized")]
    assert (results, notices, role_probe.sts.method_calls, role_probe.cfn.describe_stacks.call_args_list, ec2.method_calls) == (
        {"acc": DeployResult(account_name="acc", success=True)},
        expected_notices,
        expected_sts_calls,
        [call(StackName="test-stack")],
        [],
    )


def test_poll_pending_stacks_test_role_never_authorized_fails_the_deploy_with_the_role_remediation(
    mocker: MockerFixture, role_probe: RoleProbeMocks
) -> None:
    # arrange - the role is assumable but its grant is still denied when the bound passes
    clock = {"now": 100.0}
    mocker.patch("cats.cfn.readiness.time.monotonic", side_effect=lambda: clock["now"])
    role_probe.cfn.describe_stacks.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform: cloudformation:DescribeStacks"}}, "DescribeStacks"
    )
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS)
    pending = {"acc": _pending(poller, _readiness_ec2())}
    expected_error = (
        "Stack test-stack reached CREATE_COMPLETE but its test role is not usable yet. "
        + f"Test role {TEST_ROLE_ARN} was assumed but cloudformation:DescribeStacks on the stack is still denied after 180s: "
        + "not authorized to perform: cloudformation:DescribeStacks. Either its CatsTestRolePolicy has not converged in IAM yet, "
        + "or a service control policy or permissions boundary denies it. Fix the cause, then deploy again: cats infra deploy acc"
    )

    # act - cycle 1 assumes and is denied; the clock then passes the bound and cycle 2 fails
    def pass_the_bound(_seconds: float) -> None:
        clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS

    mocker.patch("cats.cfn.deploy.time.sleep", side_effect=pass_the_bound)
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert results == {"acc": DeployResult(account_name="acc", success=False, error=expected_error)}


def test_poll_pending_stacks_additional_roles_are_probed_for_assumability_only(role_probe: RoleProbeMocks) -> None:
    # arrange - the stack created a test role and one additional role
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS)
    pending = {"acc": _pending(poller, _readiness_ec2(), additional_role_arns=[READONLY_ROLE_ARN])}
    notices: list[tuple[str, str]] = []

    # act
    results = poll_pending_stacks(pending, on_notice=lambda name, message: notices.append((name, message)), poll_interval=0.01)

    # assert - both roles assumed, DescribeStacks once (the test role's grant), the additional role's grants untouched
    expected_sts_calls = [
        call.assume_role(RoleArn=TEST_ROLE_ARN, RoleSessionName="cats-infra"),
        call.assume_role(RoleArn=READONLY_ROLE_ARN, RoleSessionName="cats-infra"),
    ]
    expected_notices = [("acc", "Test role test-stack-default-test-role authorized"), ("acc", "Role test-stack-readonly assumable")]
    assert (results, notices, role_probe.sts.method_calls, role_probe.cfn.describe_stacks.call_count) == (
        {"acc": DeployResult(account_name="acc", success=True)},
        expected_notices,
        expected_sts_calls,
        1,
    )


def test_poll_pending_stacks_with_listeners_and_a_role_needs_both_ready(mocker: MockerFixture, role_probe: RoleProbeMocks) -> None:
    # arrange - the role authorizes on cycle 1; the listener's console is silent on cycle 1 and ready on cycle 2
    sleep = mocker.patch("cats.cfn.deploy.time.sleep")
    ec2 = _readiness_ec2()
    ec2.get_console_output.side_effect = [{"InstanceId": "i-0aaa"}, {"InstanceId": "i-0aaa", "Output": "cats listeners ready\n"}]
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, ec2)}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert - one sleep between the two cycles; the role was probed exactly once
    assert (results, sleep.call_count, ec2.get_console_output.call_count, role_probe.sts.assume_role.call_count) == (
        {"acc": DeployResult(account_name="acc", success=True)},
        1,
        2,
        1,
    )


def test_poll_pending_stacks_with_a_role_and_listeners_waits_for_the_role_after_the_listeners(
    mocker: MockerFixture, role_probe: RoleProbeMocks
) -> None:
    # arrange - the listener is ready on cycle 1; the role is denied on cycle 1 and authorized on cycle 2
    sleep = mocker.patch("cats.cfn.deploy.time.sleep")
    role_probe.sts.assume_role.side_effect = [
        ClientError({"Error": {"Code": "AccessDenied", "Message": "not authorized to perform: sts:AssumeRole"}}, "AssumeRole"),
        {"Credentials": FAKE_ASSUMED_CREDENTIALS},
    ]
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, _readiness_ec2())}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert (results, sleep.call_count, role_probe.sts.assume_role.call_count) == ({"acc": DeployResult(account_name="acc", success=True)}, 1, 2)


def test_poll_pending_stacks_probe_lambda_proven_invocable_before_deployed(role_probe: RoleProbeMocks) -> None:
    # arrange - a lambda target: the probe's execution role must be assumable by Lambda before 'deployed'
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 200, "Payload": MagicMock()}
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LAMBDA_OUTPUTS)
    pending = {"acc": _pending(poller, _readiness_ec2(), lambda_client=lambda_client)}
    notices: list[tuple[str, str]] = []

    # act
    results = poll_pending_stacks(pending, on_notice=lambda name, message: notices.append((name, message)), poll_interval=0.01)

    # assert - the probe was invoked once with the loopback gate event; roles are reported before lambdas
    expected_invoke = [call.invoke(FunctionName=PROBE_LAMBDA_NAME, InvocationType="RequestResponse", Payload=GATE_PROBE_EVENT)]
    expected_notices = [("acc", "Test role test-stack-default-test-role authorized"), ("acc", f"Probe Lambda {PROBE_LAMBDA_NAME} invocable")]
    assert (results, notices, lambda_client.method_calls) == (
        {"acc": DeployResult(account_name="acc", success=True)},
        expected_notices,
        expected_invoke,
    )


def test_poll_pending_stacks_probe_lambda_never_invocable_fails_with_the_lambda_remediation(
    mocker: MockerFixture, role_probe: RoleProbeMocks
) -> None:
    # arrange - Lambda cannot assume the execution role for the whole bound
    clock = {"now": 100.0}
    mocker.patch("cats.cfn.readiness.time.monotonic", side_effect=lambda: clock["now"])
    lambda_client = MagicMock()
    lambda_client.invoke.side_effect = LAMBDA_CANNOT_BE_ASSUMED
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LAMBDA_OUTPUTS)
    pending = {"acc": _pending(poller, _readiness_ec2(), lambda_client=lambda_client)}
    expected_error = (
        "Stack test-stack reached CREATE_COMPLETE but a probe Lambda is not usable yet. "
        + f"Probe Lambda {PROBE_LAMBDA_NAME}'s execution role could not be assumed by Lambda after 180s: "
        + "The role defined for the function cannot be assumed by Lambda.. Either its trust policy has not converged in IAM yet, "
        + "or a service control policy or permissions boundary prevents Lambda from assuming it. "
        + "Fix the cause, then deploy again: cats infra deploy acc"
    )

    # act - cycle 1 invokes and is denied; the clock then passes the bound and cycle 2 fails
    def pass_the_bound(_seconds: float) -> None:
        clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS

    mocker.patch("cats.cfn.deploy.time.sleep", side_effect=pass_the_bound)
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert results == {"acc": DeployResult(account_name="acc", success=False, error=expected_error)}


def test_poll_pending_stacks_with_a_role_a_lambda_and_a_listener_needs_all_three_ready(mocker: MockerFixture, role_probe: RoleProbeMocks) -> None:
    # arrange - all three resources are ready on cycle 1
    mocker.patch("cats.cfn.deploy.time.sleep")
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 200, "Payload": MagicMock()}
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LAMBDA_OUTPUTS + LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, _readiness_ec2(), lambda_client=lambda_client)}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert - role assumed once, probe invoked once, listener console read once
    assert (results, role_probe.sts.assume_role.call_count, lambda_client.invoke.call_count) == (
        {"acc": DeployResult(account_name="acc", success=True)},
        1,
        1,
    )


def test_poll_pending_stacks_failed_stack_is_not_gated() -> None:
    # arrange - a rolled-back stack has no listeners to wait for, and its outputs are not read
    ec2 = _readiness_ec2()
    poller = _complete_poller("ROLLBACK_COMPLETE", LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, ec2)}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    expected = {"acc": DeployResult(account_name="acc", success=False, error="ROLLBACK_COMPLETE")}
    assert (results, ec2.method_calls, poller.cfn_client.describe_stacks.call_count) == (expected, [], 0)


def test_poll_pending_stacks_delete_is_not_gated(role_probe: RoleProbeMocks) -> None:
    # arrange - a delete carries no EC2 client, region or roles; the stack's outputs are gone with it
    poller = _complete_poller("DELETE_COMPLETE", [])
    pending = {"acc": PendingStack(account_name="acc", stack_name="test-stack", poller=poller, operation="delete")}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert - and STS is never touched: a delete needs no test role
    assert (results, poller.cfn_client.describe_stacks.call_count, role_probe.attributed_session.call_count) == (
        {"acc": DeployResult(account_name="acc", success=True)},
        0,
        0,
    )


def test_poll_pending_stacks_no_op_update_is_gated_without_reading_events(role_probe: RoleProbeMocks) -> None:
    # arrange - the PendingStack start_deploy returns for "No updates are to be performed": complete before polling begins
    cfn = MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_COMPLETE", "Outputs": TEST_ROLE_OUTPUTS}]}
    poller = StackPoller(cfn, "test-stack")
    poller.final_status = "UPDATE_COMPLETE"
    pending = {
        "acc": PendingStack(account_name="acc", stack_name="test-stack", poller=poller, operation="update", ec2=_readiness_ec2(), region=REGION)
    }
    events: list[StackEvent] = []

    # act
    results = poll_pending_stacks(pending, on_event=lambda _name, event: events.append(event), poll_interval=0.01)

    # assert - the role was probed; no stack events were fetched or reported
    assert (results, events, cfn.describe_stack_events.call_count, role_probe.sts.assume_role.call_count) == (
        {"acc": DeployResult(account_name="acc", success=True)},
        [],
        0,
        1,
    )


def test_poll_pending_stacks_no_op_update_of_a_rolled_back_stack_reports_its_status(role_probe: RoleProbeMocks) -> None:
    """Deliberate change: a re-run against UPDATE_ROLLBACK_COMPLETE was 'No updates needed'; its last update failed, so say so."""
    # arrange
    cfn = MagicMock()
    poller = StackPoller(cfn, "test-stack")
    poller.final_status = "UPDATE_ROLLBACK_COMPLETE"
    pending = {
        "acc": PendingStack(account_name="acc", stack_name="test-stack", poller=poller, operation="update", ec2=_readiness_ec2(), region=REGION)
    }

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert - not gated: a failed stack's outputs are not read and its roles are not probed
    assert (results, cfn.describe_stacks.call_count, role_probe.attributed_session.call_count) == (
        {"acc": DeployResult(account_name="acc", success=False, error="UPDATE_ROLLBACK_COMPLETE")},
        0,
        0,
    )


def test_poll_pending_stacks_stack_with_neither_listeners_nor_test_role_is_not_gated(role_probe: RoleProbeMocks) -> None:
    # arrange - not a CATS template (no TestRoleArn output): deployed when CloudFormation says so
    poller = _complete_poller("CREATE_COMPLETE", [{"OutputKey": "BucketName", "OutputValue": "bucket"}])
    pending = {"acc": _pending(poller, _readiness_ec2())}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert (results, role_probe.attributed_session.call_count) == ({"acc": DeployResult(account_name="acc", success=True)}, 0)


def test_poll_pending_stacks_two_stacks_advance_through_readiness_in_the_same_cycles(mocker: MockerFixture, role_probe: RoleProbeMocks) -> None:
    # arrange - both stacks are complete and both consoles are still silent on the first read, then both report ready;
    # both test roles authorize on the first cycle
    sleep = mocker.patch("cats.cfn.deploy.time.sleep")
    pending: dict[str, PendingStack] = {}
    ec2s: dict[str, MagicMock] = {}
    for name in ("acc1", "acc2"):
        ec2s[name] = _readiness_ec2()
        ec2s[name].get_console_output.side_effect = [{"InstanceId": "i-0aaa"}, {"InstanceId": "i-0aaa", "Output": "cats listeners ready\n"}]
        poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LISTENER_OUTPUTS)
        pending[name] = _pending(poller, ec2s[name], account_name=name, stack_name=f"stack-{name}")

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert - cycle 1: both stacks complete, both roles authorized, both consoles read once; cycle 2: both ready. One sleep between.
    expected_results = {name: DeployResult(account_name=name, success=True) for name in ("acc1", "acc2")}
    call_counts = (ec2s["acc1"].get_console_output.call_count, ec2s["acc2"].get_console_output.call_count)
    assert (results, sleep.call_count, call_counts, role_probe.sts.assume_role.call_count) == (expected_results, 1, (2, 2), 2)


def test_poll_pending_stacks_ec2_error_during_readiness_fails_that_stack(mocker: MockerFixture) -> None:
    # arrange - the deploy identity cannot read the console: that is a permissions failure, reported as such
    mocker.patch("cats.cfn.deploy.time.sleep")
    ec2 = _readiness_ec2()
    ec2.get_console_output.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "You are not authorized to perform this operation."}}, "GetConsoleOutput"
    )
    poller = _complete_poller("CREATE_COMPLETE", LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, ec2)}
    expected_error = (
        "An error occurred (UnauthorizedOperation) when calling the GetConsoleOutput operation: You are not authorized to perform this operation."
    )

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert results == {"acc": DeployResult(account_name="acc", success=False, error=expected_error)}


def test_poll_pending_stacks_sts_error_during_readiness_fails_that_stack(role_probe: RoleProbeMocks) -> None:
    # arrange - the environment's own credentials are gone: the caller's problem, reported as such, not waited out
    role_probe.sts.assume_role.side_effect = ClientError(
        {"Error": {"Code": "ExpiredToken", "Message": "The security token included in the request is expired"}}, "AssumeRole"
    )
    poller = _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS)
    pending = {"acc": _pending(poller, _readiness_ec2())}
    expected_error = "An error occurred (ExpiredToken) when calling the AssumeRole operation: The security token included in the request is expired"

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01)

    # assert
    assert results == {"acc": DeployResult(account_name="acc", success=False, error=expected_error)}


def test_poll_pending_stacks_timeout_while_readying_names_the_listeners_and_role(mocker: MockerFixture) -> None:
    # arrange - the stack completed, the listener's console stays empty, and the overall deploy timeout arrives first.
    # Both modules read the one global time.monotonic; each patched sleep moves it forward 10s.
    clock = {"now": 0.0}
    mocker.patch("cats.cfn.deploy.time.monotonic", side_effect=lambda: clock["now"])
    sleep = mocker.patch("cats.cfn.deploy.time.sleep", side_effect=lambda _seconds: clock.__setitem__("now", clock["now"] + 10.0))
    ec2 = _readiness_ec2()
    ec2.get_console_output.return_value = {"InstanceId": "i-0aaa"}
    poller = _complete_poller("CREATE_COMPLETE", LISTENER_OUTPUTS)
    pending = {"acc": _pending(poller, ec2)}

    # act
    results = poll_pending_stacks(pending, poll_interval=0.01, timeout=5.0)

    # assert - one cycle reaches readiness and finds the console empty; the sleep carries the clock past the timeout
    expected = {
        "acc": DeployResult(
            account_name="acc",
            success=False,
            error="Timeout after 5s - the stack is complete but its EC2 listeners or test role were still not ready",
        )
    }
    assert (results, sleep.call_count, ec2.get_console_output.call_count) == (expected, 1, 1)


# =============================================================================
# poll_pending_stacks: on_result, each result reported the cycle it is written
# =============================================================================


def test_poll_pending_stacks_reports_a_result_the_cycle_it_is_written_not_when_the_batch_ends(
    mocker: MockerFixture, role_probe: RoleProbeMocks
) -> None:
    # arrange - acc1 has no listeners and its role authorizes on cycle 1; acc2's listener is silent on cycle 1 and ready on cycle 2
    sleep = mocker.patch("cats.cfn.deploy.time.sleep")
    ec2 = _readiness_ec2()
    ec2.get_console_output.side_effect = [{"InstanceId": "i-0aaa"}, {"InstanceId": "i-0aaa", "Output": "cats listeners ready\n"}]
    pending = {
        "acc1": _pending(_complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS), _readiness_ec2(), account_name="acc1", stack_name="stack-acc1"),
        "acc2": _pending(
            _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LISTENER_OUTPUTS), ec2, account_name="acc2", stack_name="stack-acc2"
        ),
    }
    reported: list[tuple[DeployResult, int]] = []  # (result, poll cycles slept before it was reported)

    # act
    results = poll_pending_stacks(pending, on_result=lambda result: reported.append((result, sleep.call_count)), poll_interval=0.01)

    # assert - acc1 before the first sleep, acc2 after it
    expected_reported = [(DeployResult(account_name="acc1", success=True), 0), (DeployResult(account_name="acc2", success=True), 1)]
    assert (reported, results) == (expected_reported, {result.account_name: result for result, _cycle in expected_reported})


def test_poll_pending_stacks_reports_a_gate_failure_while_other_stacks_still_wait(mocker: MockerFixture, role_probe: RoleProbeMocks) -> None:
    # arrange - acc1's role is explicitly denied (fails on cycle 1); acc2's listener is ready on cycle 2
    sleep = mocker.patch("cats.cfn.deploy.time.sleep")
    role_probe.sts.assume_role.side_effect = [
        ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "User is not authorized to perform: sts:AssumeRole with an explicit deny"}}, "AssumeRole"
        ),
        {"Credentials": FAKE_ASSUMED_CREDENTIALS},
    ]
    ec2 = _readiness_ec2()
    ec2.get_console_output.side_effect = [{"InstanceId": "i-0aaa"}, {"InstanceId": "i-0aaa", "Output": "cats listeners ready\n"}]
    pending = {
        "acc1": _pending(_complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS), _readiness_ec2(), account_name="acc1", stack_name="stack-acc1"),
        "acc2": _pending(
            _complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LISTENER_OUTPUTS), ec2, account_name="acc2", stack_name="stack-acc2"
        ),
    }
    reported: list[tuple[str, bool, int]] = []  # (account, success, poll cycles slept before it was reported)

    # act
    poll_pending_stacks(
        pending, on_result=lambda result: reported.append((result.account_name, result.success, sleep.call_count)), poll_interval=0.01
    )

    # assert
    assert reported == [("acc1", False, 0), ("acc2", True, 1)]


def test_poll_pending_stacks_reports_every_result_it_returns(mocker: MockerFixture, role_probe: RoleProbeMocks) -> None:
    # arrange - one stack per way a result is written on a cycle: complete without a gate, rolled back, vanished during a delete, gated
    mocker.patch("cats.cfn.deploy.time.sleep")
    vanished = MagicMock()
    vanished.fetch_new_events.side_effect = _stack_does_not_exist()
    pending = {
        "ungated": _pending(
            _complete_poller("CREATE_COMPLETE", [{"OutputKey": "BucketName", "OutputValue": "bucket"}]), _readiness_ec2(), account_name="ungated"
        ),
        "rolled_back": _pending(_complete_poller("ROLLBACK_COMPLETE", []), _readiness_ec2(), account_name="rolled_back"),
        "deleted": PendingStack(account_name="deleted", stack_name="test-stack", poller=vanished, operation="delete"),
        "gated": _pending(_complete_poller("CREATE_COMPLETE", TEST_ROLE_OUTPUTS + LISTENER_OUTPUTS), _readiness_ec2(), account_name="gated"),
    }
    reported: list[DeployResult] = []

    # act
    results = poll_pending_stacks(pending, on_result=reported.append, poll_interval=0.01)

    # assert
    expected = [
        DeployResult(account_name="ungated", success=True),
        DeployResult(account_name="rolled_back", success=False, error="ROLLBACK_COMPLETE"),
        DeployResult(account_name="deleted", success=True),
        DeployResult(account_name="gated", success=True),
    ]
    assert (reported, results) == (expected, {result.account_name: result for result in expected})


def test_poll_pending_stacks_reports_both_kinds_of_timeout(mocker: MockerFixture) -> None:
    # arrange - one stack never completes, the other completed but its listener's console stays empty; the deploy timeout ends both
    clock = {"now": 0.0}
    mocker.patch("cats.cfn.deploy.time.monotonic", side_effect=lambda: clock["now"])
    mocker.patch("cats.cfn.deploy.time.sleep", side_effect=lambda _seconds: clock.__setitem__("now", clock["now"] + 10.0))
    never_complete = MagicMock()
    never_complete.fetch_new_events.return_value = []
    never_complete.is_complete = False
    ec2 = _readiness_ec2()
    ec2.get_console_output.return_value = {"InstanceId": "i-0aaa"}
    pending = {
        "stuck": PendingStack(account_name="stuck", stack_name="stuck-stack", poller=never_complete, operation="create"),
        "silent": _pending(_complete_poller("CREATE_COMPLETE", LISTENER_OUTPUTS), ec2, account_name="silent"),
    }
    reported: list[DeployResult] = []

    # act
    results = poll_pending_stacks(pending, on_result=reported.append, poll_interval=0.01, timeout=5.0)

    # assert
    expected = [
        DeployResult(account_name="stuck", success=False, error="Timeout after 5s - stack may still be in progress"),
        DeployResult(
            account_name="silent",
            success=False,
            error="Timeout after 5s - the stack is complete but its EC2 listeners or test role were still not ready",
        ),
    ]
    assert (reported, results) == (expected, {result.account_name: result for result in expected})


def test_start_deploy_pending_stack_records_a_create(aws_session: boto3.Session) -> None:
    # act
    result = start_deploy(aws_session, "test-op-create", SIMPLE_TEMPLATE, REGION, "acc")

    # assert
    assert isinstance(result, PendingStack)
    assert result.operation == "create"


def test_start_destroy_pending_stack_records_a_delete(aws_session: boto3.Session) -> None:
    # arrange
    created = start_deploy(aws_session, "test-op-delete", SIMPLE_TEMPLATE, REGION, "acc")
    assert isinstance(created, PendingStack)
    poll_pending_stacks({"acc": created}, poll_interval=0.1)

    # act
    result = start_destroy(aws_session, "test-op-delete", REGION, "acc")

    # assert
    assert isinstance(result, PendingStack)
    assert result.operation == "delete"


def test_start_deploy_refuses_a_stack_another_keyname_deployed(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    """'CATS-' + 'aprod' and 'CATS-a' + 'prod' name one stack; the keyname tag tells them apart."""
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", account_keyname="aprod")
    expected = DeployResult(
        account_name="prod",
        success=False,
        error=(
            "Stack test-stack was deployed for account keyname 'aprod', not 'prod': "
            + "the two names concatenate with cfn_stack_prefix to the same stack name. "
            + "Deploy with a different cfn_stack_prefix, or rename one of the keynames."
        ),
    )

    # act
    result = start_deploy(mock_session, "test-stack", "template", REGION, "prod")

    # assert
    assert (result, stack_mutations(mock_cfn)) == (expected, [])


def test_start_deploy_updates_a_stack_the_same_keyname_deployed(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", account_keyname="prod")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": []}

    # act
    start_deploy(mock_session, "test-stack", "template", REGION, "prod")

    # assert
    assert stack_mutations(mock_cfn) == ["update_stack"]


def test_get_stack_state_reads_the_account_keyname_tag(mock_session: MagicMock, mock_cfn: MagicMock) -> None:
    # arrange
    mock_cfn.describe_stacks.return_value = existing_stack("CREATE_COMPLETE", account_keyname="prod")

    # act
    result = get_stack_state(mock_session, "test-stack", REGION)

    # assert
    assert result is not None
    assert result.account_keyname == "prod"
