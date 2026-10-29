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

"""Tests for cats.report_writers.securityhub.asff_timestamp."""

from datetime import UTC, datetime

from cats.report_writers.securityhub import asff_timestamp


def test_asff_timestamp_has_millisecond_precision_and_a_literal_z() -> None:
    # act
    result = asff_timestamp(datetime(2026, 9, 24, 10, 53, 40, 120456, tzinfo=UTC))

    # assert
    expected = "2026-09-24T10:53:40.120Z"
    assert expected == result
