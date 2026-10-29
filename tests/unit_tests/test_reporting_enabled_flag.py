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

"""Tests for CatsReportingSettings.enabled gating in pytest_sessionfinish."""

import pytest
import yaml

from tests.utils import make_config


def _config_with_reporting(enabled: bool) -> str:
    """Produce a cats.yaml with reporting.enabled set to the given value."""
    cfg = make_config(accounts={"testaccount": "234567890123"})
    cfg["reporting"] = {"enabled": enabled}
    return yaml.dump(cfg)


def test_reporting_enabled_true_writes_report_json(pytester: pytest.Pytester) -> None:
    """With reporting.enabled=true, report.json is written after the session."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_config_with_reporting(enabled=True))

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile("def test_pass(): assert True")

    # act
    pytester.runpytest_subprocess("-v")

    # assert
    report_path = pytester.path / "report.json"
    assert report_path.exists() is True


def test_reporting_enabled_false_skips_report_json(pytester: pytest.Pytester) -> None:
    """With reporting.enabled=false, no report.json is written."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_config_with_reporting(enabled=False))

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile("def test_pass(): assert True")

    # act
    pytester.runpytest_subprocess("-v")

    # assert
    report_path = pytester.path / "report.json"
    assert report_path.exists() is False
