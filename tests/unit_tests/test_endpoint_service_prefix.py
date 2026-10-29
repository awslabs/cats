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

"""Tests for cats/cfn/render.py _endpoint_service_prefix - the partition's DNS name, labels reversed.

The rendered ServiceName strings for aws-cn and aws-us-gov are covered in test_cfn_render.py.
"""

import pytest

from cats.cfn.render import _endpoint_service_prefix


@pytest.mark.parametrize(
    ("partition", "expected"),
    [
        ("aws", "com.amazonaws"),
        ("aws-us-gov", "com.amazonaws"),
        ("aws-cn", "cn.com.amazonaws"),
    ],
)
def test_prefix_is_the_partition_dns_name_reversed(partition: str, expected: str) -> None:
    # act
    result = _endpoint_service_prefix(partition)

    # assert
    assert result == expected
