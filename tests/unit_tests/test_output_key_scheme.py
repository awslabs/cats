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

"""The CloudFormation output-key scheme is a covered surface with no separator between key and target name.

The scheme stays collision-free only while two rules hold: a per-target key may not extend another
(an `Ec2DnsNameV6` key would render the same output as `Ec2DnsName` for a target named `V6...`), and
a base key may not start with a per-target key (an `EnabledHandlersAll` base output would collide
with target `All`). The parser reads f"{key}{target}" for each configured target, so under these
rules it can never mis-assign.
"""

import yaml

from cats.cfn.render import render_template
from cats.cfn.resolve import ResolvedAccount, ResolvedAccountSettings, ResolvedNetworkTarget, ResolvedSubnet
from cats.config import CatsDeploymentSettings, EnabledNetworkHandler, NetworkSettings
from cats.constants import BASE_OUTPUT_KEYS, PER_TARGET_OUTPUT_KEYS


def test_no_per_target_key_extends_another() -> None:
    """f"{key}{target}" has no separator: a key that is another key plus letters collides with some user's target name."""
    # act
    extending = sorted(
        (longer, shorter)
        for longer in PER_TARGET_OUTPUT_KEYS
        for shorter in PER_TARGET_OUTPUT_KEYS
        if longer != shorter and longer.startswith(shorter)
    )

    # assert
    assert extending == []


def test_no_base_key_starts_with_a_per_target_key() -> None:
    """A base output shaped f"{key}{ValidTargetName}" would be read as that target's output."""
    # act
    shadowing = sorted((base, key) for base in BASE_OUTPUT_KEYS for key in PER_TARGET_OUTPUT_KEYS if base.startswith(key))

    # assert
    assert shadowing == []


def test_rendered_outputs_are_exactly_the_scheme() -> None:
    """The template and the constants the parser reads from are one scheme: three base keys, six per target, no others."""
    # arrange
    settings = NetworkSettings(enabled_network_handlers=[EnabledNetworkHandler.EC2, EnabledNetworkHandler.LAMBDA])
    target = ResolvedNetworkTarget(
        target_name="private",
        vpc_id="vpc-123",
        ami_id="ami-0123456789abcdef0",
        subnets=[ResolvedSubnet(subnet_id="subnet-aaa", availability_zone="us-east-1a", route_table_id="rtb-aaa")],
        existing_security_group_ids=None,
        interface_endpoint_security_group_ids=None,
        settings=settings,
    )
    resolved = ResolvedAccount(account_id="111111111111", account_name="prod", network_targets={"private": target})
    account_settings = ResolvedAccountSettings(
        additional_roles={}, default_test_role_managed_policies=[], default_test_role_inline_policies={}, fixture_scope="session", allow_shell=False
    )

    # act
    template = yaml.safe_load(
        render_template(
            resolved, CatsDeploymentSettings(parent_account_id="999999999999", aws_region="us-east-1"), account_settings, securityhub_region=None
        )
    )

    # assert
    expected = sorted([*BASE_OUTPUT_KEYS, *(f"{key}private" for key in PER_TARGET_OUTPUT_KEYS)])
    assert sorted(template["Outputs"]) == expected
