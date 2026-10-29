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

"""Tests for cats.report_run.rfc3339_timestamp: one shape for every CATS-owned timestamp."""

from datetime import UTC, datetime, timedelta, timezone

from cats.report_run import rfc3339_timestamp


def test_microseconds_are_kept() -> None:
    # arrange
    moment = datetime(2026, 9, 28, 10, 39, 23, 549000, tzinfo=UTC)

    # act
    text = rfc3339_timestamp(moment)

    # assert
    assert text == "2026-09-28T10:39:23.549000Z"


def test_zero_microsecond_keeps_the_same_width() -> None:
    """isoformat() would drop the fraction here and change the shape mid-run."""
    # arrange
    moment = datetime(2026, 9, 28, 10, 39, 23, 0, tzinfo=UTC)

    # act
    text = rfc3339_timestamp(moment)

    # assert
    assert text == "2026-09-28T10:39:23.000000Z"


def test_other_offsets_are_converted_to_utc() -> None:
    # arrange
    moment = datetime(2026, 9, 28, 11, 39, 23, 1, tzinfo=timezone(timedelta(hours=1)))

    # act
    text = rfc3339_timestamp(moment)

    # assert
    assert text == "2026-09-28T10:39:23.000001Z"


def test_round_trips_through_fromisoformat() -> None:
    # arrange
    moment = datetime(2026, 9, 28, 10, 39, 23, 549000, tzinfo=UTC)

    # act
    parsed = datetime.fromisoformat(rfc3339_timestamp(moment))

    # assert
    assert parsed == moment
