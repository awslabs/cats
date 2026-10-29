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

"""Example CATS tests: the EC2 listener probing itself, in one account.

The config enables only the default `ec2` handler, so every probe runs on the listener
via SSM and targets the listener's own DNS name. Add `lambda` to
`enabled_network_handlers` to run the same tests from the probe Lambda instead.

Run with: CATS_CONFIG=example_cats_project/cats.yaml uv run cats test example_cats_project
"""

from cats import CatsNetworkResponse, cats
from example_cats_project.cats_types import MyaccountAccount


@cats(standard_name="network-connectivity")
def test_http_to_ec2_listener(myaccount_account: MyaccountAccount) -> None:
    """HTTP call to the EC2 listener: any response is SUCCESS."""
    # arrange
    target = myaccount_account.private.ec2_dns_name

    # act
    result = myaccount_account.private.http_call(host=target, port=8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@cats(standard_name="network-connectivity")
def test_tcp_to_ec2_listener(myaccount_account: MyaccountAccount) -> None:
    """TCP connect to the EC2 listener."""
    # arrange
    target = myaccount_account.private.ec2_dns_name

    # act
    result = myaccount_account.private.tcp_call(host=target, port=8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@cats(standard_name="network-connectivity")
def test_udp_to_ec2_listener(myaccount_account: MyaccountAccount) -> None:
    """UDP datagram to the EC2 listener, arrival proven by the receipt in S3."""
    # arrange
    target = myaccount_account.private.ec2_dns_name

    # act
    sent = myaccount_account.private.udp_call(host=target, port=5000)

    # assert - SENT means the datagram left the sender; receive_s3 proves it arrived
    assert sent == CatsNetworkResponse.SENT
    assert myaccount_account.private.receive_s3(sent) is True
