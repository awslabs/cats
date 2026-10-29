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

"""A VPC that lives for one module's run, for a CATS stack that must create its own VPC endpoints.

Every other integration module deploys into the base stack's VPC and uses the endpoints it has.
Private DNS allows one interface endpoint per service per VPC, so a CATS stack that creates its
own `ssm` endpoint cannot share that VPC, or any VPC another pipeline's run is using. Such a
module creates a VPC from infra/endpoint-test-vpc.yaml, deploys CATS into it, and deletes both in
reverse order; this is the first half of that.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import WaiterError

if TYPE_CHECKING:
    from mypy_boto3_cloudformation import CloudFormationClient
    from mypy_boto3_cloudformation.type_defs import WaiterConfigTypeDef

TEMPLATE_PATH = Path(__file__).parent / "infra" / "endpoint-test-vpc.yaml"
# The stack has no endpoints of its own, so it creates and deletes in about a minute; ten
# minutes is the bound for a stack that is stuck, not slow.
WAITER_CONFIG: "WaiterConfigTypeDef" = {"Delay": 10, "MaxAttempts": 60}


def _failed_resources(cloudformation: "CloudFormationClient", stack_name: str) -> list[str]:
    """Each failed resource event of the stack, so a stack that did not converge says why."""
    events = cloudformation.describe_stack_events(StackName=stack_name)["StackEvents"]
    return [
        f"{event['LogicalResourceId']} {event['ResourceStatus']}: {event.get('ResourceStatusReason', '')}"
        for event in events
        if event["ResourceStatus"].endswith("_FAILED")
    ]


@contextmanager
def per_run_vpc(stack_name: str, parameter_prefix: str, region: str) -> Iterator[dict[str, str]]:
    """Create the per-run VPC stack, yield its outputs by key, and delete it on exit.

    The delete runs even when the create failed: a failed create rolls back to ROLLBACK_COMPLETE,
    which deletes, and a stack left behind would make every later run with the same run id fail
    at creation. A delete that fails raises naming the stack, so the orphan is visible rather than
    silent. Credentials and Region come from the environment, as the tests take them.
    """
    cloudformation: CloudFormationClient = boto3.Session(region_name=region).client("cloudformation")
    print(f"\n[fixture] Creating per-run VPC stack {stack_name}...")
    cloudformation.create_stack(
        StackName=stack_name,
        TemplateBody=TEMPLATE_PATH.read_text(encoding="utf-8"),
        Parameters=[{"ParameterKey": "ParameterPrefix", "ParameterValue": parameter_prefix}],
    )
    try:
        try:
            cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name, WaiterConfig=WAITER_CONFIG)
        except WaiterError as e:
            raise RuntimeError(f"Per-run VPC stack {stack_name} did not create: {_failed_resources(cloudformation, stack_name)}") from e
        stack = cloudformation.describe_stacks(StackName=stack_name)["Stacks"][0]
        outputs = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
        print(f"[fixture] Per-run VPC stack {stack_name} outputs: {outputs}")
        yield outputs
    finally:
        print(f"\n[fixture] Deleting per-run VPC stack {stack_name}...")
        cloudformation.delete_stack(StackName=stack_name)
        try:
            cloudformation.get_waiter("stack_delete_complete").wait(StackName=stack_name, WaiterConfig=WAITER_CONFIG)
        except WaiterError as e:
            raise RuntimeError(f"Per-run VPC stack {stack_name} did not delete: {_failed_resources(cloudformation, stack_name)}") from e
        print(f"[fixture] Deleted per-run VPC stack {stack_name}")
