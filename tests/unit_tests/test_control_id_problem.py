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

"""Tests for cats.hookspecs.control_id_problem: the one control-id rule the decorator and the registry apply."""

import pytest

from cats.hookspecs import control_id_problem

OSCAL_TOKEN_RULE = (
    "is not an OSCAL token, which reporting.report_formats oscal_ar needs for a finding target-id: it must start with "
    + "a letter or '_' and contain only letters, digits, '.', '-', '_'. Spaces and parentheses are not allowed - "
    + "use the catalog convention (e.g. 'ac-2.1' for AC-2(1))"
)


@pytest.mark.parametrize("control_id", ["AC-3", "sc-7.5", "_internal-1", "AC-2(1)", "1.1.1", "SC-7 (5)"])
def test_any_non_empty_string_is_accepted_without_oscal_output(control_id: str) -> None:
    # act
    result = control_id_problem(control_id, oscal=False)

    # assert
    assert result is None


@pytest.mark.parametrize("control_id", ["AC-3", "sc-7.5", "_internal-1"])
def test_token_is_accepted_with_oscal_output(control_id: str) -> None:
    # act
    result = control_id_problem(control_id, oscal=True)

    # assert
    assert result is None


@pytest.mark.parametrize("control_id", ["AC-2(1)", "1.1.1", "SC-7 (5)"])
def test_non_token_is_refused_with_oscal_output(control_id: str) -> None:
    # act
    result = control_id_problem(control_id, oscal=True)

    # assert
    assert result == OSCAL_TOKEN_RULE


@pytest.mark.parametrize("control_id", ["", "   ", 7, None])
def test_empty_or_non_string_is_refused_in_every_format(control_id: object) -> None:
    # act
    result = control_id_problem(control_id, oscal=False)

    # assert
    assert result == "must be a non-empty string"
