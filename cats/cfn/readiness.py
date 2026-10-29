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

"""The deploy readiness gate: a stack is deployed when its EC2 listeners answer, not when they exist.

CloudFormation reports an `AWS::EC2::Instance` CREATE_COMPLETE when the instance is running. The
listener's user data (cats/cfn/templates/userdata.sh.j2) runs after that, under cloud-init; its last
lines start the tcpserver and udpserver units, wait for every configured port to be bound, and write
one verdict line to the serial console: LISTENER_READY_MARKER, or LISTENER_FAILED_MARKER naming the
ports still unbound. Until that line exists, a probe of the listener's port is a genuine REFUSED
from a listener that does not exist yet, a wrong answer a test asserts on.

`poll_pending_stacks` (cats/cfn/deploy.py) holds a create's or update's DeployResult until every
`Ec2InstanceId{target}` output has written its verdict. The deploy reads it with
ec2:GetConsoleOutput and Latest=True, the live serial console of a Nitro instance type. Nothing
travels over the instance's own network, so a listener in a subnet with no route to any endpoint
(a data-tier subnet with an empty route table) is gated exactly like
one with SSM reachability. That is also why the gate does not go over SSM: such a listener never
registers, and it is not meant to; it is a target, and probes from it use the Lambda handler.

Each ListenerReadiness moves booting -> ready | failed, one non-blocking API call per poll cycle,
so many stacks advance together. `failed` carries the tail of the console output, which is where
the cause is written: the unbound ports, cloud-init's own complaint about the user data, or a boot
that never got as far as cloud-init.

The second gate is IAM. CloudFormation reports a role and its policy CREATE_COMPLETE when IAM's
control plane has accepted them; the authorizer that decides each API call converges seconds later,
and a test that starts in that window is denied by a role that CloudFormation says exists. The
deploy therefore proves the test role usable the way `cats test` will use it: `RoleReadiness`
assumes the role from the environment's credential chain, the same identity the tests assume it
from, then calls cloudformation:DescribeStacks on the stack as that role, the first call every test
session makes. It does both on every cycle with a newly assumed session, never a reused one, because
every test session starts with a new one. The STS success proves the trust policy (CreateRole) has
converged, the
DescribeStacks success proves CatsTestRolePolicy (PutRolePolicy) has, and that policy is the one
document carrying every CATS grant on the role, so proving one statement proves them all. An
additional role is probed for being assumable only: its grants are the user's writes, and no call
CATS could choose is known to be allowed by them, safe to make, or denied only by non-convergence.
AccessDenied is "not yet" until the shared bound; an explicit deny, which no amount of waiting
changes, fails at once.

The third gate is Lambda. A probe function's execution role (CatsLambdaRole, trust lambda.amazonaws.com)
is reported CREATE_COMPLETE by CloudFormation before Lambda itself can assume it: an Invoke in that
window fails with AccessDeniedException "...cannot be assumed by Lambda", and the same function is
invocable seconds later. No API reports that convergence, so `LambdaReadiness` does what `cats test`
does -- it invokes the function -- and any return proves the role was assumed (the payload is discarded).
The invoke is made from the deploy identity, not the test role, because the race is caller-independent:
once Lambda can assume the role it can for every caller, so proving it here proves it for the test role's
invoke too, whose grant RoleReadiness already proves converged (it is one statement of the one
CatsTestRolePolicy document). The gate event is a loopback TCP probe, which stays inside the Lambda
sandbox: no VPC or internet egress, no dependency on the EC2 listener, and it returns at once so it never
blocks the poll cycle. "...cannot be assumed by Lambda" is "not yet" until the shared bound; any other
error, including any other AccessDeniedException, propagates as the caller's grant or the service.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from botocore.exceptions import ClientError

from cats.cfn.session import BOTO_RETRY_CONFIG, DEPLOY_ROLE_SESSION_NAME
from cats.constants import LISTENER_FAILED_MARKER, LISTENER_READY_MARKER, PER_TARGET_OUTPUT_KEYS
from cats.user_agent import attributed_session

if TYPE_CHECKING:
    from mypy_boto3_cloudformation.type_defs import OutputTypeDef
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_lambda import LambdaClient

logger = logging.getLogger(__name__)

# How long after CREATE_COMPLETE a listener may take to write its verdict, and a role to converge in
# IAM. Today's listeners do so within about fifteen seconds; a first boot on a slow instance type
# takes longer, a broken boot never gets there, and this is where the two part. IAM converges in
# seconds on an ordinary day and in minutes on a bad one; the same bound covers both.
READINESS_TIMEOUT_SECONDS = 180

# How much of the console output a failure carries. Enough for cloud-init's last lines and the
# unit status the user data prints; the full log stays on the instance.
CONSOLE_TAIL_LINES = 20

# cloud-init's final line, written after the user data has run whatever happened to it. Seen with
# no verdict before it, it means the user data stopped before its last line.
CLOUD_INIT_FINISHED = re.compile(r"Cloud-init v\. \S+ finished at ")

# GetConsoleOutput with Latest=True is refused on an instance type that is not Nitro-based; the
# buffered console it falls back to is posted minutes after boot, too late to gate on.
NO_LIVE_CONSOLE_REASON = (
    "its instance type has no live serial console (ec2:GetConsoleOutput with Latest), so its boot cannot be watched. "
    + "Use a Nitro-based ec2_instance_type; the default t3.micro is one"
)

INSTANCE_ID_OUTPUT_KEY = "Ec2InstanceId"

ReadinessPhase = Literal["booting", "ready", "failed"]


def _now() -> float:
    """time.monotonic looked up at call time, so a test's patched clock reaches the dataclass default too."""
    return time.monotonic()


def console_tail(output: str) -> str:
    """The last CONSOLE_TAIL_LINES of a console output, indented as a block for an error message."""
    lines = output.splitlines()[-CONSOLE_TAIL_LINES:]
    if len(lines) == 0:
        return "  (no console output yet)"
    return "\n".join(f"  {line}" for line in lines)


@dataclass
class ListenerReadiness:
    """One EC2 listener's progress from CREATE_COMPLETE to answering on its ports."""

    instance_id: str
    target_name: str
    phase: ReadinessPhase = "booting"
    error: str | None = None
    started_at: float = field(default_factory=_now)

    @property
    def is_done(self) -> bool:
        return self.phase in ("ready", "failed")

    def _fail(self, reason: str) -> None:
        elapsed = int(time.monotonic() - self.started_at)
        self.phase = "failed"
        self.error = f"EC2 listener {self.instance_id} ({self.target_name}) is not ready after {elapsed}s: {reason}"

    def advance(self, ec2: "EC2Client") -> str | None:
        """Take one non-blocking step. Returns a progress notice when the listener became ready, else None.

        Raises nothing for the instance's own failures: they end in the `failed` phase with
        `error` set. A ClientError other than the one this reads around propagates, because it is
        the caller's credentials or the service, not this listener.
        """
        if self.phase != "booting":
            return None
        try:
            response = ec2.get_console_output(InstanceId=self.instance_id, Latest=True)
        except ClientError as e:
            if e.response["Error"]["Code"] == "UnsupportedOperation":
                self._fail(NO_LIVE_CONSOLE_REASON)
                return None
            raise
        output = response.get("Output", "")  # boto3 decodes the base64 for this call; absent until the console has posted once
        if LISTENER_READY_MARKER in output:
            self.phase = "ready"
            return f"EC2 listener {self.instance_id} ({self.target_name}) ready"
        if LISTENER_FAILED_MARKER in output:
            self._fail(f"its user data found listener ports still unbound. Console:\n{console_tail(output)}")
        elif CLOUD_INIT_FINISHED.search(output) is not None:
            self._fail(f"cloud-init finished without the listeners' ready line, so the user data stopped early. Console:\n{console_tail(output)}")
        elif time.monotonic() - self.started_at >= READINESS_TIMEOUT_SECONDS:
            self._fail(f"it did not report ready within {READINESS_TIMEOUT_SECONDS}s. Console:\n{console_tail(output)}")
        return None


TEST_ROLE_ARN_OUTPUT_KEY = "TestRoleArn"

# IAM's standardized wording for a denial that a policy states, as opposed to one that nothing
# allows. The former does not converge away; the latter is what the gate waits out.
EXPLICIT_DENY_MARKER = "with an explicit deny"

# STS and CloudFormation both report an identity-based denial under this code.
ACCESS_DENIED_CODE = "AccessDenied"

RolePhase = Literal["probing", "ready", "failed"]

ProbedAction = Literal["sts:AssumeRole", "cloudformation:DescribeStacks"]


@dataclass
class RoleReadiness:
    """One IAM role's progress from CREATE_COMPLETE to being usable by the identity that runs the tests.

    Each cycle in `probing` does what a new `cats test` session does: sts:AssumeRole from the
    environment's credential chain, which proves the trust policy has converged, then, unless this
    is an additional role (no `stack_name`, where being assumable is the verdict),
    cloudformation:DescribeStacks on the stack as that fresh session, which proves
    CatsTestRolePolicy has converged too. An already-converged role costs one cycle.

    The session is never reused across cycles. A session assumed within seconds of IAM writing the
    role can stay denied after a newly assumed one is allowed (seen on an integration run: four
    stacks with no EC2 reached the gate ~17 s after creation and stayed denied for the whole bound),
    and `cats test` always starts with a new session, so a new session is what the gate must prove.
    """

    role_arn: str
    region: str
    stack_name: str | None
    phase: RolePhase = "probing"
    error: str | None = None
    started_at: float = field(default_factory=_now)
    _denied_action: ProbedAction = field(default="sts:AssumeRole", init=False, repr=False)
    _last_denial: str = field(default="", init=False, repr=False)

    @property
    def is_done(self) -> bool:
        return self.phase in ("ready", "failed")

    @property
    def role_name(self) -> str:
        return self.role_arn.rsplit("/", 1)[-1]

    def advance(self) -> str | None:
        """Take one non-blocking step. Returns a progress notice when the role became ready, else None.

        AccessDenied from either call is IAM not yet knowing the role or its grant: the role stays
        `probing` until READINESS_TIMEOUT_SECONDS, then fails naming both causes it cannot tell
        apart for the call that was denied last. An explicit deny fails at once. Any other
        ClientError propagates, because it is the caller's credentials or the service, not this role.
        """
        if self.is_done:
            return None
        try:
            credentials = (
                attributed_session().client("sts").assume_role(RoleArn=self.role_arn, RoleSessionName=DEPLOY_ROLE_SESSION_NAME)["Credentials"]
            )
        except ClientError as e:
            self._absorb_denial(e, "sts:AssumeRole")
            return None
        if self.stack_name is None:
            self.phase = "ready"
            return f"Role {self.role_name} assumable"
        cfn = attributed_session(
            region_name=self.region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        ).client("cloudformation", config=BOTO_RETRY_CONFIG)
        try:
            cfn.describe_stacks(StackName=self.stack_name)
        except ClientError as e:
            self._absorb_denial(e, "cloudformation:DescribeStacks")
            return None
        self.phase = "ready"
        return f"Test role {self.role_name} authorized"

    def _absorb_denial(self, e: ClientError, action: ProbedAction) -> None:
        """Keep probing on an AccessDenied, fail on an explicit deny or at the bound; re-raise anything else."""
        if e.response["Error"]["Code"] != ACCESS_DENIED_CODE:
            raise e
        message = e.response["Error"]["Message"]
        if EXPLICIT_DENY_MARKER in message:
            self._fail(
                f"Test role {self.role_arn}: {action} is explicitly denied: {message}. "
                + "A service control policy, permissions boundary or resource policy denies it; waiting will not change that."
            )
            return
        self._denied_action = action
        self._last_denial = message
        if time.monotonic() - self.started_at >= READINESS_TIMEOUT_SECONDS:
            self._fail(self._timeout_reason())

    def _timeout_reason(self) -> str:
        elapsed = int(time.monotonic() - self.started_at)
        if self._denied_action == "sts:AssumeRole":
            return (
                f"Test role {self.role_arn} could not be assumed after {elapsed}s: {self._last_denial}. "
                + "Either its trust policy has not converged in IAM yet, or the identity running 'cats infra deploy' is not allowed "
                + "sts:AssumeRole on CATS test roles (the readiness probe assumes the test role from the same credentials 'cats test' uses)."
            )
        return (
            f"Test role {self.role_arn} was assumed but cloudformation:DescribeStacks on the stack is still denied after {elapsed}s: "
            + f"{self._last_denial}. Either its CatsTestRolePolicy has not converged in IAM yet, or a service control policy or "
            + "permissions boundary denies it."
        )

    def _fail(self, reason: str) -> None:
        self.phase = "failed"
        self.error = reason


def listeners_from_outputs(outputs: list["OutputTypeDef"]) -> list[ListenerReadiness]:
    """One ListenerReadiness per `Ec2InstanceId{target}` output, in output order.

    The output scheme is f"{key}{target}" with no separator (PER_TARGET_OUTPUT_KEYS); no other key
    in the scheme starts with Ec2InstanceId, pinned by tests/unit_tests/test_output_key_scheme.py.
    """
    assert INSTANCE_ID_OUTPUT_KEY in PER_TARGET_OUTPUT_KEYS
    listeners: list[ListenerReadiness] = []
    for output in outputs:
        key = output.get("OutputKey", "")
        if key.startswith(INSTANCE_ID_OUTPUT_KEY) and key != INSTANCE_ID_OUTPUT_KEY:
            listeners.append(ListenerReadiness(instance_id=output["OutputValue"], target_name=key[len(INSTANCE_ID_OUTPUT_KEY) :]))
    return listeners


def test_role_from_outputs(outputs: list["OutputTypeDef"], region: str, stack_name: str) -> RoleReadiness | None:
    """The RoleReadiness for the stack's `TestRoleArn` output, or None when it has none (not a CATS template).

    The output is the ARN of the role CloudFormation created, so the gate probes exactly that role
    rather than a name reconstructed from the configuration.
    """
    for output in outputs:
        if output.get("OutputKey") == TEST_ROLE_ARN_OUTPUT_KEY:
            return RoleReadiness(role_arn=output["OutputValue"], region=region, stack_name=stack_name)
    return None


PROBE_LAMBDA_NAME_OUTPUT_KEY = "ProbeLambdaName"

# Lambda's wording when its control plane cannot yet assume a freshly created execution role: the role's
# trust policy (CreateRole) has not converged in the Region Lambda assumes it from. Reported under the
# code below (distinct from STS and CloudFormation's "AccessDenied"). Caller-independent, so the deploy
# identity's invoke proves it for the test role's invoke too.
LAMBDA_ROLE_NOT_ASSUMABLE_MARKER = "cannot be assumed by Lambda"
LAMBDA_ACCESS_DENIED_CODE = "AccessDeniedException"

# The event the gate invokes each probe with. The network result is discarded: the invoke exists only to
# make Lambda assume the execution role, which any execution proves. A loopback TCP connect stays inside
# the Lambda sandbox (no VPC or internet egress, no dependency on the EC2 listener) and returns at once,
# so a slow or unreachable target can never make the probe block the poll cycle.
GATE_PROBE_EVENT = json.dumps({"PROTOCOL": "tcp", "TARGET_URL": "127.0.0.1", "TARGET_PORT": 1})

LambdaPhase = Literal["probing", "ready", "failed"]


@dataclass
class LambdaReadiness:
    """One probe Lambda's progress from CREATE_COMPLETE to Lambda being able to assume its execution role.

    Each cycle in `probing` invokes the function once (RequestResponse; DryRun would validate only the
    caller's lambda:InvokeFunction, not the execution role). Any return proves the role was assumed, the
    function ran, so the phase goes `ready` and the payload is discarded. AccessDeniedException whose
    message carries LAMBDA_ROLE_NOT_ASSUMABLE_MARKER is the trust policy not yet converged: the function
    stays `probing` until READINESS_TIMEOUT_SECONDS, then fails naming both causes it cannot tell apart.
    Any other ClientError propagates, because it is the caller's grant or the service, not this role.
    """

    function_name: str
    phase: LambdaPhase = "probing"
    error: str | None = None
    started_at: float = field(default_factory=_now)

    @property
    def is_done(self) -> bool:
        return self.phase in ("ready", "failed")

    def advance(self, lambda_client: "LambdaClient") -> str | None:
        """Take one non-blocking step. Returns a progress notice when the Lambda became invocable, else None."""
        if self.is_done:
            return None
        try:
            lambda_client.invoke(FunctionName=self.function_name, InvocationType="RequestResponse", Payload=GATE_PROBE_EVENT)
        except ClientError as e:
            self._absorb_denial(e)
            return None
        self.phase = "ready"
        return f"Probe Lambda {self.function_name} invocable"

    def _absorb_denial(self, e: ClientError) -> None:
        """Keep probing while Lambda cannot yet assume the execution role; fail at the bound; re-raise anything else."""
        error = e.response["Error"]
        if error["Code"] != LAMBDA_ACCESS_DENIED_CODE or LAMBDA_ROLE_NOT_ASSUMABLE_MARKER not in error.get("Message", ""):
            raise e
        if time.monotonic() - self.started_at >= READINESS_TIMEOUT_SECONDS:
            self._fail(error["Message"])

    def _fail(self, message: str) -> None:
        elapsed = int(time.monotonic() - self.started_at)
        self.phase = "failed"
        self.error = (
            f"Probe Lambda {self.function_name}'s execution role could not be assumed by Lambda after {elapsed}s: {message}. "
            + "Either its trust policy has not converged in IAM yet, or a service control policy or permissions boundary "
            + "prevents Lambda from assuming it."
        )


def lambdas_from_outputs(outputs: list["OutputTypeDef"]) -> list["LambdaReadiness"]:
    """One LambdaReadiness per `ProbeLambdaName{target}` output, in output order.

    The output scheme is f"{key}{target}" with no separator (PER_TARGET_OUTPUT_KEYS); no other key in the
    scheme starts with ProbeLambdaName, pinned by tests/unit_tests/test_output_key_scheme.py.
    """
    assert PROBE_LAMBDA_NAME_OUTPUT_KEY in PER_TARGET_OUTPUT_KEYS
    lambdas: list[LambdaReadiness] = []
    for output in outputs:
        key = output.get("OutputKey", "")
        if key.startswith(PROBE_LAMBDA_NAME_OUTPUT_KEY) and key != PROBE_LAMBDA_NAME_OUTPUT_KEY:
            lambdas.append(LambdaReadiness(function_name=output["OutputValue"]))
    return lambdas


ReadinessKind = Literal["listener", "role", "lambda"]


@dataclass(frozen=True)
class ReadinessFailure:
    """Why a stack's gate failed: which kind of resource, and that resource's own account of the cause.

    The kind decides the remediation deploy.py attaches: a listener that never came up is recreated
    (its user data runs on first boot only); a role that was never authorized, or a probe Lambda whose
    execution role Lambda still cannot assume, is fixed where the denial comes from, then deployed again.
    """

    kind: ReadinessKind
    detail: str


@dataclass
class StackReadiness:
    """Every listener, probed role and probe Lambda of one stack; done when each is ready or one has failed."""

    listeners: list[ListenerReadiness]
    roles: list[RoleReadiness] = field(default_factory=list)
    lambdas: list[LambdaReadiness] = field(default_factory=list)

    @property
    def is_done(self) -> bool:
        if self.failure is not None:
            return True
        return (
            all(role.phase == "ready" for role in self.roles)
            and all(probe_lambda.phase == "ready" for probe_lambda in self.lambdas)
            and all(listener.phase == "ready" for listener in self.listeners)
        )

    @property
    def failure(self) -> ReadinessFailure | None:
        """The first failure, roles before lambdas before listeners, or None while none has failed."""
        for role in self.roles:
            if role.phase == "failed":
                assert role.error is not None
                return ReadinessFailure(kind="role", detail=role.error)
        for probe_lambda in self.lambdas:
            if probe_lambda.phase == "failed":
                assert probe_lambda.error is not None
                return ReadinessFailure(kind="lambda", detail=probe_lambda.error)
        for listener in self.listeners:
            if listener.phase == "failed":
                assert listener.error is not None
                return ReadinessFailure(kind="listener", detail=listener.error)
        return None

    def advance(self, ec2: "EC2Client", lambda_client: "LambdaClient | None" = None) -> list[str]:
        """Advance every unfinished role, probe Lambda and listener by one step; returns the notices they produced."""
        notices: list[str] = []
        for role in self.roles:
            if role.is_done:
                continue
            role_notice = role.advance()
            if role_notice is not None:
                logger.info(role_notice)
                notices.append(role_notice)
        for probe_lambda in self.lambdas:
            if probe_lambda.is_done:
                continue
            assert lambda_client is not None  # _begin_readiness builds the client whenever a stack carries probe lambdas
            lambda_notice = probe_lambda.advance(lambda_client)
            if lambda_notice is not None:
                logger.info(lambda_notice)
                notices.append(lambda_notice)
        for listener in self.listeners:
            if listener.is_done:
                continue
            notice = listener.advance(ec2)
            if notice is not None:
                logger.info(notice)
                notices.append(notice)
        return notices
