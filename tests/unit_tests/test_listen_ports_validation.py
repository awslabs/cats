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

"""Tests for listen port validation: range, uniqueness, and the combined per-target cap.

Each configured listen port renders two ingress rules (IPv4 and IPv6) on the CATS EC2
listener security group, so misconfiguration must fail at validation time rather than
mid-deploy.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from cats.config import MAX_COMBINED_LISTEN_PORTS, CatsConfig
from tests.utils import make_config


def make_ports_config(
    tcp_listen_ports: list[int] | None = None,
    udp_listen_ports: list[int] | None = None,
    target_network_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Config dict with one network target and the given global listen port lists."""
    target: dict[str, Any] = {"vpc_id": "vpc-123", "subnet_ids": ["subnet-123"]}
    if target_network_settings is not None:
        target["network_settings"] = target_network_settings
    config = make_config(network_targets={"subnet": target})
    if tcp_listen_ports is not None:
        config["global_network_settings"]["tcp_listen_ports"] = tcp_listen_ports
    if udp_listen_ports is not None:
        config["global_network_settings"]["udp_listen_ports"] = udp_listen_ports
    return config


def test_tcp_port_zero_rejected() -> None:
    """Port 0 is below the valid port range."""
    # arrange
    config = make_ports_config(tcp_listen_ports=[0])

    # act & assert
    with pytest.raises(ValidationError, match=r"Input should be greater than or equal to 1"):
        CatsConfig.model_validate(config)


def test_udp_port_above_65535_rejected() -> None:
    """Port 65536 is above the valid port range."""
    # arrange
    config = make_ports_config(udp_listen_ports=[65536])

    # act & assert
    with pytest.raises(ValidationError, match=r"Input should be less than or equal to 65535"):
        CatsConfig.model_validate(config)


def test_boundary_ports_accepted() -> None:
    """Ports 1 and 65535 are the valid range boundaries."""
    # arrange
    config = make_ports_config(tcp_listen_ports=[1, 65535], udp_listen_ports=[1, 65535])

    # act
    result = CatsConfig.model_validate(config)

    # assert
    expected = [1, 65535]
    assert result.global_network_settings.tcp_listen_ports == expected


def test_duplicate_tcp_ports_rejected() -> None:
    """Duplicate ports in one list would render duplicate security group rules."""
    # arrange
    config = make_ports_config(tcp_listen_ports=[8080, 8080])

    # act & assert
    with pytest.raises(ValidationError, match=r"List must be unique"):
        CatsConfig.model_validate(config)


def test_duplicate_udp_ports_rejected() -> None:
    """Duplicate ports in one list would render duplicate security group rules."""
    # arrange
    config = make_ports_config(udp_listen_ports=[5000, 5000])

    # act & assert
    with pytest.raises(ValidationError, match=r"List must be unique"):
        CatsConfig.model_validate(config)


def test_same_port_on_tcp_and_udp_accepted() -> None:
    """The same port across protocols renders two distinct security group rules."""
    # arrange
    config = make_ports_config(tcp_listen_ports=[8080], udp_listen_ports=[8080])

    # act
    result = CatsConfig.model_validate(config)

    # assert
    expected = [8080]
    assert result.global_network_settings.udp_listen_ports == expected


def test_combined_ports_at_cap_accepted() -> None:
    """Exactly MAX_COMBINED_LISTEN_PORTS combined ports is valid."""
    # arrange
    config = make_ports_config(
        tcp_listen_ports=list(range(8000, 8015)),
        udp_listen_ports=list(range(9000, 9015)),
    )

    # act
    result = CatsConfig.model_validate(config)

    # assert
    settings = result.accounts_under_test["testaccount"].account_network_under_test["subnet"].network_settings
    assert len(settings.tcp_listen_ports) + len(settings.udp_listen_ports) == MAX_COMBINED_LISTEN_PORTS


def test_combined_ports_above_cap_rejected() -> None:
    """One port over the combined cap fails with the security group quota explanation."""
    # arrange
    config = make_ports_config(
        tcp_listen_ports=list(range(8000, 8016)),
        udp_listen_ports=list(range(9000, 9015)),
    )
    expected_message = (
        r"Network target 'subnet' in account 'testaccount' resolves to 31 listen ports "
        + r"\(tcp_listen_ports \+ udp_listen_ports\); the maximum is 30\. "
        + r"Each port becomes two security group ingress rules \(IPv4 and IPv6\) and the default AWS quota "
        + r"is 60 inbound rules per security group\."
    )

    # act & assert
    with pytest.raises(ValidationError, match=expected_message):
        CatsConfig.model_validate(config)


def test_combined_cap_applies_after_hierarchy_resolution() -> None:
    """The cap counts the resolved target lists even when tcp and udp arrive from different levels."""
    # arrange - 20 tcp ports inherited from global, 20 udp ports set on the target
    config = make_ports_config(
        tcp_listen_ports=list(range(8000, 8020)),
        udp_listen_ports=[],
        target_network_settings={"udp_listen_ports": list(range(9000, 9020))},
    )
    expected_message = r"Network target 'subnet' in account 'testaccount' resolves to 40 listen ports"

    # act & assert
    with pytest.raises(ValidationError, match=expected_message):
        CatsConfig.model_validate(config)
