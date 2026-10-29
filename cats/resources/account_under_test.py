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

import logging
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

from boto3.session import Session
from botocore.exceptions import ClientError

from cats.cfn.deploy import CATS_VERSION_TAG_KEY, RECREATE_REMEDIATION, get_stack_name, read_deployed_version
from cats.config import Settings
from cats.constants import DeployedEC2Resources, DeployedLambdaResources, DeployedTargetResources
from cats.exceptions import CatsConfigError, CatsInfrastructureError, CatsUsageError, CatsVersionMismatchError
from cats.filepath import get_cats_config_filepath
from cats.resources.network_interact import NetworkHandler, register_udp_receipt_token
from cats.resources.session_interact import SessionHandler
from cats.version import __version__

if TYPE_CHECKING:
    from mypy_boto3_cloudformation.type_defs import StackTypeDef

logger = logging.getLogger(__name__)

SKIP_VERSION_CHECK_ENV_VAR = "CATS_SKIP_VERSION_CHECK"
_SKIP_HINT = f"(set {SKIP_VERSION_CHECK_ENV_VAR}=1 to bypass this check)"


def check_deployed_version(deployed_version: str | None, stack_name: str, account_name: str) -> None:
    """Raise unless the stack was deployed by the installed CATS release."""
    if deployed_version == __version__:
        return

    remediation = RECREATE_REMEDIATION.format(account_name=account_name)
    if deployed_version is None:
        reason = (
            f"CATS stack '{stack_name}' in target account '{account_name}' has no {CATS_VERSION_TAG_KEY} tag, "
            + f"so CATS cannot confirm it matches installed CATS {__version__}. It was not created by 'cats infra deploy'."
        )
    else:
        reason = (
            f"CATS stack '{stack_name}' in target account '{account_name}' was deployed by CATS {deployed_version} "
            + f"but CATS {__version__} is installed. Deployed listeners and handlers may not match this library."
        )

    if os.environ.get(SKIP_VERSION_CHECK_ENV_VAR) == "1":
        logger.warning(f"{SKIP_VERSION_CHECK_ENV_VAR} is set: {reason} Results may not reflect this library.")
        return

    raise CatsVersionMismatchError(f"{reason} {remediation} {_SKIP_HINT}")


def _parse_cfn_outputs_to_network_targets(
    output_map: dict[str, str],
    network_target_names: list[str],
    account_name: str,
    stack_name: str,
    response_bucket: str,
) -> dict[str, DeployedTargetResources]:
    """Parse CFN stack outputs into DeployedTargetResources dict keyed by target name.

    Raises:
        CatsInfrastructureError: If an output for a configured target is missing (cats.yaml
            changed since deploy), or if the outputs disagree with themselves: EnabledHandlers
            says lambda is deployed but there is no ProbeLambdaName output, which is the
            shape of a stack deployed by an older CATS.
    """
    result: dict[str, DeployedTargetResources] = {}
    for target_name in network_target_names:
        try:
            # The scheme is f"{key}{target}" with no separator; the keys are PER_TARGET_OUTPUT_KEYS (cats.constants).
            deployed_ec2 = DeployedEC2Resources(
                dns_name=output_map[f"Ec2DnsName{target_name}"],
                instance_id=output_map[f"Ec2InstanceId{target_name}"],
            )
            enabled_handlers = output_map[f"EnabledHandlers{target_name}"].split(",")

            deployed_lambda: DeployedLambdaResources | None = None
            if f"ProbeLambdaName{target_name}" in output_map:
                deployed_lambda = DeployedLambdaResources(probe_lambda_name=output_map[f"ProbeLambdaName{target_name}"])
            elif "lambda" in enabled_handlers:
                raise CatsInfrastructureError(
                    f"Stack {stack_name} was deployed by another CATS release and its outputs do not match this version. "
                    + RECREATE_REMEDIATION.format(account_name=account_name)
                )

            deployed_target = DeployedTargetResources(
                response_bucket_name=response_bucket,
                account_name=account_name,
                target_name=target_name,
                vpc_id=output_map[f"VpcId{target_name}"],
                subnet_id_list=output_map[f"SubnetIds{target_name}"].split(","),
                enabled_handlers=enabled_handlers,
                ec2=deployed_ec2,
                lambda_=deployed_lambda,
            )
        except KeyError as e:
            raise CatsInfrastructureError(
                f"Stack output {e} not found for network target '{target_name}' in target account '{account_name}'. "
                + f"This usually means cats.yaml changed since last deploy. Redeploy: cats infra deploy {account_name}"
            ) from e

        result[target_name] = deployed_target

    return result


class AccountUnderTest:
    """
    A class to encapsulate a child test account relationship to the parent test runner.

    These are consumed in tests with Account fixtures provided by the CATS pytest plugin.
    Constructing one assumes the account's default test role and reads its CATS stack, so
    the deployed infrastructure must exist. Network targets declared in cats.yaml are
    reached as attributes: ``account.<target_name>`` returns a NetworkHandler. The
    single-word public attributes are exactly ``name``, ``partition``, ``session``, ``role``
    and ``tags``; every other public attribute contains an underscore, which a target name
    cannot, so a valid target name never shadows an attribute.

    Args:
        name: The account's keyname under ``accounts_under_test`` in cats.yaml.

    Raises:
        CatsConfigError: ``name`` is not an account keyname in the loaded cats.yaml, or cats.yaml
            leaves ``aws_region`` unset with no ``AWS_REGION`` in the environment.
        CatsInfrastructureError: The account's CATS stack is missing, its outputs do not match the
            configured network targets, or the default test role cannot be assumed (no credentials
            in the environment, an unreachable endpoint, an STS refusal).
        CatsVersionMismatchError: The stack was deployed by another CATS release.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        settings = Settings()
        if name not in settings.accounts_under_test:
            available = ", ".join(sorted(settings.accounts_under_test)) or "(none)"
            raise CatsConfigError(f"Account '{name}' not found in {get_cats_config_filepath()}. Available accounts: {available}")
        self.name = name
        self._account_config = settings.accounts_under_test[name]
        self.account_id: str = self._account_config.account_id

        deploy = settings.cats_deployment_settings
        self.cfn_stack_prefix = deploy.cfn_stack_prefix
        self.aws_region: str = settings.require_aws_region()
        self.partition = deploy.partition

        self._resource_prefix = f"{self.cfn_stack_prefix}{name}"
        self._network_targets = list(self._account_config.account_network_under_test.keys())
        # One assumed-role session per role for the life of this account object; see SessionHandler.
        self._sessions: dict[str, Session] = {}

        stack = self._describe_stack()
        self.deployed_version: str | None = read_deployed_version(stack)
        check_deployed_version(self.deployed_version, get_stack_name(self.cfn_stack_prefix, self.name), self.name)

        self._udp_receipt_token: str | None
        self._deployed_network_handlers, self._udp_receipt_token = self._get_network_handlers(stack)

        if self._udp_receipt_token is not None:
            for deployed in self._deployed_network_handlers.values():
                register_udp_receipt_token(deployed.ec2.dns_name, self._udp_receipt_token)

    def _describe_stack(self) -> "StackTypeDef":
        """Describe this account's CATS stack through the test role."""
        stack_name = get_stack_name(self.cfn_stack_prefix, self.name)
        cfn = self.role.default.client("cloudformation")
        try:
            response = cfn.describe_stacks(StackName=stack_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ValidationError":
                raise CatsInfrastructureError(
                    f"CATS stack '{stack_name}' not found for target account '{self.name}' (defined in cats.yaml). "
                    + f"Deploy infrastructure first: cats infra deploy {self.name}"
                ) from e
            raise
        return response["Stacks"][0]

    def _get_network_handlers(self, stack: "StackTypeDef") -> tuple[dict[str, DeployedTargetResources], str | None]:
        """Read deployed resources and the UDP receipt token from CloudFormation stack outputs."""
        outputs = stack.get("Outputs", [])
        output_map = {o["OutputKey"]: o["OutputValue"] for o in outputs}

        handlers = _parse_cfn_outputs_to_network_targets(
            output_map=output_map,
            network_target_names=self._network_targets,
            account_name=self.name,
            stack_name=get_stack_name(self.cfn_stack_prefix, self.name),
            response_bucket=output_map["ResponseBucketName"],
        )
        return handlers, output_map.get("UdpReceiptToken")

    def __getattr__(self, attr: str) -> NetworkHandler:
        """Look up deployed CFN resources for a network target.
        Return a NetworkHandler that allows interaction with these resources.

        Only ever reached for a missing attribute. On an instance that has not finished
        ``__init__`` (copy, pickle, a mock spec, a debugger after a failed construction) the
        plumbing itself is missing, and looking it up here would recurse; a target name can
        never start with ``_``, so those lookups are plain AttributeErrors.
        """
        if attr.startswith("_") or "_deployed_network_handlers" not in self.__dict__:
            raise AttributeError(attr)
        try:
            deployed = self._deployed_network_handlers[attr]
        except KeyError as e:
            raise AttributeError(
                f"Network target '{attr}' not found for account '{self.name}'. Available targets: {self.available_network_targets}"
            ) from e
        target_config = self._account_config.account_network_under_test[attr]
        return NetworkHandler(
            host_account=self,
            deployed=deployed,
            target_config=target_config,
        )

    @property
    def udp_receipt_token(self) -> str | None:
        """Token this account's UDP listeners require as a '{token}:' message prefix.

        udp_call prepends it automatically; it is exposed for power users crafting raw
        traffic (e.g. via shell()). None for stacks deployed before UDP receipt tokening.
        """
        return self._udp_receipt_token

    @property
    def available_network_targets(self) -> list[str]:
        """Network target names available for this account."""
        return list(self._deployed_network_handlers.keys())

    @property
    def network_handlers(self) -> list[NetworkHandler]:
        """Return all network handlers for this account."""
        return [getattr(self, name) for name in self._deployed_network_handlers]

    def network_handlers_by_tag(self, tag_name: str, tag_value: str | None = None) -> Iterator[NetworkHandler]:
        """Iterate over network handlers that have a specific tag.

        Args:
            tag_name: Tag key to filter by
            tag_value: Optional tag value to match. If None, matches any value (tag presence only).
        """
        for handler in self.network_handlers:
            if tag_name in handler.tags:
                if tag_value is None or handler.tags[tag_name] == tag_value:
                    yield handler

    def get_network_handler_by_tag(self, tag_name: str, tag_value: str | None = None) -> NetworkHandler:
        """Get exactly one network handler matching a tag, or raise.

        Args:
            tag_name: Tag key to filter by
            tag_value: Optional tag value to match. If None, matches any value (tag presence only).

        Raises:
            CatsUsageError: If zero or multiple handlers match
        """
        matches = list(self.network_handlers_by_tag(tag_name, tag_value))
        if len(matches) == 0:
            raise CatsUsageError(f"No network handler found with tag '{tag_name}'")
        if len(matches) > 1:
            names = [h.name for h in matches]
            raise CatsUsageError(f"Multiple network handlers found with tag '{tag_name}': {names}")
        return matches[0]

    @property
    def role(self) -> SessionHandler:
        """
        Allows access to additional defined roles in cats.yaml by defined attribute name

        Returns:
            SessionHandler: A class that creates an AWS session based on attribute name defined in cats.yaml
        """
        return SessionHandler(
            target_account_id=self.account_id,
            target_account_name=self.name,
            resource_prefix=self._resource_prefix,
            additional_roles=self._account_config.account_settings.additional_roles,
            aws_region=self.aws_region,
            partition=self.partition,
            sessions=self._sessions,
        )

    @property
    def session(self) -> Session:
        """
        Returns the default CATS testing session: the account's default test role, assumed once and
        shared by every probe and client on this account. Its credentials refresh themselves, so a
        session held for a whole test run stays valid.

        Convenience method for AccountUnderTest.role.default

        Returns:
            boto3.Session instantiation
        """
        return self.role.default

    @property
    def tags(self) -> dict[str, str | None]:
        """Tags defined for this account in config."""
        return self._account_config.tags
