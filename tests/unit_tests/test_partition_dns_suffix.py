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

"""Tests for cats/config.py partition_dns_suffix - a partition's DNS name comes from botocore, not from CATS."""

import pytest

from cats.config import partition_dns_suffix


def test_aws_partition_lives_under_amazonaws_com() -> None:
    # act
    result = partition_dns_suffix("aws")

    # assert
    assert result == "amazonaws.com"


def test_china_partition_lives_under_amazonaws_com_cn() -> None:
    # act
    result = partition_dns_suffix("aws-cn")

    # assert
    assert result == "amazonaws.com.cn"


def test_unknown_partition_raises_key_error() -> None:
    # act & assert - config validation rejects unknown partitions first; this is the contract if it did not
    with pytest.raises(KeyError):
        partition_dns_suffix("bogus")
