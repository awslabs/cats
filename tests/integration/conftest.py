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

"""Integration test fixtures."""

from typing import Any

import pytest

from cats.user_agent import CatsTestPack
from cats.version import __version__
from tests.integration.constants import (
    ACCOUNT_ID,
    AMI_OVERRIDE_SSM_PARAM,
    CROSS_ACCOUNT_DEPLOY_ROLE,
    CROSS_ACCOUNT_ID,
    CROSS_SECURITYHUB_REGION,
    CROSS_SUBNET_SSM_PARAM,
    CROSS_VPC_SSM_PARAM,
    ENDPOINT_TEST_SG_SSM_PARAM,
    ENDPOINT_TEST_SUBNET_SSM_PARAM,
    ENDPOINT_TEST_VPC_SSM_PARAM,
    HANDLER_SG_SSM_PARAM,
    ISOLATED_SUBNET_SSM_PARAM,
    REGION,
    SUBNET_SSM_PARAM,
    VPC_SSM_PARAM,
    stack_prefix,
)
from tests.utils import make_config

# The integration suite is itself a test pack: every real AWS call it makes carries this token
# next to the CATS one, and test_minimal.py reads it back off a real request.
INTEGRATION_TEST_PACK = CatsTestPack(name="cats-integration-tests", version=__version__)


def pytest_cats_test_pack() -> CatsTestPack:
    return INTEGRATION_TEST_PACK


# --- Same-account configs ---


@pytest.fixture(scope="module")
def minimal_config() -> dict[str, Any]:
    """Config with no network targets."""
    return make_config(
        account_name="minimal",
        account_id=ACCOUNT_ID,
        network_targets={},
        cfn_stack_prefix=stack_prefix("min"),
        aws_region=REGION,
        parent_account_id=ACCOUNT_ID,
        allow_deploy_to_current_account=True,
    )


@pytest.fixture(scope="module")
def network_config() -> dict[str, Any]:
    """Config with network target and AMI override using SSM parameter paths.

    `allow_shell: true` lets this one stack also cover the shell() tests; it only widens the
    test role's ssm:SendCommand resources to include AWS-RunShellScript.
    """
    config = make_config(
        account_name="ec2handler",
        account_id=ACCOUNT_ID,
        network_targets={"private": {"vpc_id": VPC_SSM_PARAM, "subnet_ids": [SUBNET_SSM_PARAM]}},
        cfn_stack_prefix=stack_prefix("ec2"),
        aws_region=REGION,
        parent_account_id=ACCOUNT_ID,
        allow_deploy_to_current_account=True,
        enabled_network_handlers=["ec2"],
        account_settings={"allow_shell": True},
    )
    # Exercises SSM path resolution for the AMI: existing_ec2_ami_id accepts an SSM parameter
    # path, resolved to a concrete image at deploy time.
    config["global_network_settings"]["existing_ec2_ami_id"] = AMI_OVERRIDE_SSM_PARAM
    # Two ports per protocol: the listener receives its port list as one comma-joined argument
    # through the systemd unit's env file, and only a list with a separator in it exercises that
    # path. test_network.py probes the second port of each.
    config["global_network_settings"]["tcp_listen_ports"] = [8080, 8081]
    config["global_network_settings"]["udp_listen_ports"] = [5000, 5001]
    return config


@pytest.fixture(scope="module")
def lambda_config() -> dict[str, Any]:
    """Config with per-target handler overrides: global both, ec2target EC2-only, lambdatarget Lambda-only.

    lambdatarget uses the pre-provisioned handler security group so destroy does not wait on
    Lambda's Hyperplane ENI release. ec2target keeps CATS's own group, but has no lambdas to
    attach it to: the stack-owned LambdaSG branch is deliberately never integ-deployed, because
    its destroy pays the ~20 minute ENI-release wait and would become this job's long pole - it
    is covered by test_lambda_target_without_existing_security_groups_owns_lambda_and_ec2_groups
    in tests/unit_tests/test_cfn_render.py instead.

    sharedtarget is a second EC2-only listener in the handler security group. That group admits
    TCP 9999 from itself with nothing listening, so a Lambda probe to sharedtarget:9999 gets an
    RST (REFUSED) where the same probe to ec2target:9999 is dropped by CATS's scoped group
    (TIMEOUT). A Lambda has no self-call path, so this is the only way to arrange REFUSED for it.
    """
    return {
        "cats_deployment_settings": {
            "allow_deploy_to_current_account": True,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("lam"),
        },
        "global_network_settings": {
            "enabled_network_handlers": ["ec2", "lambda"],
            "tcp_listen_ports": [8080],
            "udp_listen_ports": [5000],
        },
        "accounts_under_test": {
            "lambdahandler": {
                "account_id": ACCOUNT_ID,
                "account_network_under_test": {
                    "ec2target": {
                        "vpc_id": VPC_SSM_PARAM,
                        "subnet_ids": [SUBNET_SSM_PARAM],
                        "network_settings": {
                            "enabled_network_handlers": ["ec2"],
                        },
                    },
                    "sharedtarget": {
                        "vpc_id": VPC_SSM_PARAM,
                        "subnet_ids": [SUBNET_SSM_PARAM],
                        "existing_security_group_ids": [HANDLER_SG_SSM_PARAM],
                        "network_settings": {
                            "enabled_network_handlers": ["ec2"],
                        },
                    },
                    "lambdatarget": {
                        "vpc_id": VPC_SSM_PARAM,
                        "subnet_ids": [SUBNET_SSM_PARAM],
                        "existing_security_group_ids": [HANDLER_SG_SSM_PARAM],
                        "network_settings": {
                            "enabled_network_handlers": ["lambda"],
                        },
                    },
                },
            },
        },
    }


@pytest.fixture(scope="module")
def isolated_config() -> dict[str, Any]:
    """An isolated subnet with no route to the SSM endpoints.

    `isolated` is in the base stack's isolated subnet, whose network ACL drops TCP 443 to the endpoint
    subnet, so its listener never registers with SSM, like a data-tier listener behind an
    empty route table. Both handlers are enabled, as an isolated tier needs: the EC2 listener is the target,
    Lambda is the probe from there, and plain `*_call` dispatches to Lambda because it is listed first.
    Both targets share the pre-provisioned handler security group so 8080 is open between them and
    destroy does not wait on a stack-owned Lambda group's ENI release (see lambda_config).
    """
    return {
        "cats_deployment_settings": {
            "allow_deploy_to_current_account": True,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("iso"),
        },
        "global_network_settings": {
            "enabled_network_handlers": ["lambda", "ec2"],
            "tcp_listen_ports": [8080],
        },
        "accounts_under_test": {
            "isolatedsubnet": {
                "account_id": ACCOUNT_ID,
                "account_network_under_test": {
                    "routed": {
                        "vpc_id": VPC_SSM_PARAM,
                        "subnet_ids": [SUBNET_SSM_PARAM],
                        "existing_security_group_ids": [HANDLER_SG_SSM_PARAM],
                    },
                    "isolated": {
                        "vpc_id": VPC_SSM_PARAM,
                        "subnet_ids": [ISOLATED_SUBNET_SSM_PARAM],
                        "existing_security_group_ids": [HANDLER_SG_SSM_PARAM],
                    },
                },
            },
        },
    }


@pytest.fixture(scope="module")
def max_size_config() -> dict[str, Any]:
    """Config with 3 targets using both ec2+lambda - the densest resource shape CATS deploys.

    All three targets share the pre-provisioned handler security group, which drops the
    rendered template roughly 1,700 bytes below what it was when each target owned a group.
    So this deploy no longer sits near the 51,200-byte CloudFormation limit: that ceiling is
    guarded by the unit test test_template_with_3_targets_under_51kb_limit, and what this
    fixture proves is that the densest shape actually deploys and destroys in AWS.
    Sharing one group also lets Lambda reuse its Hyperplane ENIs across runs.
    """
    return {
        "cats_deployment_settings": {
            "allow_deploy_to_current_account": True,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("max"),
        },
        "global_network_settings": {
            "enabled_network_handlers": ["ec2", "lambda"],
            "tcp_listen_ports": [8080],
        },
        "accounts_under_test": {
            "maxsize": {
                "account_id": ACCOUNT_ID,
                "account_network_under_test": {
                    f"target{index}": {
                        "vpc_id": VPC_SSM_PARAM,
                        "subnet_ids": [SUBNET_SSM_PARAM],
                        "existing_security_group_ids": [HANDLER_SG_SSM_PARAM],
                    }
                    for index in range(3)
                },
            },
        },
    }


@pytest.fixture(scope="module")
def parallel_config() -> dict[str, Any]:
    """Config with multiple accounts for parallel deploy."""
    return make_config(
        accounts={"parallel_a": ACCOUNT_ID, "parallel_b": ACCOUNT_ID},
        network_targets={},
        cfn_stack_prefix=stack_prefix("par"),
        aws_region=REGION,
        parent_account_id=ACCOUNT_ID,
        allow_deploy_to_current_account=True,
    )


@pytest.fixture(scope="module")
def cats_endpoints_config() -> dict[str, Any]:
    """CATS creates the VPC endpoints itself: the EC2 handler's SSM suite and S3, in a VPC with no route anywhere (test_cats_endpoints.py).

    Every other config here points CATS at the base stack's own endpoints, so none of them renders an
    AWS::EC2::VPCEndpoint. This one does, in the VPC the module creates per run (private DNS allows one
    interface endpoint per service per VPC, so concurrent pipelines cannot share one). The VPC, subnet
    and the endpoints' security group are all read by SSM parameter name, the way a user names their own network,
    which makes the group on the deployed endpoint the proof that `interface_endpoint_security_group_ids`
    resolves a parameter. The override is not optional in that VPC: the default group admits only its
    own members, and the listener is in CATS's own group. `allow_shell: true` is for the anonymous S3
    request the endpoint policy must deny.
    """
    config = make_config(
        account_name="catsendpoints",
        account_id=ACCOUNT_ID,
        network_targets={"private": {"vpc_id": ENDPOINT_TEST_VPC_SSM_PARAM, "subnet_ids": [ENDPOINT_TEST_SUBNET_SSM_PARAM]}},
        enabled_network_handlers=["ec2"],
        gateway_endpoints=["S3"],
        interface_endpoints=["ssm", "ssmmessages", "ec2messages"],
        interface_endpoint_security_group_ids=[ENDPOINT_TEST_SG_SSM_PARAM],
        cfn_stack_prefix=stack_prefix("vpce"),
        aws_region=REGION,
        parent_account_id=ACCOUNT_ID,
        allow_deploy_to_current_account=True,
        account_settings={"allow_shell": True},
    )
    config["global_network_settings"]["existing_ec2_ami_id"] = AMI_OVERRIDE_SSM_PARAM
    return config


# --- Cross-account configs ---


@pytest.fixture(scope="module")
def cross_account_minimal_config() -> dict[str, Any]:
    """Cross-account config with no network targets."""
    return {
        "cats_deployment_settings": {
            "deploy_role_name": CROSS_ACCOUNT_DEPLOY_ROLE,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("xamin"),
        },
        "accounts_under_test": {
            "xacct_minimal": {
                "account_id": CROSS_ACCOUNT_ID,
                "account_network_under_test": {},
            },
        },
    }


@pytest.fixture(scope="module")
def cross_account_network_config() -> dict[str, Any]:
    """Cross-account config with one network target in the cross base stack's VPC.

    The VPC and subnet are SSM parameter names that CATS resolves in the cross account at deploy
    time, so this deploy also proves parameter resolution runs in the target account rather than
    the parent. Both base stacks are peered, but nothing here crosses it: these tests probe the
    cross account's listener from itself. test_cross_account_networking.py crosses the peering.
    """
    return {
        "cats_deployment_settings": {
            "deploy_role_name": CROSS_ACCOUNT_DEPLOY_ROLE,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("xanet"),
        },
        "global_network_settings": {
            "enabled_network_handlers": ["ec2"],
            "tcp_listen_ports": [8080],
        },
        "accounts_under_test": {
            "xacct_network": {
                "account_id": CROSS_ACCOUNT_ID,
                "account_network_under_test": {
                    "private": {
                        "vpc_id": CROSS_VPC_SSM_PARAM,
                        "subnet_ids": [CROSS_SUBNET_SSM_PARAM],
                    },
                },
            },
        },
    }


@pytest.fixture(scope="module")
def cross_account_parallel_config() -> dict[str, Any]:
    """Cross-account config with multiple parallel stacks."""
    num_stacks = 20
    return {
        "cats_deployment_settings": {
            "deploy_role_name": CROSS_ACCOUNT_DEPLOY_ROLE,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("xapar"),
        },
        "accounts_under_test": {
            f"parallel_{i}": {
                "account_id": CROSS_ACCOUNT_ID,
                "account_network_under_test": {},
            }
            for i in range(num_stacks)
        },
    }


@pytest.fixture(scope="module")
def cross_account_networking_config() -> dict[str, Any]:
    """EC2 listeners in both base VPCs, home and cross, for probes that cross the peering between them.

    Each listener is in CATS's own security group, which admits the configured ports from anywhere,
    so what decides a probe is the peering: the route each base stack has to the other's CIDR
    (infra/integ-test-vpc.yaml, infra/integ-test-cross-vpc.yaml), and the DNS resolution option
    deploy_base_stack.py sets on both sides so each listener's private name resolves from the other VPC.
    """
    return {
        "cats_deployment_settings": {
            "allow_deploy_to_current_account": True,
            "deploy_role_name": CROSS_ACCOUNT_DEPLOY_ROLE,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("xnet"),
        },
        "global_network_settings": {
            "enabled_network_handlers": ["ec2"],
            "tcp_listen_ports": [8080],
            "udp_listen_ports": [5000],
        },
        "accounts_under_test": {
            "home": {
                "account_id": ACCOUNT_ID,
                "account_network_under_test": {
                    "subnet": {
                        "vpc_id": VPC_SSM_PARAM,
                        "subnet_ids": [SUBNET_SSM_PARAM],
                    },
                },
            },
            "foreign": {
                "account_id": CROSS_ACCOUNT_ID,
                "account_network_under_test": {
                    "subnet": {
                        "vpc_id": CROSS_VPC_SSM_PARAM,
                        "subnet_ids": [CROSS_SUBNET_SSM_PARAM],
                    },
                },
            },
        },
    }


@pytest.fixture(scope="module")
def securityhub_config() -> dict[str, Any]:
    """Config with no network targets and the securityhub report format.

    Deploying it puts the securityhub:BatchImportFindings grant on the test role, which is what
    the import in test_securityhub.py runs through. Security Hub itself is enabled in the account
    by the base stack (integ-test-vpc.yaml), with no standards subscribed.
    """
    config = make_config(
        account_name="sechub",
        account_id=ACCOUNT_ID,
        network_targets={},
        cfn_stack_prefix=stack_prefix("sh"),
        aws_region=REGION,
        parent_account_id=ACCOUNT_ID,
        allow_deploy_to_current_account=True,
    )
    config["reporting"] = {"report_formats": ["cats_json", "securityhub"], "securityhub": {"aws_region": REGION}}
    return config


@pytest.fixture(scope="module")
def cross_account_securityhub_config() -> dict[str, Any]:
    """Cross-account config, no network targets, securityhub reporting into a Region other than the deploy Region.

    The cross account's Security Hub is enabled in CROSS_SECURITYHUB_REGION (by its own organization's
    tooling), not in REGION where the stack deploys, so the format's ``region`` setting carries the
    import there: the test role's grant and the importing client both follow it.
    """
    return {
        "cats_deployment_settings": {
            "deploy_role_name": CROSS_ACCOUNT_DEPLOY_ROLE,
            "parent_account_id": ACCOUNT_ID,
            "aws_region": REGION,
            "cfn_stack_prefix": stack_prefix("xash"),
        },
        "accounts_under_test": {
            "xacct_sechub": {
                "account_id": CROSS_ACCOUNT_ID,
                "account_network_under_test": {},
            },
        },
        "reporting": {"report_formats": ["cats_json", "securityhub"], "securityhub": {"aws_region": CROSS_SECURITYHUB_REGION}},
    }
