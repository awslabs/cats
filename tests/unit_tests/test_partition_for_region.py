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

"""Tests for partition_for_region: the partition botocore's endpoint data assigns a Region to."""

import pytest

from cats.config import partition_for_region


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us-east-1", "aws"),
        ("eu-west-1", "aws"),
        ("cn-north-1", "aws-cn"),
        ("us-gov-west-1", "aws-us-gov"),
        ("eusc-de-east-1", "aws-eusc"),
        ("us-iso-east-1", "aws-iso"),
    ],
)
def test_known_region_maps_to_its_partition(region: str, expected: str) -> None:
    # act
    result = partition_for_region(region)

    # assert
    assert expected == result


def test_region_no_partition_claims_returns_none() -> None:
    """A Region prefix botocore has not heard of is nobody's: the caller falls back to aws, as botocore does."""
    # act
    result = partition_for_region("xx-north-1")

    # assert
    assert result is None
