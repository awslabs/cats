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

import json
import logging
import re
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, TypedDict

from botocore.exceptions import ClientError
from pydantic import BaseModel, ValidationError

from cats.cfn.deploy import RECREATE_REMEDIATION
from cats.config import CatsNetworkUnderTest
from cats.constants import CatsNetworkResponse, CatsNetworkResult, DeployedLambdaResources, DeployedTargetResources, ShellResult
from cats.exceptions import CatsConfigError, CatsInfrastructureError, CatsLambdaInvocationError, CatsSSMError, CatsUsageError

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient
    from mypy_boto3_lambda.type_defs import InvocationResponseTypeDef
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_ssm.type_defs import SendCommandResultTypeDef

    from cats import AccountUnderTest
else:
    AccountUnderTest = object()

logger = logging.getLogger(__name__)

# AWS SSM silently truncates command output at these limits with no explicit signal.
# These are the documented values as of 2025; treat as historically drifting — callers
# that need deterministic full output should offload to S3.
SSM_STDOUT_LIMIT = 24000
SSM_STDERR_LIMIT = 8000

# SendCommand reports every instance-side refusal as InvalidInstanceId with one fixed message: the
# SSM agent has not registered yet (CloudFormation reports CREATE_COMPLETE at launch, before it
# does), the instance is stopped or terminated, the agent has lost its connection, or the caller's
# grant does not cover the instance. Only the first is transient. The managed-node inventory
# separates a listed instance (refused or disconnected: fail now) from an unlisted one, which may
# still be registering, so the first call against a fresh listener waits for it instead of
# misreporting a connectivity failure. A registered instance never returns the code, so a healthy
# call pays nothing.
SSM_REGISTRATION_TIMEOUT_SECONDS = 90
SSM_REGISTRATION_POLL_SECONDS = 3

# Instances that stayed unlisted for a whole window this process. The window is paid once per
# instance, not once per call: a later call against one of these makes one SendCommand and, on
# refusal, one inventory read (so an instance that has since been listed still gets the message
# for its status), then raises without sleeping.
_ssm_unregistered_instances: set[str] = set()

# Lambda cannot assume a freshly created execution role until its trust policy converges in the Region,
# seconds after CloudFormation reports the stack complete; the invoke then fails with
# AccessDeniedException "...cannot be assumed by Lambda". The deploy readiness gate waits this out
# (cats/cfn/readiness.py), but a pack that invokes a probe immediately after deploy, or one that bypassed
# the gate, can still race it, so _lambda_probe retries this exact error for a bounded window (the same
# shape as the Security Hub import's AccessDeniedException retry). Any other AccessDeniedException (the
# test role lacking lambda:InvokeFunction) is not retried.
LAMBDA_ROLE_PROPAGATION_MARKER = "cannot be assumed by Lambda"
LAMBDA_ROLE_PROPAGATION_RETRY_SECONDS = 30
LAMBDA_ROLE_PROPAGATION_RETRY_INTERVAL_SECONDS = 3


def _ssm_ping_status(ssm: "SSMClient", instance_id: str) -> str | None:
    """PingStatus of the instance in SSM's managed-node inventory; None when it is not listed.

    Stopped and terminated instances are not listed, and an ID the service does not manage may
    come back as InvalidInstanceId rather than an empty list. A test role deployed before the
    inventory read was granted cannot look; that also reads as not listed, so the caller falls
    back to waiting out the registration window.
    """
    try:
        nodes = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])["InstanceInformationList"]
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code not in ("AccessDeniedException", "InvalidInstanceId"):
            raise
        logger.debug("Cannot read SSM inventory for %s (%s); treating it as unlisted", instance_id, error_code)
        return None
    if len(nodes) == 0:
        return None
    return nodes[0].get("PingStatus")


# Must stay identical to RECEIPT_KEY_REGEX in cats/cfn/ec2_server/udpserver.py (the
# stdlib-only listener cannot import it); test_correlation_id_parity.py guards drift.
CORRELATION_ID_REGEX = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

Protocol = Literal["http", "https", "tcp", "udp"]


def _validate_correlation_id(correlation_id: str) -> None:
    if CORRELATION_ID_REGEX.fullmatch(correlation_id) is None:
        raise CatsUsageError(f"Correlation ID {correlation_id!r} must match {CORRELATION_ID_REGEX.pattern}")


def _validate_port(port: int) -> None:
    """The deployed probe trusts its caller; a caller's mistake stops here, before any transport."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise CatsUsageError(f"port must be an integer in 1..65535, got {port!r}")


def _is_ipv6_literal(candidate: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6, candidate)
    except OSError:
        return False
    return True


# Letters, digits, '.', '-' and '_': what hostnames and IPv4 literals are made of.
_HOSTNAME_CHARACTERS = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_host(host: str) -> str:
    """The hostname or IP literal the probe will resolve; a caller's mistake stops here, before any transport.

    Accepts hostname characters, or an IPv6 literal bare or in the brackets a URL puts around
    it, and returns the bare form getaddrinfo takes. Anything else is a URL or a host:port
    pasted where a host belongs, rejected here rather than resolved and misreported as
    DNS_FAILURE.
    """
    if type(host) is not str:
        raise CatsUsageError(f"host must be a hostname or IP literal, got {host!r}")
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if _is_ipv6_literal(bare):
        return bare
    if _HOSTNAME_CHARACTERS.fullmatch(host) is None:
        raise CatsUsageError(
            f"host must be a hostname or IP literal, not a URL or host:port, got {host!r} "
            + "(the method you call chooses the protocol; pass the port as the second argument)"
        )
    return host


# ec2_dns_name -> UDP receipt token, populated by AccountUnderTest.__init__ from stack outputs.
_udp_receipt_tokens: dict[str, str] = {}


def register_udp_receipt_token(dns_name: str, token: str) -> None:
    """Record a CATS listener's UDP receipt token against its DNS name."""
    _udp_receipt_tokens[dns_name] = token


def lookup_udp_receipt_token(dns_name: str) -> str | None:
    """The UDP receipt token for a CATS listener DNS name, or None for unknown targets."""
    return _udp_receipt_tokens.get(dns_name)


def _udp_wire_message(host: str, correlation_id: str) -> str:
    """The datagram for a CATS listener: its account's receipt token, then the correlation id.

    The token is what the listener checks before it records a receipt, and it is known once
    the listener's account has been constructed. A host with no known token gets the bare
    correlation id, which is right for a target that is not a CATS listener and is logged at
    WARNING because a CATS listener would drop it and receive_s3 would then read False.
    """
    token = lookup_udp_receipt_token(host)
    if token is None:
        logger.warning(
            "No receipt token is known for %s, so the datagram is sent bare. If this is a CATS listener, name its account "
            + "first (in pytest, request that account's fixture; in cats run or MCP, pass the receiving account); "
            + "otherwise ignore this.",
            host,
        )
        return correlation_id
    return f"{token}:{correlation_id}"


NetworkCall = Callable[[str, int], CatsNetworkResult]
ProbeTransport = Callable[[Protocol, str, int, str | None], CatsNetworkResult]


class DispatchDict(TypedDict):
    http: NetworkCall
    https: NetworkCall
    tcp: NetworkCall
    udp: NetworkCall


class ProbeOutput(BaseModel):
    """The probe's wire contract: identical as SSM stdout and as the Lambda payload.

    Emitted by cats/cfn/ec2_server/probe.py. ERROR means the probe itself broke (a
    caller or infrastructure bug), never a network outcome; the client raises on it.
    """

    result: Literal["SUCCESS", "REFUSED", "TIMEOUT", "DNS_FAILURE", "TLS_FAILURE", "FAILURE", "SENT", "ERROR"]
    reason: str | None
    http_status: int | None


def _parse_probe_output(raw: str | bytes, source: str, error_type: type[CatsSSMError] | type[CatsLambdaInvocationError]) -> CatsNetworkResult:
    """Turn the probe's JSON document into a CatsNetworkResult, or raise error_type.

    Raises when the JSON is unparseable, when `result` is absent (a Lambda runtime
    `errorMessage` payload has no `result` key), or when `result` is ERROR.
    """
    try:
        output = ProbeOutput.model_validate_json(raw)
    except ValidationError as e:
        raise error_type(f"Invalid probe output from {source}: {raw!r}. Parse error: {e}") from e
    if output.result == "ERROR":
        raise error_type(f"Infrastructure error in {source}: {output.reason}")
    return CatsNetworkResult(response=CatsNetworkResponse[output.result], reason=output.reason, http_status=output.http_status)


def _invoke_probe_lambda(client: "LambdaClient", function_name: str, payload: str) -> "InvocationResponseTypeDef":
    """Invoke the probe function, retrying only the execution-role propagation race for a bounded window.

    AccessDeniedException "...cannot be assumed by Lambda" is Lambda not yet able to assume a freshly
    created execution role; it converges in seconds, so the invoke is retried within
    LAMBDA_ROLE_PROPAGATION_RETRY_SECONDS. Every other error, including any other AccessDeniedException
    (the test role lacking lambda:InvokeFunction), raises at once.
    """
    started = time.time()
    while True:
        try:
            return client.invoke(FunctionName=function_name, InvocationType="RequestResponse", Payload=payload)
        except ClientError as e:
            error = e.response["Error"]
            if error["Code"] != "AccessDeniedException" or LAMBDA_ROLE_PROPAGATION_MARKER not in error.get("Message", ""):
                raise
            if time.time() - started >= LAMBDA_ROLE_PROPAGATION_RETRY_SECONDS:
                raise
            time.sleep(LAMBDA_ROLE_PROPAGATION_RETRY_INTERVAL_SECONDS)


class NetworkHandler:
    """
    Allows AccountUnderTest to target network segments in cats.yaml by using a class
    property, by implementing a custom attribute lookup.

    For example: result = AccountUnderTest.subnet_a.http_call("google.com", 443)

    Every network call returns a CatsNetworkResult. Compare it to a CatsNetworkResponse
    member: SUCCESS, REFUSED, TIMEOUT, DNS_FAILURE, TLS_FAILURE, FAILURE, or SENT (UDP).
    The result's reason, http_status, and correlation_id are diagnostics carried
    alongside the category.

    A NetworkHandler is constructed by AccountUnderTest when a network target attribute is
    read; ``__init__`` is internal and not part of the compatibility contract. The public
    surface is ``name``, ``tags``, ``ec2_dns_name``, ``response_bucket_name`` and the call
    methods.

    What every ``*_call`` can raise, on top of what its own docstring lists:

    - ``CatsUsageError``: ``host`` is a URL or not a hostname/IP literal, or ``port`` is out of range.
      Checked before any AWS call.
    - ``CatsConfigError``: the handler the method names (``ec2_*``, ``lambda_*``) is not deployed for
      this target. Checked before any AWS call.
    - ``CatsSSMError`` (EC2 transport) / ``CatsLambdaInvocationError`` (Lambda transport): the
      transport failed or the probe itself errored, so no result category can be honestly returned.
      The default ``http_call``/``https_call``/``tcp_call``/``udp_call`` raise whichever their
      dispatched transport raises.
    - ``botocore.exceptions.ClientError`` is passed through unwrapped when an AWS API refuses the
      underlying call for a reason CATS does not model (an ``AccessDenied`` on ``lambda:Invoke`` or
      ``ssm:SendCommand``, for example). The one exception is the Lambda execution-role propagation
      race: a ``lambda:Invoke`` ``AccessDeniedException`` whose message is "...cannot be assumed
      by Lambda" is retried for a bounded window before it is allowed to propagate.
    """

    def __init__(
        self,
        host_account: AccountUnderTest,
        deployed: DeployedTargetResources,
        target_config: CatsNetworkUnderTest,
    ) -> None:
        self._host_account = host_account
        self._deployed = deployed
        self.name = deployed.target_name
        self.tags = target_config.tags
        self._network_settings = target_config.network_settings
        self._dispatch = self._build_dispatch()

    def _build_dispatch(self) -> DispatchDict:
        handlers = self._deployed.enabled_handlers
        if not handlers:
            raise CatsConfigError(f"No network handlers deployed for target '{self.name}'. Redeploy with enabled_network_handlers configured.")
        first_handler = handlers[0]
        dispatch_map: dict[str, DispatchDict] = {
            "ec2": {
                "http": self.ec2_http_call,
                "https": self.ec2_https_call,
                "tcp": self.ec2_tcp_call,
                "udp": self.ec2_udp_call,
            },
            "lambda": {
                "http": self.lambda_http_call,
                "https": self.lambda_https_call,
                "tcp": self.lambda_tcp_call,
                "udp": self.lambda_udp_call,
            },
        }
        return dispatch_map[first_handler]

    @property
    def _lambda(self) -> DeployedLambdaResources:
        """Get Lambda metadata, raising if not available."""
        if self._deployed.lambda_ is None:
            raise CatsConfigError("Lambda handler not available. " + "Add 'lambda' to enabled_network_handlers in cats.yaml and redeploy.")
        return self._deployed.lambda_

    # =========================================================================
    # Default methods - delegate to the first enabled handler
    # =========================================================================

    def http_call(self, host: str, port: int) -> CatsNetworkResult:
        """Make an HTTP call (GET /) using the first enabled network handler; outcomes and errors are those of ec2_http_call or lambda_http_call."""
        return self._dispatch["http"](host, port)

    def https_call(self, host: str, port: int) -> CatsNetworkResult:
        """Make an HTTPS call (GET /) using the first enabled network handler; outcomes and errors follow ec2_https_call or lambda_https_call."""
        return self._dispatch["https"](host, port)

    def tcp_call(self, host: str, port: int) -> CatsNetworkResult:
        """Make TCP call using the first enabled network handler; outcomes and errors are those of ec2_tcp_call or lambda_tcp_call."""
        return self._dispatch["tcp"](host, port)

    def udp_call(self, host: str, port: int) -> CatsNetworkResult:
        """Send a UDP datagram using the first enabled network handler; outcomes and errors are those of ec2_udp_call or lambda_udp_call."""
        return self._dispatch["udp"](host, port)

    # =========================================================================
    # UDP receipts
    # =========================================================================

    def receive_s3(self, sent: CatsNetworkResult, *, timeout: float = 10.0, interval: float = 2.0) -> bool:
        """
        Confirm that a UDP datagram sent with udp_call arrived at a CATS listener,
        by polling this account's response bucket for the receipt the listener writes.

        Args:
            sent: The result returned by udp_call / ec2_udp_call / lambda_udp_call
            timeout: Maximum seconds to wait for the receipt (default 10.0); keyword-only
            interval: Seconds between polls (default 2.0); keyword-only

        Returns:
            bool: True if the receipt exists, False if it did not appear within timeout

        Raises:
            CatsUsageError: If `sent` is not a UDP result, or the datagram never left the
                host (response is not SENT). A DNS_FAILURE is not "dropped in flight",
                so it must not be allowed to read as False.
            CatsInfrastructureError: The response bucket cannot be read (missing bucket,
                access denied): a receipt that cannot be checked must not read as False.
        """
        if sent.correlation_id is None:
            raise CatsUsageError(f"receive_s3 needs the result of a udp_call; {sent!r} carries no correlation_id")
        if sent.response is not CatsNetworkResponse.SENT:
            raise CatsUsageError(f"receive_s3 needs a SENT result; the datagram never left the host: {sent!r}")
        return self._receive_s3_by_id(sent.correlation_id, timeout, interval)

    def _receive_s3_by_id(self, correlation_id: str, timeout: float = 10.0, interval: float = 2.0) -> bool:
        """Poll the response bucket for a receipt keyed by correlation_id.

        The CLI and MCP `receive-s3` run in a different process from the `udp-call`
        that produced the ID, so they call this directly rather than constructing a
        SENT result they did not observe.
        """
        deadline = time.time() + timeout
        client = self._host_account.session.client("s3")

        while time.time() < deadline:
            try:
                client.head_object(Bucket=self.response_bucket_name, Key=correlation_id, ExpectedBucketOwner=self._host_account.account_id)
                return True
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    time.sleep(interval)
                    continue
                raise CatsInfrastructureError(
                    f"Cannot read receipt '{correlation_id}' from response bucket '{self.response_bucket_name}' of account "
                    + f"'{self._host_account.name}': {e.response['Error']['Code']}. The bucket is created by 'cats infra deploy' and "
                    + "read through the account's default test role; redeploy if the stack changed."
                ) from e
        return False

    @property
    def ec2_dns_name(self) -> str:
        """
        The DNS name for deployed EC2 listener in this network target

        Returns:
            str: DNS name
        """
        return self._deployed.ec2.dns_name

    @property
    def response_bucket_name(self) -> str:
        """
        The S3 bucket name for deployed S3 bucket in this account for receiving UDP
        call side-effects.

        Returns:
            str: S3 bucket name
        """
        return self._deployed.response_bucket_name

    # =========================================================================
    # Lambda Handler Methods
    # =========================================================================

    def _lambda_probe(self, protocol: Protocol, host: str, port: int, message: str | None = None) -> CatsNetworkResult:
        """Invoke the target's probe Lambda and parse its payload, which is the probe's JSON document."""
        host = _validate_host(host)
        _validate_port(port)
        function_name = self._lambda.probe_lambda_name
        event: dict[str, object] = {"PROTOCOL": protocol, "TARGET_URL": host, "TARGET_PORT": port}
        if message is not None:
            event["TARGET_MESSAGE"] = message
        lmbda = self._host_account.session.client("lambda")
        response = _invoke_probe_lambda(lmbda, function_name, json.dumps(event))
        payload = response["Payload"].read()
        logger.debug("Lambda %s payload: %s", function_name, payload)
        function_error = response.get("FunctionError", "")
        if function_error != "":
            # The runtime, not the probe, produced the payload: a timeout, an out-of-memory kill, an
            # import failure. Its errorMessage is the diagnosis; parsing it as a probe result would
            # bury it under "Invalid probe output".
            raise CatsLambdaInvocationError(f"Lambda {function_name} failed ({function_error}): {payload.decode(errors='replace')}")
        return _parse_probe_output(payload, function_name, CatsLambdaInvocationError)

    def lambda_http_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Make an HTTP call (GET /) using Lambda deployed in this target network segment.
        Requires 'lambda' in enabled_network_handlers.

        Returns:
            CatsNetworkResult: SUCCESS (any HTTP status in reply; http_status carries the code),
                REFUSED, TIMEOUT, DNS_FAILURE, or FAILURE - see ec2_http_call for what each proves.

        Raises:
            CatsConfigError: If Lambda handler not enabled
            CatsLambdaInvocationError: If the Lambda invocation or the probe itself fails
        """
        return self._lambda_probe("http", host, port)

    def lambda_https_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Make an HTTPS call (GET /) using Lambda deployed in this target network segment.
        Requires 'lambda' in enabled_network_handlers.

        Returns:
            CatsNetworkResult: SUCCESS (any HTTP status in reply; http_status carries the code),
                REFUSED, TIMEOUT, DNS_FAILURE, TLS_FAILURE, or FAILURE - see ec2_https_call for what each proves.

        Raises:
            CatsConfigError: If Lambda handler not enabled
            CatsLambdaInvocationError: If the Lambda invocation or the probe itself fails
        """
        return self._lambda_probe("https", host, port)

    def lambda_tcp_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Make a TCP connection using Lambda deployed in this target network segment.
        Requires 'lambda' in enabled_network_handlers.

        Returns:
            CatsNetworkResult: SUCCESS, REFUSED, TIMEOUT, DNS_FAILURE, or FAILURE - see CatsNetworkResponse docs.

        Raises:
            CatsConfigError: If Lambda handler not enabled
            CatsLambdaInvocationError: If the Lambda invocation or the probe itself fails
        """
        return self._lambda_probe("tcp", host, port)

    def lambda_udp_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Send a UDP datagram using Lambda deployed in this target network segment.
        Pass the result to receive_s3 on the receiving account to confirm arrival.
        Requires 'lambda' in enabled_network_handlers.

        Returns:
            CatsNetworkResult: SENT, DNS_FAILURE, or FAILURE, with correlation_id set to
                the generated receipt ID. REFUSED and TIMEOUT are unreachable for UDP.

        Raises:
            CatsConfigError: If Lambda handler not enabled
            CatsLambdaInvocationError: If the Lambda invocation or the probe itself fails
        """
        return self._udp_call(self._lambda_probe, host, port)

    def _udp_call(self, transport: ProbeTransport, host: str, port: int) -> CatsNetworkResult:
        """Generate the receipt ID, send the token-prefixed datagram, and attach the ID to the result."""
        correlation_id = str(uuid.uuid4())
        _validate_correlation_id(correlation_id)
        result = transport("udp", host, port, _udp_wire_message(host, correlation_id))
        return replace(result, correlation_id=correlation_id)

    # =========================================================================
    # EC2 Handler Methods (via SSM RunCommand)
    # =========================================================================

    def _run_ssm_raw(self, document_name: str, params: dict[str, list[str]]) -> tuple[str, str, int, int]:
        """
        Send an SSM command and poll until a definitive status is reached.

        Takes a fully-formed document name (caller does any prefixing). Returns raw,
        unstripped (stdout, stderr, exit_code, duration_ms). Callers decide whether
        to strip. duration_ms is wall-clock from SendCommand acceptance and includes
        poll latency.

        Status mapping:
        - Pending / InProgress / Delayed: keep polling
        - Success: return (stdout, stderr, 0, duration_ms)
        - Failed: return (stdout, stderr, ResponseCode, duration_ms) — command ran
          but exited non-zero; caller decides semantics.
        - All others (TimedOut, Cancelled, Cancelling, Undeliverable, Terminated,
          AccessDenied, DeliveryTimedOut, ExecutionTimedOut): raise CatsSSMError.

        Raises:
            CatsSSMError: On infrastructure failures only (no stdout/stderr/exit_code
                to return). Includes SendCommand ClientErrors (InvalidDocument; InvalidInstanceId
                at once for an instance SSM lists as registered, after the registration window
                for one it does not) and non-{Success, Failed} terminal statuses.
        """
        ssm = self._host_account.session.client("ssm")
        target_config = self._host_account._account_config.account_network_under_test[self.name]
        timeout = target_config.network_settings.network_call_timeout_seconds
        instance_id = self._deployed.ec2.instance_id

        response = self._send_command(ssm, document_name, params, timeout)
        command_id = response["Command"]["CommandId"]
        start = time.time()

        deadline = time.time() + timeout + 5
        last_status: str | None = None  # None: no invocation was ever visible, so the command never reached the instance
        while time.time() < deadline:
            try:
                invocation = ssm.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id,
                )
            except ClientError as e:
                if "InvocationDoesNotExist" in str(e):
                    time.sleep(1)
                    continue
                raise

            status = invocation["Status"]
            last_status = status
            if status in ("Pending", "InProgress", "Delayed"):
                time.sleep(1)
                continue

            raw_stdout = invocation.get("StandardOutputContent", "")
            raw_stderr = invocation.get("StandardErrorContent", "")
            duration_ms = int((time.time() - start) * 1000)

            if status == "Success":
                return raw_stdout, raw_stderr, 0, duration_ms
            if status == "Failed":
                exit_code = int(invocation.get("ResponseCode", 1))
                return raw_stdout, raw_stderr, exit_code, duration_ms
            raise CatsSSMError(f"SSM command {status}: {invocation.get('StatusDetails', '')}. stderr: {raw_stderr}")

        if last_status is None or last_status == "Pending":
            raise CatsSSMError(
                f"SSM command was not delivered to the instance within {timeout}s: SSM accepted it, so the instance is registered, "
                + "but its agent did not pick it up. The instance is stopping, or its agent has lost its path to the SSM services "
                + "(NAT, a route to your organization's endpoints, or interface endpoints in its VPC)"
            )
        raise CatsSSMError(
            f"SSM command was still {last_status} after {timeout}s and CATS stopped waiting; it may still be running on the instance. "
            + "Raise network_call_timeout_seconds for commands that legitimately take longer"
        )

    def _send_command(self, ssm: "SSMClient", document_name: str, params: dict[str, list[str]], timeout: int) -> "SendCommandResultTypeDef":
        """Send the SSM command, waiting out the agent-registration window that follows a fresh deploy."""
        instance_id = self._deployed.ec2.instance_id
        account_name = self._deployed.account_name
        window = 0 if instance_id in _ssm_unregistered_instances else SSM_REGISTRATION_TIMEOUT_SECONDS
        registration_deadline = time.time() + window

        while True:
            try:
                return ssm.send_command(
                    InstanceIds=[instance_id],
                    DocumentName=document_name,
                    Parameters=params,
                    TimeoutSeconds=timeout,
                )
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "InvalidDocument":
                    # _ssm_probe has already established that ec2 is an enabled handler, so the document was deployed
                    # by a release that named it differently (reachable under CATS_SKIP_VERSION_CHECK=1).
                    raise CatsSSMError(
                        f"SSM document '{document_name}' not found: the stack was deployed by another CATS release. "
                        + RECREATE_REMEDIATION.format(account_name=account_name)
                    ) from e
                if error_code != "InvalidInstanceId":
                    raise
                ping_status = _ssm_ping_status(ssm, instance_id)
                if ping_status == "Online":
                    raise CatsSSMError(
                        f"EC2 instance {instance_id} is listed in SSM as Online yet refused SendCommand. Consistent causes: "
                        + "the caller may not target this instance (the test role's grant requires the instance tag cats:stack to name this stack; "
                        + "a policy denying ssm:SendCommand on the instance looks the same), the instance is shutting down, "
                        + "or its agent registered moments ago and SendCommand has not caught up yet (retry). "
                        + RECREATE_REMEDIATION.format(account_name=account_name)
                    ) from e
                if ping_status is not None:
                    raise CatsSSMError(
                        f"EC2 instance {instance_id} is registered with SSM but its agent is not connected (PingStatus {ping_status}): "
                        + "SSM Agent has stopped or lost its route to the SSM endpoints. "
                        + RECREATE_REMEDIATION.format(account_name=account_name)
                    ) from e
                if time.time() >= registration_deadline:
                    _ssm_unregistered_instances.add(instance_id)
                    raise CatsSSMError(
                        f"EC2 instance {instance_id} not registered with SSM after {SSM_REGISTRATION_TIMEOUT_SECONDS}s. "
                        + "Either the instance is stopped or terminated, or its subnet gives it no path to the SSM services "
                        + "(ssm, ssmmessages, ec2messages): the listener has no public IPv4 address, so an internet gateway carries no IPv4 "
                        + "traffic for it, and it needs NAT, a route to your organization's endpoints, or interface endpoints in its VPC. "
                        + "Without that path the EC2 handler cannot be the source for this target; use the Lambda handler as the source: "
                        + "enable it (enabled_network_handlers: [lambda, ec2]) or, where it is enabled, call lambda_<protocol>_call. "
                        + "The listener remains a target."
                    ) from e
                logger.debug("EC2 instance %s not yet registered with SSM; retrying in %ss", instance_id, SSM_REGISTRATION_POLL_SECONDS)
                time.sleep(SSM_REGISTRATION_POLL_SECONDS)

    def _run_ssm_document(self, params: dict[str, list[str]]) -> CatsNetworkResult:
        """
        Run the account's CATS probe SSM document on this target's EC2 instance and parse its JSON result.

        Contract (differs from _run_ssm_raw):
        1. Builds the CATS-prefixed document name.
        2. Strips stdout before parsing.
        3. Parses the JSON output into a CatsNetworkResult.
        4. Raises CatsSSMError on non-zero exit (SSM status Failed). shell() does not.

        Args:
            params: SSM document parameters (keys must match SSM document parameter names
                defined in cats/cfn/templates/stack.yaml.j2 - AWS prefixes them with SSM_
                when using interpolationType: ENV_VAR)

        Raises:
            CatsSSMError: If SSM command fails, exits non-zero, or returns an ERROR result.
        """
        account_name = self._deployed.account_name
        doc_name = f"{self._host_account.cfn_stack_prefix}{account_name}-Probe"
        raw_stdout, raw_stderr, exit_code, _ = self._run_ssm_raw(doc_name, params)
        if exit_code != 0:
            raise CatsSSMError(f"SSM command Failed: {doc_name} exited {exit_code}. stderr: {raw_stderr}")
        return _parse_probe_output(raw_stdout.strip(), doc_name, CatsSSMError)

    def _ssm_probe(self, protocol: Protocol, host: str, port: int, message: str | None = None) -> CatsNetworkResult:
        """Run the probe on the EC2 instance. SSM takes every parameter as a string.

        The probe document exists only where the ec2 handler is deployed, and the outputs say
        which handlers are; a target without it is refused here, as the Lambda side refuses in
        `_lambda`, rather than after a SendCommand that can only fail.
        """
        if "ec2" not in self._deployed.enabled_handlers:
            raise CatsConfigError("EC2 handler not available. Add 'ec2' to enabled_network_handlers in cats.yaml and redeploy.")
        host = _validate_host(host)
        _validate_port(port)
        params: dict[str, list[str]] = {"Protocol": [protocol], "TargetUrl": [host], "TargetPort": [str(port)]}
        if message is not None:
            params["TargetMessage"] = [message]
        return self._run_ssm_document(params)

    def shell(self, command: str | list[str]) -> ShellResult:
        """
        Run a shell command on the EC2 instance via SSM AWS-RunShellScript.
        Requires `allow_shell: true` on the account's `account_settings` at deploy time.

        EC2-only — does not participate in handler dispatch. Runs as root with the
        EC2 instance's IAM role (a hand-scoped SSM Agent policy with no Parameter
        Store read, plus `s3:PutObject` on the response bucket), not the CATS test
        role. The check is a convenience; the real enforcement is the test role's
        IAM policy, set at deploy time.

        Shell metacharacters (pipes, redirects, &&) work within a single command
        string. When passing a list, each element is an independent line — not a
        pipeline.

        Args:
            command: Shell command string, or list of command lines.

        Returns:
            ShellResult with stdout, stderr, exit_code, duration_ms, stdout_truncated, stderr_truncated.
            stdout and stderr are stripped of trailing whitespace; truncation flags are computed
            against the raw SSM output before stripping.

        Raises:
            CatsConfigError: If allow_shell is not enabled for this account.
            CatsSSMError: If SSM infrastructure fails (instance unreachable, timeout, etc.).
        """
        if self._host_account._account_config.account_settings.allow_shell is not True:
            raise CatsConfigError(
                f"allow_shell is not enabled for account '{self._host_account.name}'. Set allow_shell: true under account_settings and redeploy."
            )
        commands = [command] if isinstance(command, str) else list(command)
        raw_stdout, raw_stderr, exit_code, duration_ms = self._run_ssm_raw("AWS-RunShellScript", {"commands": commands})
        return ShellResult(
            stdout=raw_stdout.strip(),
            stderr=raw_stderr.strip(),
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_truncated=len(raw_stdout) == SSM_STDOUT_LIMIT,
            stderr_truncated=len(raw_stderr) == SSM_STDERR_LIMIT,
        )

    def ec2_http_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Make an HTTP call (GET /) from the EC2 instance via SSM RunCommand.

        The probe asks whether the path is open and something answers HTTP, not what the
        application does with a request: any status in reply is SUCCESS.

        Returns:
            CatsNetworkResult: SUCCESS if any HTTP status arrived (http_status carries the
                code; redirects are not followed), REFUSED if the connection was refused
                or reset, TIMEOUT if the TCP connect got no answer (consistent with a
                silent drop), DNS_FAILURE if the name did not resolve, FAILURE for
                anything else, including a backend that accepted the connection and
                never replied (reason starts "connected; no HTTP reply").

        Raises:
            CatsSSMError: If SSM command fails due to infrastructure problems
        """
        return self._ssm_probe("http", host, port)

    def ec2_https_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Make an HTTPS call (GET /) from the EC2 instance via SSM RunCommand.

        Returns:
            CatsNetworkResult: SUCCESS if any HTTP status arrived (http_status carries the
                code), REFUSED, TIMEOUT, DNS_FAILURE, TLS_FAILURE if TCP connected
                but the handshake failed (untrusted CA, plaintext listener, ...), FAILURE
                for anything else; see ec2_http_call for what each proves.

        Raises:
            CatsSSMError: If SSM command fails due to infrastructure problems
        """
        return self._ssm_probe("https", host, port)

    def ec2_tcp_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Make a TCP connection from the EC2 instance via SSM RunCommand.

        Returns:
            CatsNetworkResult: SUCCESS if the TCP connection succeeded, REFUSED if the
                connection was refused or reset, TIMEOUT if no response arrived before
                the timeout (consistent with a silent drop), DNS_FAILURE if the name
                did not resolve, FAILURE for unclassified errors.

        Raises:
            CatsSSMError: If SSM command fails due to infrastructure problems
        """
        return self._ssm_probe("tcp", host, port)

    def ec2_udp_call(self, host: str, port: int) -> CatsNetworkResult:
        """
        Send a UDP datagram from the EC2 instance via SSM RunCommand.
        Pass the result to receive_s3 on the receiving account to confirm arrival.

        Returns:
            CatsNetworkResult: SENT, DNS_FAILURE, or FAILURE, with correlation_id set to
                the generated receipt ID. REFUSED and TIMEOUT are unreachable for UDP:
                the probe sends on an unconnected socket, so the datagram leaving the
                host is all it can observe.

        Raises:
            CatsSSMError: If SSM command fails due to infrastructure problems
        """
        return self._udp_call(self._ssm_probe, host, port)
