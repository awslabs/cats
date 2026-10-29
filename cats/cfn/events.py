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

"""CloudFormation event polling for progress display."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from cats.cfn.retry import retry_on_throttle

if TYPE_CHECKING:
    from mypy_boto3_cloudformation import CloudFormationClient


@dataclass
class StackEvent:
    """A CloudFormation stack event."""

    timestamp: datetime
    logical_id: str
    resource_type: str
    status: str
    status_reason: str | None


TERMINAL_SUCCESS: frozenset[str] = frozenset(
    {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "DELETE_COMPLETE",
    }
)

TERMINAL_FAILURE: frozenset[str] = frozenset(
    {
        "CREATE_FAILED",
        "DELETE_FAILED",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
        "UPDATE_FAILED",
        "UPDATE_ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_FAILED",
    }
)


def is_terminal(status: str) -> bool:
    """Check if a stack status is terminal."""
    return status in TERMINAL_SUCCESS or status in TERMINAL_FAILURE


def is_success(status: str) -> bool:
    """Check if a stack status indicates success."""
    return status in TERMINAL_SUCCESS


@dataclass
class StackPoller:
    """
    Non-blocking event poller for a single stack.

    Tracks seen events for deduplication. Call fetch_new_events()
    repeatedly to get new events. Does not block - caller controls timing.

    Usage (single stack):
        poller = StackPoller(cfn_client, "my-stack")
        while not poller.is_complete:
            for event in poller.fetch_new_events():
                print(event)
            time.sleep(5)

    Usage (multiple stacks):
        pollers = [StackPoller(client, name) for name in stack_names]
        while any(not p.is_complete for p in pollers):
            for poller in pollers:
                for event in poller.fetch_new_events():
                    handle_event(poller.stack_name, event)
            time.sleep(5)
    """

    cfn_client: "CloudFormationClient"
    stack_name: str
    _seen_event_ids: set[str] = field(default_factory=set)
    final_status: str | None = field(default=None, init=False)

    @retry_on_throttle
    def fetch_new_events(self) -> list[StackEvent]:
        """
        Fetch new events since last call. Non-blocking, single API call.

        Returns list of new events (oldest first).
        Sets self.final_status when stack reaches terminal state.
        Returns empty list if already complete.
        """
        if self.final_status is not None:
            return []

        response = self.cfn_client.describe_stack_events(StackName=self.stack_name)
        raw_events = response.get("StackEvents", [])

        new_events: list[StackEvent] = []
        # Events come newest-first, reverse to return oldest-first
        for raw in reversed(raw_events):
            event_id = raw["EventId"]
            if event_id in self._seen_event_ids:
                continue
            self._seen_event_ids.add(event_id)

            event = StackEvent(
                timestamp=raw["Timestamp"],
                logical_id=raw["LogicalResourceId"],
                resource_type=raw["ResourceType"],
                status=raw["ResourceStatus"],
                status_reason=raw.get("ResourceStatusReason"),
            )
            new_events.append(event)

            # Stack-level events have resource_type AWS::CloudFormation::Stack
            if event.resource_type == "AWS::CloudFormation::Stack" and is_terminal(event.status):
                self.final_status = event.status

        return new_events

    @property
    def is_complete(self) -> bool:
        """True if stack has reached terminal state."""
        return self.final_status is not None

    @classmethod
    def seeded(cls, cfn_client: "CloudFormationClient", stack_name: str) -> "StackPoller":
        """Create a poller pre-seeded with existing events.

        Use this when starting an operation on an existing stack to avoid
        seeing historical events. The poller will only return events that
        occur after this call.
        """
        poller = cls(cfn_client, stack_name)
        poller.fetch_new_events()  # Discard historical events
        poller.final_status = None  # Reset - we want to wait for new terminal state
        return poller
