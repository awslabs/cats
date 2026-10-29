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

"""Tests for cats.report_run.resolve_report_dir: --cats-report-dir > reporting.report_dir > CWD."""

from pathlib import Path

from cats.report_run import resolve_report_dir

CONFIG_PATH = Path("/project/config/cats.yaml")


def test_nothing_set_is_the_current_directory() -> None:
    # act
    result = resolve_report_dir(cli_report_dir=None, configured_report_dir=None, config_path=CONFIG_PATH)

    # assert
    expected = Path(".")
    assert expected == result


def test_relative_config_value_resolves_against_the_cats_yaml_directory() -> None:
    """Like inline_policies paths: independent of where pytest is invoked."""
    # act
    result = resolve_report_dir(cli_report_dir=None, configured_report_dir="reports", config_path=CONFIG_PATH)

    # assert
    expected = Path("/project/config/reports")
    assert expected == result


def test_absolute_config_value_is_used_as_is() -> None:
    # act
    result = resolve_report_dir(cli_report_dir=None, configured_report_dir="/evidence/cats", config_path=CONFIG_PATH)

    # assert
    expected = Path("/evidence/cats")
    assert expected == result


def test_cli_value_beats_config_value() -> None:
    # act
    result = resolve_report_dir(cli_report_dir="out", configured_report_dir="reports", config_path=CONFIG_PATH)

    # assert
    expected = Path("out")
    assert expected == result


def test_relative_cli_value_stays_relative_to_the_current_directory() -> None:
    """A command-line path means what it means on every other CLI: relative to where you ran it."""
    # act
    result = resolve_report_dir(cli_report_dir="out/nested", configured_report_dir=None, config_path=CONFIG_PATH)

    # assert
    expected = Path("out/nested")
    assert expected == result
