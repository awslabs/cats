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

"""Bring the persistent integration-test base stacks in line with the templates in this directory.

The home base stack (integ-test-vpc.yaml) holds the VPC, subnet, SSM endpoints and shared handler
security group that every integration test builds on. The cross base stack
(integ-test-cross-vpc.yaml) is its counterpart in the cross account, peered to it, for the tests
where a listener in one account probes a listener in the other. Both are persistent - CATS stacks
come and go per run, these do not - so a change to a template used to need a human to redeploy it
before the suite could pass. CI runs this script before pytest instead, which keeps the templates
on the branch authoritative and removes that manual step.

Each stack's name is discovered rather than assumed: the home stack was created by hand and its
name is recorded nowhere in the tree, so the physical ID of a resource it owns (the SSM parameter
holding the VPC ID) is the reliable handle. A fresh account has no such parameter, so the script
creates the stack under its fallback name instead.

The two stacks are circular. The home stack owns the peering connection, for which it needs the
cross VPC's ID and an accept role ARN; the cross stack needs the peering ID for its route back. So
with CATS_INTEG_CROSS_ACCOUNT_ID set the script converges in a fixed order: the cross stack
(peering ID carried forward, empty on the very first run), the home stack with the cross stack's
outputs as parameters, the cross stack again with the new peering ID, and finally the peering's
DNS options on each side, from that side's own account. Unset, it converges the home stack alone,
carrying every deployed parameter forward, so a single-account run never removes CI's peering by
converging the parameters' empty defaults.

Concurrency needs no external lock, even when several pipelines run this script at
once. Two rules do the work:

1. CloudFormation is single-writer per stack. The script settles any operation already in
   progress before deciding anything, and treats a mid-flight rejection as "another pipeline is
   writing" rather than an error - waiting and rechecking instead of failing.
2. Convergence is monotonic. Each template carries a version marker in its Description - `(vN)` -
   and the script never deploys a version older than the one already deployed. Without that, a
   stale branch's pipeline would converge the shared stack backward while a newer pipeline's
   tests were mid-flight.

The invariant this creates: changes to the base templates MUST be additive and backward
compatible, because branches older than the deployed version run their tests against them. Bump
the marker with every change so concurrent writers order themselves; a downgrade is then
structurally impossible. Equal versions still attempt an update, so a template edit whose author
forgot to bump still converges - the residual accepted case is two branches at the same version
with different content, which is last-writer-wins and is caught in review, not here.

Run with `uv run python tests/integration/infra/deploy_base_stack.py`. Region and credentials
come from the environment via boto3's default chain, exactly as the tests take them; the cross
account is reached by assuming its CATS-integ-deploy-role, as the tests do.
"""

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, WaiterError

from tests.integration.constants import CROSS_ACCOUNT_DEPLOY_ROLE

if TYPE_CHECKING:
    from mypy_boto3_cloudformation import CloudFormationClient
    from mypy_boto3_cloudformation.literals import CapabilityType
    from mypy_boto3_cloudformation.type_defs import ParameterTypeDef, StackTypeDef
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ec2.type_defs import PeeringConnectionOptionsRequestTypeDef

TEMPLATE_PATH = Path(__file__).parent / "integ-test-vpc.yaml"
FALLBACK_STACK_NAME = "CATS-integ-test-vpc"
# An SSM parameter the base stack owns. Its value is the VPC ID; its physical ID is the name,
# which is what describe_stack_resources looks up.
VPC_ID_PARAMETER = "/cats/integ-test/vpc-id"
CROSS_TEMPLATE_PATH = Path(__file__).parent / "integ-test-cross-vpc.yaml"
CROSS_FALLBACK_STACK_NAME = "CATS-integ-test-cross-vpc"
CROSS_VPC_ID_PARAMETER = "/cats/integ-test/cross-vpc-id"
CROSS_ACCOUNT_VARIABLE = "CATS_INTEG_CROSS_ACCOUNT_ID"
# The parameters and outputs the two templates exchange. Names are the templates' own: the home
# stack's PeeringConnectionId output feeds the cross stack's PeeringConnectionId parameter, so the
# two constants coincide by design.
HOME_ACCOUNT_PARAMETER = "HomeAccountId"
PEERING_ID_PARAMETER = "PeeringConnectionId"
PEERING_ID_OUTPUT = "PeeringConnectionId"
NO_UPDATES_MESSAGE = "No updates are to be performed."
IN_PROGRESS_SUFFIX = "_IN_PROGRESS"
SETTLE_POLL_SECONDS = 10
# Creating the VPC endpoints is the slowest thing these stacks do, in minutes not tens of them.
# A stack still busy after this long is stuck, not shared, so fail rather than wait out the job.
SETTLE_TIMEOUT_SECONDS = 1800
UPDATE_ATTEMPTS = 2
# The version marker in the template Description, and the version a stack deployed before
# markers existed is taken to be.
VERSION_MARKER = re.compile(r"\(v(?P<version>\d+)\)")
DESCRIPTION_LINE = re.compile(r"^Description:(?P<description>.*)$", re.MULTILINE)
UNVERSIONED = 1

# Outcomes of one update attempt.
UPDATE_STARTED = "started"
ALREADY_CURRENT = "current"
STACK_BUSY = "busy"

PeeringSide = Literal["Requester", "Accepter"]


@dataclass(frozen=True)
class BaseStack:
    """One persistent stack: where its template is, how to find it, and how to create it."""

    template_path: Path
    fallback_name: str
    discovery_parameter: str
    capabilities: tuple["CapabilityType", ...] = ()


# The cross stack names an IAM role (the peering accept role), hence the capability; the home stack
# declares no IAM resources and passes none.
HOME_STACK = BaseStack(TEMPLATE_PATH, FALLBACK_STACK_NAME, VPC_ID_PARAMETER)
CROSS_STACK = BaseStack(CROSS_TEMPLATE_PATH, CROSS_FALLBACK_STACK_NAME, CROSS_VPC_ID_PARAMETER, ("CAPABILITY_NAMED_IAM",))


@dataclass(frozen=True)
class AccountClients:
    """The clients the script needs in one account, and which account that is."""

    account_id: str
    cloudformation: "CloudFormationClient"
    ec2: "EC2Client"


class BaseStackBusyError(RuntimeError):
    """The base stack was being written by someone else for longer than this script will wait."""


class BaseStackOutputError(RuntimeError):
    """A converged stack lacks an output the other stack's parameters need."""


def description_version(description: str) -> int:
    """Version a Description declares, or UNVERSIONED when it declares none."""
    marker = VERSION_MARKER.search(description)
    if marker is None:
        return UNVERSIONED
    return int(marker.group("version"))


def template_version(template_body: str) -> int:
    """Version the local template declares, read from its Description line.

    Regex rather than a yaml parse because the template uses CloudFormation's short-form tags,
    which safe_load rejects, and only this one line matters.
    """
    description = DESCRIPTION_LINE.search(template_body)
    if description is None:
        return UNVERSIONED
    return description_version(description.group("description"))


def find_stack_name(cloudformation: "CloudFormationClient", discovery_parameter: str) -> str | None:
    """Name of the stack owning the SSM parameter, or None when nothing owns it yet.

    CloudFormation raises ValidationError for both an unknown physical ID and a stack that
    does not exist, and neither is distinguishable from the other here, so any ClientError
    means "no such base stack in this account".
    """
    try:
        resources = cloudformation.describe_stack_resources(PhysicalResourceId=discovery_parameter)
    except ClientError:
        return None
    return resources["StackResources"][0]["StackName"]


def describe_stack(cloudformation: "CloudFormationClient", stack_name: str) -> "StackTypeDef":
    """Current state of a stack that is known to exist: status, Description, parameters and outputs."""
    return cloudformation.describe_stacks(StackName=stack_name)["Stacks"][0]


def stack_outputs(stack: "StackTypeDef") -> dict[str, str]:
    """A stack's outputs by key. A conditional output that did not fire is simply absent."""
    return {output["OutputKey"]: output["OutputValue"] for output in stack.get("Outputs", [])}


def stack_parameters(explicit: dict[str, str], deployed: "StackTypeDef | None") -> list["ParameterTypeDef"]:
    """The parameter list for a create or update: explicit values, then UsePreviousValue for the rest.

    Every parameter the deployed stack carries that the caller did not set is carried forward
    unchanged, which is what keeps a single-account run from converging the home stack's peering
    parameters back to their empty defaults. A create has nothing to carry.
    """
    parameters: list[ParameterTypeDef] = [{"ParameterKey": key, "ParameterValue": value} for key, value in explicit.items()]
    if deployed is None:
        return parameters
    for deployed_parameter in deployed.get("Parameters", []):
        if deployed_parameter["ParameterKey"] not in explicit:
            parameters.append({"ParameterKey": deployed_parameter["ParameterKey"], "UsePreviousValue": True})
    return parameters


def wait_for_settle(cloudformation: "CloudFormationClient", stack_name: str) -> "StackTypeDef":
    """Block until no operation is in progress, and report the stack as it settled.

    Polling rather than a waiter because the operation in flight belongs to another pipeline
    and could be a create, an update or a rollback. REVIEW_IN_PROGRESS is the one status that
    never settles, and it cannot apply here: it means a stack that only ever had a change set,
    which the base stacks are not. Such a stack would time out and fail loudly.
    """
    waited = 0
    stack = describe_stack(cloudformation, stack_name)
    while stack["StackStatus"].endswith(IN_PROGRESS_SUFFIX):
        if waited >= SETTLE_TIMEOUT_SECONDS:
            raise BaseStackBusyError(f"{stack_name} has been {stack['StackStatus']} for {waited}s: giving up rather than deploying over it")
        print(f"{stack_name} is {stack['StackStatus']}: waiting {SETTLE_POLL_SECONDS}s for whoever is writing it")
        time.sleep(SETTLE_POLL_SECONDS)
        waited += SETTLE_POLL_SECONDS
        stack = describe_stack(cloudformation, stack_name)
    return stack


def try_update(
    cloudformation: "CloudFormationClient",
    stack: BaseStack,
    stack_name: str,
    template_body: str,
    parameters: list["ParameterTypeDef"],
) -> str:
    """Ask for an update once, and classify the answer.

    Returns ALREADY_CURRENT when CloudFormation rejects a no-change update, STACK_BUSY when the
    rejection is because an operation started in the window since settling, and UPDATE_STARTED
    when the update is under way. Anything else is a real failure and raises. Classification
    goes by stack status rather than by matching the rejection message, which varies.
    """
    try:
        cloudformation.update_stack(StackName=stack_name, TemplateBody=template_body, Parameters=parameters, Capabilities=list(stack.capabilities))
    except ClientError as error:
        if error.response["Error"]["Message"].strip() == NO_UPDATES_MESSAGE:
            return ALREADY_CURRENT
        if describe_stack(cloudformation, stack_name)["StackStatus"].endswith(IN_PROGRESS_SUFFIX):
            return STACK_BUSY
        raise
    return UPDATE_STARTED


def update_existing_stack(
    cloudformation: "CloudFormationClient",
    stack: BaseStack,
    stack_name: str,
    template_body: str,
    parameters: dict[str, str],
) -> dict[str, str]:
    """Settle, refuse to go backward, then update - rechecking both after a concurrent write.

    Returns the outputs of the stack as it stands when the script is done with it, whichever way
    that came about: left alone because it is newer, already matching, or freshly updated.
    """
    local_version = template_version(template_body)
    for attempt in range(1, UPDATE_ATTEMPTS + 1):
        deployed = wait_for_settle(cloudformation, stack_name)
        deployed_version = description_version(deployed.get("Description", ""))
        if local_version < deployed_version:
            print(f"Deployed {stack_name} is v{deployed_version}, this branch has v{local_version}: running against the newer stack")
            return stack_outputs(deployed)
        outcome = try_update(cloudformation, stack, stack_name, template_body, stack_parameters(parameters, deployed))
        if outcome == ALREADY_CURRENT:
            print(f"{stack_name} already matches {stack.template_path.name} at v{local_version}: nothing to do")
            return stack_outputs(deployed)
        if outcome == UPDATE_STARTED:
            cloudformation.get_waiter("stack_update_complete").wait(StackName=stack_name)
            print(f"Updated {stack_name} to v{local_version}")
            return stack_outputs(describe_stack(cloudformation, stack_name))
        print(f"{stack_name} was claimed by another writer during attempt {attempt}: settling and rechecking")
    raise BaseStackBusyError(f"{stack_name} was claimed by another writer on all {UPDATE_ATTEMPTS} attempts")


def converge(cloudformation: "CloudFormationClient", stack: BaseStack, parameters: dict[str, str]) -> dict[str, str]:
    """Create or update one base stack, wait for it to settle, and return its outputs.

    Raises on failure - the caller turns that into a nonzero exit so CI does not run tests
    against a half-updated base stack.
    """
    template_body = stack.template_path.read_text(encoding="utf-8")
    stack_name = find_stack_name(cloudformation, stack.discovery_parameter)

    if stack_name is None:
        print(f"No stack owns {stack.discovery_parameter}: creating {stack.fallback_name}")
        cloudformation.create_stack(
            StackName=stack.fallback_name,
            TemplateBody=template_body,
            Parameters=stack_parameters(parameters, None),
            Capabilities=list(stack.capabilities),
        )
        cloudformation.get_waiter("stack_create_complete").wait(StackName=stack.fallback_name)
        print(f"Created {stack.fallback_name}")
        return stack_outputs(describe_stack(cloudformation, stack.fallback_name))

    print(f"Found base stack {stack_name} via {stack.discovery_parameter}")
    return update_existing_stack(cloudformation, stack, stack_name, template_body, parameters)


def required_output(outputs: dict[str, str], key: str, stack: BaseStack) -> str:
    """One output the other stack's parameters need, or a named failure when the stack lacks it."""
    if key not in outputs:
        raise BaseStackOutputError(f"{stack.fallback_name} has no {key} output: its deployed template predates the peering, or dropped it")
    return outputs[key]


def enable_dns_resolution(ec2: "EC2Client", peering_id: str, side: PeeringSide) -> None:
    """Turn on DNS resolution from the remote VPC for one side of the peering, if it is off.

    No CloudFormation property exists for the peering options, and each side can set only its
    own, so this runs once per account after both stacks have converged. Read first: the call is
    idempotent, but a steady-state run should make no write at all.

    Unlike the stack convergence this is not settled or retried. Two pipelines racing here can
    only both set the same value, so no wrong state is reachable; the worst case is one of them
    failing the job on a transient EC2 error and being re-run, which is accepted for a write
    that happens once in the life of the peering.
    """
    peering = ec2.describe_vpc_peering_connections(VpcPeeringConnectionIds=[peering_id])["VpcPeeringConnections"][0]
    vpc_info = peering["RequesterVpcInfo"] if side == "Requester" else peering["AccepterVpcInfo"]
    if vpc_info.get("PeeringOptions", {}).get("AllowDnsResolutionFromRemoteVpc", False) is True:
        print(f"{side} side of {peering_id} already resolves the remote VPC's DNS")
        return
    options: PeeringConnectionOptionsRequestTypeDef = {"AllowDnsResolutionFromRemoteVpc": True}
    if side == "Requester":
        ec2.modify_vpc_peering_connection_options(VpcPeeringConnectionId=peering_id, RequesterPeeringConnectionOptions=options)
    else:
        ec2.modify_vpc_peering_connection_options(VpcPeeringConnectionId=peering_id, AccepterPeeringConnectionOptions=options)
    print(f"Enabled remote DNS resolution on the {side} side of {peering_id}")


def deploy_peered(home: AccountClients, cross: AccountClients) -> None:
    """Converge both base stacks and the peering between them, in the order the cycle allows.

    The cross stack goes first because the home stack's peering needs its VPC ID and accept
    role. On the very first run the cross stack has no peering ID and so no route back; the
    home stack then creates the peering, and the cross stack's second pass adds the route. On
    every later run all three passes are no-change updates.
    """
    cross_outputs = converge(cross.cloudformation, CROSS_STACK, {HOME_ACCOUNT_PARAMETER: home.account_id})
    home_outputs = converge(
        home.cloudformation,
        HOME_STACK,
        {
            "PeerVpcId": required_output(cross_outputs, "VpcId", CROSS_STACK),
            "PeerOwnerId": cross.account_id,
            "PeerRoleArn": required_output(cross_outputs, "PeeringAcceptRoleArn", CROSS_STACK),
            "PeerCidr": required_output(cross_outputs, "VpcCidr", CROSS_STACK),
        },
    )
    peering_id = required_output(home_outputs, PEERING_ID_OUTPUT, HOME_STACK)
    converge(cross.cloudformation, CROSS_STACK, {HOME_ACCOUNT_PARAMETER: home.account_id, PEERING_ID_PARAMETER: peering_id})
    enable_dns_resolution(home.ec2, peering_id, "Requester")
    enable_dns_resolution(cross.ec2, peering_id, "Accepter")


def account_clients(session: boto3.Session, account_id: str) -> AccountClients:
    """The clients this script uses in one account."""
    return AccountClients(account_id=account_id, cloudformation=session.client("cloudformation"), ec2=session.client("ec2"))


def assume_deploy_role(session: boto3.Session, account_id: str) -> boto3.Session:
    """A session in the cross account through its CATS-integ-deploy-role, in the home session's Region.

    The credentials live an hour, which bounds one run comfortably: the cross stack's endpoints
    take minutes to create, and every later run is a few describes.
    """
    credentials = session.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_DEPLOY_ROLE}",
        RoleSessionName="cats-integ-base-stack",
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=session.region_name,
    )


def main() -> int:
    """Exit code for CI: 0 when the base stacks match their templates, 1 when they do not."""
    session = boto3.Session()
    cross_account_id = os.environ.get(CROSS_ACCOUNT_VARIABLE, "")
    try:
        home = account_clients(session, session.client("sts").get_caller_identity()["Account"])
        if cross_account_id == "":
            print(f"{CROSS_ACCOUNT_VARIABLE} is unset: converging the home base stack only")
            converge(home.cloudformation, HOME_STACK, {})
        else:
            deploy_peered(home, account_clients(assume_deploy_role(session, cross_account_id), cross_account_id))
    except NoCredentialsError:
        print("Base stack deploy failed: no AWS credentials. Sign in to the home integration account first; the tests take theirs the same way")
        return 1
    except (ClientError, WaiterError, BaseStackBusyError, BaseStackOutputError) as error:
        print(f"Base stack deploy failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
