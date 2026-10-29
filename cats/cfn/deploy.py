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

"""CloudFormation deployment operations."""

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import boto3
import typer
from botocore.exceptions import ClientError

from cats.cfn.events import StackEvent, StackPoller, is_success
from cats.cfn.readiness import RoleReadiness, StackReadiness, lambdas_from_outputs, listeners_from_outputs, test_role_from_outputs
from cats.cfn.retry import retry_on_throttle
from cats.cfn.session import BOTO_RETRY_CONFIG
from cats.version import __version__

if TYPE_CHECKING:
    from mypy_boto3_cloudformation import CloudFormationClient
    from mypy_boto3_cloudformation.type_defs import ParameterTypeDef, StackTypeDef, TagTypeDef
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_lambda import LambdaClient

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


# start_deploy creates or updates; start_destroy deletes. Polling treats a vanished stack as
# success only for a delete.
StackOperation = Literal["create", "update", "delete"]
DELETE_WAITER_DELAY_SECONDS = 5
DELETE_WAITER_MAX_ATTEMPTS = 240  # 20 minutes - Lambda ENI detachment keeps deletes running for a long time

# Tag proving CATS owns a stack. Only stacks carrying it are ever deleted to recover a deploy.
CATS_MANAGED_TAG_KEY = "cats:managed"
CATS_MANAGED_TAG_VALUE = "true"

CATS_VERSION_TAG_KEY = "cats:version"
CATS_ACCOUNT_KEYNAME_TAG_KEY = "cats:account-keyname"

UDP_RECEIPT_TOKEN_PARAMETER_KEY = "CatsUdpReceiptToken"  # noqa: S105


def generate_udp_receipt_token() -> str:
    """Generate a fresh UDP receipt token: 32 hex chars, so it can never contain the ':' delimiter."""
    return secrets.token_hex(16)


# Statuses where CloudFormation refuses update_stack ("stack ... can not be updated") and a
# delete-then-create is the documented recovery. Every entry is in TERMINAL_FAILURE
# (cats/cfn/events.py) - when a status is added there, decide which set it belongs to here.
UNRECOVERABLE_STACK_STATUSES: frozenset[str] = frozenset(
    {
        "ROLLBACK_COMPLETE",  # create failed and rolled back
        "CREATE_FAILED",  # create failed with OnFailure=DO_NOTHING, resources left in place
        "ROLLBACK_FAILED",  # the rollback of a failed create failed
        "DELETE_FAILED",  # destroy failed, commonly security groups held by lingering Lambda ENIs
    }
)

# Statuses update_stack also refuses, but where deleting is the wrong answer: these hold
# resources from an earlier healthy deploy, or need a human decision. Deploy stops and says
# what to run rather than destroying anything. Formatted with stack_name, account_name, region.
MANUAL_RECOVERY_REMEDIATION: dict[str, str] = {
    "UPDATE_ROLLBACK_FAILED": (
        "It holds resources from an earlier successful deploy, so CATS will not delete it. "
        + "Run 'aws cloudformation continue-update-rollback --stack-name {stack_name} --region {region}' "
        + "using credentials for account {account_name}, then deploy again"
    ),
    "REVIEW_IN_PROGRESS": "A change set created it and never executed it. Run 'cats infra destroy {account_name}', then deploy again",
}

RECREATE_REMEDIATION = "Recreate it: cats infra destroy {account_name} && cats infra deploy {account_name}"


def _is_throttled(error: ClientError) -> bool:
    """The errors @retry_on_throttle retries; a handler that swallows one into a DeployResult hides it from the decorator."""
    message = str(error)
    return "Throttling" in message or "Rate exceeded" in message


VERSION_MISMATCH_DEPLOY_ERROR = (
    "Stack {stack_name} was deployed by CATS {deployed_version} but CATS {installed_version} is installed. "
    + "Updating in place would not replace the deployed EC2 listeners or SSM documents. "
    + RECREATE_REMEDIATION
)
VERSION_UNKNOWN_DEPLOY_ERROR = (
    "Stack {stack_name} has no {version_tag_key} tag, so CATS cannot confirm it matches installed CATS {installed_version}. "
    + "It was not created by 'cats infra deploy'. "
    + RECREATE_REMEDIATION
)


@dataclass
class DeployResult:
    """Result of a deployment operation."""

    account_name: str
    success: bool
    stack_id: str | None = None
    error: str | None = None
    already_absent: bool = False


@dataclass
class PendingStack:
    """A stack operation that has been started but not yet completed.

    `ec2` is the deploy identity's EC2 client in the stack's region, used to read each listener's
    boot verdict from its serial console after a create or update completes (cats/cfn/readiness.py);
    `lambda_client` is the same identity's Lambda client, used to prove each probe function's execution
    role is assumable by Lambda before the deploy reports success; `region` and `additional_role_arns`
    let the same gate probe the stack's test role and the additional roles it created. A delete carries
    none of them.
    """

    account_name: str
    stack_name: str
    poller: StackPoller
    operation: StackOperation
    ec2: "EC2Client | None" = None
    lambda_client: "LambdaClient | None" = None
    region: str | None = None
    additional_role_arns: list[str] = field(default_factory=list)


def get_stack_name(prefix: str, account_name: str) -> str:
    """CFN stack names forbid `_`; keynames forbid `-`, so the mapping is collision-free."""
    return f"{prefix}{account_name.replace('_', '-')}"


@dataclass
class StackState:
    """The parts of an existing stack's description that deploy decisions depend on."""

    status: str
    cats_managed: bool
    has_udp_receipt_token: bool
    version: str | None
    account_keyname: str | None  # the cats:account-keyname tag: which config keyname deployed the stack


def read_deployed_version(stack: "StackTypeDef") -> str | None:
    """Return the CATS release recorded in the stack's tags, or None if it was not deployed by CATS."""
    for tag in stack.get("Tags", []):
        if tag.get("Key") == CATS_VERSION_TAG_KEY:
            return tag.get("Value")
    return None


def read_deployed_account_keyname(stack: "StackTypeDef") -> str | None:
    """Return the config keyname recorded in the stack's tags, or None if it was not deployed by CATS."""
    for tag in stack.get("Tags", []):
        if tag.get("Key") == CATS_ACCOUNT_KEYNAME_TAG_KEY:
            return tag.get("Value")
    return None


def version_deploy_error(stack_name: str, account_name: str, deployed_version: str | None) -> str:
    """Error for a deploy that would update a stack from another release in place."""
    if deployed_version is None:
        return VERSION_UNKNOWN_DEPLOY_ERROR.format(
            stack_name=stack_name,
            version_tag_key=CATS_VERSION_TAG_KEY,
            installed_version=__version__,
            account_name=account_name,
        )
    return VERSION_MISMATCH_DEPLOY_ERROR.format(
        stack_name=stack_name,
        deployed_version=deployed_version,
        installed_version=__version__,
        account_name=account_name,
    )


def describe_stack_state(cfn: "CloudFormationClient", stack_name: str) -> StackState | None:
    """Read a stack's status, ownership, version, and token parameter presence. None means the stack does not exist."""
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    except ClientError as e:
        if "does not exist" in str(e):
            return None
        raise

    cats_managed = any(tag.get("Key") == CATS_MANAGED_TAG_KEY and tag.get("Value") == CATS_MANAGED_TAG_VALUE for tag in stack.get("Tags", []))
    has_udp_receipt_token = any(p.get("ParameterKey") == UDP_RECEIPT_TOKEN_PARAMETER_KEY for p in stack.get("Parameters", []))
    return StackState(
        status=str(stack["StackStatus"]),
        cats_managed=cats_managed,
        has_udp_receipt_token=has_udp_receipt_token,
        version=read_deployed_version(stack),
        account_keyname=read_deployed_account_keyname(stack),
    )


def wait_for_stack_deleted(cfn: "CloudFormationClient", stack_name: str) -> None:
    """Block until the stack is fully deleted. Raises WaiterError if it never gets there."""
    waiter = cfn.get_waiter("stack_delete_complete")
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={"Delay": DELETE_WAITER_DELAY_SECONDS, "MaxAttempts": DELETE_WAITER_MAX_ATTEMPTS},
    )


def delete_dead_stack(
    session: boto3.Session,
    cfn: "CloudFormationClient",
    stack_name: str,
    region: str,
) -> None:
    """Delete a stack that CloudFormation will not update, blocking until it is gone.

    Empties the response bucket first: a bucket with objects in it blocks the deletion.

    This blocks the calling thread for as long as CloudFormation takes. Under `deploy --all`
    that occupies one of the 10 pool workers, so accounts still queued behind it start later -
    accepted for a rare recovery path, since the alternative is an account nobody can deploy.

    Raises ClientError or WaiterError if the deletion cannot be completed.
    """
    empty_cats_bucket(session, stack_name, region)
    cfn.delete_stack(StackName=stack_name)
    wait_for_stack_deleted(cfn, stack_name)


@retry_on_throttle
def start_deploy(
    session: boto3.Session,
    stack_name: str,
    template_body: str,
    region: str,
    account_name: str,
    disable_rollback: bool = False,
    on_notice: Callable[[str], None] | None = None,
    additional_role_arns: list[str] | None = None,
) -> PendingStack | DeployResult:
    """Start stack create/update, return immediately.

    A stack left behind by a failed create or destroy cannot be updated, so a CATS-managed one
    is deleted and recreated - otherwise a single failure breaks every deploy after it. Stacks
    needing a human decision return a failed DeployResult naming the command to run.

    Args:
        on_notice: Called with a human-readable message when slow recovery work starts, so the
            CLI can show it - the Live progress display suppresses log records below WARNING.
        additional_role_arns: The ARNs of the additional roles the template creates. The readiness
            gate proves each assumable before the deploy reports success (cats/cfn/readiness.py).

    Returns DeployResult for immediate failures.
    Returns PendingStack for operations that need polling, including a stack CloudFormation has no
    changes for: its readiness gate still runs, so a re-run of 'cats infra deploy' also means ready.
    """
    cfn: CloudFormationClient = session.client("cloudformation", region_name=region, config=BOTO_RETRY_CONFIG)
    tags: list[TagTypeDef] = [
        {"Key": CATS_MANAGED_TAG_KEY, "Value": CATS_MANAGED_TAG_VALUE},
        {"Key": CATS_ACCOUNT_KEYNAME_TAG_KEY, "Value": account_name},
        {"Key": CATS_VERSION_TAG_KEY, "Value": __version__},
    ]

    def notify(message: str) -> None:
        """Announce slow recovery work at WARNING, which the Live display lets through."""
        logger.warning(message)
        if on_notice is not None:
            on_notice(message)

    try:
        state = describe_stack_state(cfn, stack_name)
    except ClientError as e:
        if _is_throttled(e):
            raise  # @retry_on_throttle retries the whole call
        return DeployResult(account_name=account_name, success=False, error=str(e))

    stack_status: str | None = None if state is None else state.status  # None means the stack is absent

    if state is not None and state.account_keyname is not None and state.account_keyname != account_name:
        # Two configs whose prefix and keyname concatenate to one stack name ("CATS-" + "aprod" and
        # "CATS-a" + "prod") would otherwise update each other's stack in place.
        return DeployResult(
            account_name=account_name,
            success=False,
            error=(
                f"Stack {stack_name} was deployed for account keyname '{state.account_keyname}', not '{account_name}': "
                + "the two names concatenate with cfn_stack_prefix to the same stack name. "
                + "Deploy with a different cfn_stack_prefix, or rename one of the keynames."
            ),
        )

    if stack_status in MANUAL_RECOVERY_REMEDIATION:
        remediation = MANUAL_RECOVERY_REMEDIATION[stack_status].format(stack_name=stack_name, account_name=account_name, region=region)
        return DeployResult(
            account_name=account_name,
            success=False,
            error=f"Stack {stack_name} is {stack_status} and cannot be deployed. {remediation}.",
        )

    if stack_status is not None and stack_status.endswith("_IN_PROGRESS") and stack_status != "DELETE_IN_PROGRESS":
        # A --all re-run or a CI retry meets a stack still moving. update_stack would be refused
        # with CloudFormation's own "can not be updated"; say what is happening and what to do.
        return DeployResult(
            account_name=account_name,
            success=False,
            error=(
                f"Stack {stack_name} is {stack_status}: another operation is still running on it. Wait for it to finish "
                + f"('cats infra status {account_name}' shows the state), then deploy again."
            ),
        )

    if state is not None and stack_status in UNRECOVERABLE_STACK_STATUSES and state.cats_managed is False:
        return DeployResult(
            account_name=account_name,
            success=False,
            error=(
                f"Stack {stack_name} is {stack_status} and cannot be updated, but it is not tagged "
                + f"{CATS_MANAGED_TAG_KEY}={CATS_MANAGED_TAG_VALUE} so CATS will not delete it. "
                + "Delete it yourself, or deploy with a different cfn_stack_prefix."
            ),
        )

    try:
        if stack_status in UNRECOVERABLE_STACK_STATUSES:
            notify(f"Deleting unrecoverable stack {stack_name} ({stack_status}) before recreating")
            delete_dead_stack(session, cfn, stack_name, region)
            stack_status = None

        elif stack_status == "DELETE_IN_PROGRESS":
            # A deletion is already running - often one whose waiter timed out. update_stack is
            # refused while it runs, so wait it out and create fresh.
            notify(f"Waiting for the in-progress deletion of stack {stack_name} before recreating")
            wait_for_stack_deleted(cfn, stack_name)
            stack_status = None

        operation: StackOperation
        if stack_status is not None:
            assert state is not None  # stack_status comes from state
            if state.version != __version__:
                return DeployResult(
                    account_name=account_name,
                    success=False,
                    error=version_deploy_error(stack_name, account_name, state.version),
                )
            operation = "update"
            update_parameters: list[ParameterTypeDef]
            if state.has_udp_receipt_token is True:
                update_parameters = [{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "UsePreviousValue": True}]
            else:
                update_parameters = [{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "ParameterValue": generate_udp_receipt_token()}]
            poller = StackPoller.seeded(cfn, stack_name)
            cfn.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=update_parameters,
                Capabilities=["CAPABILITY_NAMED_IAM"],
                Tags=tags,
            )
        else:
            operation = "create"
            on_failure: Literal["DO_NOTHING", "ROLLBACK"] = "DO_NOTHING" if disable_rollback else "ROLLBACK"
            cfn.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=[{"ParameterKey": UDP_RECEIPT_TOKEN_PARAMETER_KEY, "ParameterValue": generate_udp_receipt_token()}],
                Capabilities=["CAPABILITY_NAMED_IAM"],
                OnFailure=on_failure,
                Tags=tags,
            )
            poller = StackPoller(cfn, stack_name)

        return PendingStack(
            account_name=account_name,
            stack_name=stack_name,
            poller=poller,
            operation=operation,
            ec2=session.client("ec2", region_name=region, config=BOTO_RETRY_CONFIG),
            lambda_client=session.client("lambda", region_name=region, config=BOTO_RETRY_CONFIG),
            region=region,
            additional_role_arns=[] if additional_role_arns is None else list(additional_role_arns),
        )

    except ClientError as e:
        if _is_throttled(e):
            raise  # Let decorator handle retry
        error_msg = str(e)
        if "No updates are to be performed" in error_msg:
            # CloudFormation has nothing to do, but the gate still has: a stack deployed a moment ago
            # may have a test role IAM has not converged on yet, or a listener still booting, and a
            # re-run is exactly when that is asked about. A poller already at the stack's status
            # makes poll_pending_stacks run the gate on its first cycle without reading any events.
            assert state is not None  # update_stack was only called for an existing stack
            poller = StackPoller(cfn, stack_name)
            poller.final_status = state.status
            return PendingStack(
                account_name=account_name,
                stack_name=stack_name,
                poller=poller,
                operation="update",
                ec2=session.client("ec2", region_name=region, config=BOTO_RETRY_CONFIG),
                lambda_client=session.client("lambda", region_name=region, config=BOTO_RETRY_CONFIG),
                region=region,
                additional_role_arns=[] if additional_role_arns is None else list(additional_role_arns),
            )
        return DeployResult(account_name=account_name, success=False, error=error_msg)
    except typer.Exit:
        # Ctrl+C during a blocking wait arrives as typer.Exit, which subclasses RuntimeError -
        # the handler below would otherwise turn an interrupt into a failed deployment.
        raise
    except Exception as e:
        return DeployResult(account_name=account_name, success=False, error=str(e))


@retry_on_throttle
def start_destroy(
    session: boto3.Session,
    stack_name: str,
    region: str,
    account_name: str,
) -> PendingStack | DeployResult:
    """Start stack deletion, return immediately.

    Returns DeployResult if stack doesn't exist.
    Returns PendingStack for operations that need polling.
    """
    cfn: CloudFormationClient = session.client("cloudformation", region_name=region, config=BOTO_RETRY_CONFIG)

    try:
        response = cfn.describe_stacks(StackName=stack_name)
        stack_id = response["Stacks"][0]["StackId"]
    except ClientError as e:
        if "does not exist" in str(e):
            return DeployResult(account_name=account_name, success=True, already_absent=True)
        return DeployResult(account_name=account_name, success=False, error=str(e))

    try:
        empty_cats_bucket(session, stack_name, region)

        poller = StackPoller.seeded(cfn, stack_id)
        cfn.delete_stack(StackName=stack_name)

        return PendingStack(
            account_name=account_name,
            stack_name=stack_id,  # Use stack ID for polling deleted stacks
            poller=poller,
            operation="delete",
        )
    except ClientError as e:
        error_msg = str(e)
        if "Throttling" in error_msg or "Rate exceeded" in error_msg:
            raise  # Let decorator handle retry
        return DeployResult(account_name=account_name, success=False, error=error_msg)
    except Exception as e:
        return DeployResult(account_name=account_name, success=False, error=str(e))


DEFAULT_TIMEOUT_SECONDS = 3600.0  # 1 hour

LISTENER_NOT_READY_ERROR = "Stack {stack_name} reached {status} but its listeners are not ready. {detail} " + RECREATE_REMEDIATION
ROLE_NOT_READY_ERROR = (
    "Stack {stack_name} reached {status} but its test role is not usable yet. {detail} "
    + "Fix the cause, then deploy again: cats infra deploy {account_name}"
)
LAMBDA_NOT_READY_ERROR = (
    "Stack {stack_name} reached {status} but a probe Lambda is not usable yet. {detail} "
    + "Fix the cause, then deploy again: cats infra deploy {account_name}"
)
READINESS_TIMEOUT_ERROR = "Timeout after {timeout}s - the stack is complete but its EC2 listeners or test role were still not ready"


def _begin_readiness(stack: PendingStack) -> StackReadiness | None:
    """The readiness gate for a stack whose create or update just succeeded; None when there is nothing to wait for.

    Reads the stack outputs once, for the `TestRoleArn` the role gate probes, the `Ec2InstanceId{target}`
    values the listener gate watches, and the `ProbeLambdaName{target}` values the Lambda gate invokes. A
    delete carries no EC2 client and is complete when CloudFormation says so; a template with none of
    those outputs (not a CATS stack) is deployed as soon as CloudFormation says so.
    """
    if stack.ec2 is None:
        return None
    assert stack.region is not None  # set together with ec2 by start_deploy
    outputs = stack.poller.cfn_client.describe_stacks(StackName=stack.stack_name)["Stacks"][0].get("Outputs", [])
    roles: list[RoleReadiness] = []
    test_role = test_role_from_outputs(outputs, stack.region, stack.stack_name)
    if test_role is not None:
        roles.append(test_role)
    for role_arn in stack.additional_role_arns:
        roles.append(RoleReadiness(role_arn=role_arn, region=stack.region, stack_name=None))
    listeners = listeners_from_outputs(outputs)
    probe_lambdas = lambdas_from_outputs(outputs)
    if len(listeners) == 0 and len(roles) == 0 and len(probe_lambdas) == 0:
        return None
    return StackReadiness(listeners, roles, probe_lambdas)


def poll_pending_stacks(
    pending: dict[str, PendingStack],
    on_event: Callable[[str, StackEvent], None] | None = None,
    on_notice: Callable[[str, str], None] | None = None,
    on_result: Callable[[DeployResult], None] | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, DeployResult]:
    """Poll all pending stacks until all complete or timeout.

    A create or update is complete when CloudFormation reports success AND every EC2 listener the
    stack created has written its ready verdict to its serial console, meaning its ports are bound
    (cats/cfn/readiness.py). A delete is complete when CloudFormation says so.

    Args:
        pending: account_name -> PendingStack mapping
        on_event: Callback (account_name, event) for each new event
        on_notice: Callback (account_name, message) for each listener that becomes ready, which
            produces no stack event
        on_result: Callback (result) the moment a stack's DeployResult is decided, success or failure.
            Every result in the returned mapping is reported exactly once, in the poll cycle it is
            written, so a `--all` deploy can close each account as its own gate finishes rather than
            when the slowest listener in the batch is ready.
        poll_interval: Seconds between poll cycles
        timeout: Maximum seconds to wait (default 1 hour)

    Returns account_name -> DeployResult mapping.
    """
    results: dict[str, DeployResult] = {}
    active = dict(pending)  # Copy to mutate
    readying: dict[str, StackReadiness] = {}  # stacks CloudFormation has finished, listeners still coming up
    start_time = time.monotonic()

    def record(result: DeployResult) -> None:
        """The one place a result is written, so none is decided without being reported."""
        results[result.account_name] = result
        if on_result is not None:
            on_result(result)

    while active or readying:
        if time.monotonic() - start_time > timeout:
            for account_name in list(active.keys()):
                record(
                    DeployResult(
                        account_name=account_name,
                        success=False,
                        error=f"Timeout after {int(timeout)}s - stack may still be in progress",
                    )
                )
            for account_name in list(readying.keys()):
                record(DeployResult(account_name=account_name, success=False, error=READINESS_TIMEOUT_ERROR.format(timeout=int(timeout))))
            break

        for account_name, stack in list(active.items()):
            try:
                for event in stack.poller.fetch_new_events():
                    if on_event is not None:
                        on_event(account_name, event)

                if stack.poller.is_complete:
                    status = stack.poller.final_status
                    assert status is not None
                    readiness = _begin_readiness(stack) if is_success(status) else None
                    del active[account_name]
                    if readiness is not None:
                        readying[account_name] = readiness
                        continue
                    record(
                        DeployResult(
                            account_name=account_name,
                            success=is_success(status),
                            error=None if is_success(status) else status,
                        )
                    )

            except ClientError as e:
                if "does not exist" in str(e) and stack.operation == "delete":
                    # Stack deleted successfully
                    record(DeployResult(account_name=account_name, success=True))
                    del active[account_name]
                elif "does not exist" in str(e):
                    record(
                        DeployResult(
                            account_name=account_name,
                            success=False,
                            error=f"Stack {stack.stack_name} disappeared during {stack.operation}: {e}",
                        )
                    )
                    del active[account_name]
                else:
                    record(DeployResult(account_name=account_name, success=False, error=str(e)))
                    del active[account_name]

        for account_name, readiness in list(readying.items()):
            stack = pending[account_name]
            assert stack.ec2 is not None  # _begin_readiness only gates stacks that carry a client
            try:
                for notice in readiness.advance(stack.ec2, stack.lambda_client):
                    if on_notice is not None:
                        on_notice(account_name, notice)
            except ClientError as e:
                record(DeployResult(account_name=account_name, success=False, error=str(e)))
                del readying[account_name]
                continue
            if readiness.is_done:
                del readying[account_name]
                failure = readiness.failure
                if failure is None:
                    record(DeployResult(account_name=account_name, success=True))
                else:
                    status = stack.poller.final_status
                    assert status is not None
                    templates = {"role": ROLE_NOT_READY_ERROR, "lambda": LAMBDA_NOT_READY_ERROR, "listener": LISTENER_NOT_READY_ERROR}
                    record(
                        DeployResult(
                            account_name=account_name,
                            success=False,
                            error=templates[failure.kind].format(
                                stack_name=stack.stack_name, status=status, detail=failure.detail, account_name=account_name
                            ),
                        )
                    )

        if active or readying:
            time.sleep(poll_interval)

    return results


def empty_cats_bucket(session: boto3.Session, stack_name: str, region: str) -> None:
    """Empty the CATS response bucket before stack deletion."""
    cfn = session.client("cloudformation", region_name=region)
    s3 = session.client("s3", region_name=region)

    try:
        # Get bucket name from stack resources
        resources = cfn.list_stack_resources(StackName=stack_name)
        bucket_resource = next(
            (r for r in resources["StackResourceSummaries"] if r["LogicalResourceId"] == "CatsResponseBucket"),
            None,
        )
        if bucket_resource is None:
            return

        # Absent on resources that never finished creating - exactly the stacks recovery targets
        bucket_name = bucket_resource.get("PhysicalResourceId")
        if bucket_name is None or bucket_name == "":
            return

        # The session is assumed into the account that owns this stack, so its own identity is the
        # bucket owner; asserting it on every S3 call rules out acting on a bucket this account does
        # not own (defense-in-depth for CATS's own autogenerated bucket name).
        owner = session.client("sts", region_name=region).get_caller_identity()["Account"]

        logger.info(f"Emptying bucket {bucket_name}")

        # Delete all objects
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name, ExpectedBucketOwner=owner):
            objects = page.get("Contents", [])
            if objects:
                s3.delete_objects(
                    Bucket=bucket_name,
                    ExpectedBucketOwner=owner,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchBucket", "ValidationError"):
            # The bucket or the stack is already gone: nothing to empty.
            logger.debug(f"Could not empty bucket: {e}")
            return
        # Anything else (AccessDenied including a wrong-owner assertion, a bucket policy, throttling)
        # leaves objects behind and the stack's delete will then stop at DELETE_FAILED on the bucket;
        # say so now, not at the end.
        logger.warning(f"Could not empty the response bucket for {stack_name}; expect the stack to land in DELETE_FAILED: {e}")


def get_stack_state(
    session: boto3.Session,
    stack_name: str,
    region: str,
) -> StackState | None:
    """Describe a CloudFormation stack's status and CATS metadata, or None if it doesn't exist."""
    cfn = session.client("cloudformation", region_name=region)
    return describe_stack_state(cfn, stack_name)


def get_stack_status(
    session: boto3.Session,
    stack_name: str,
    region: str,
) -> str | None:
    """Get the status of a CloudFormation stack, or None if it doesn't exist."""
    state = get_stack_state(session, stack_name, region)
    return None if state is None else state.status
