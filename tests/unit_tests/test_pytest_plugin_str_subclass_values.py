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

"""A ``str`` subclass as evidence: a ``StrEnum`` or ``(str, Enum)`` standard name, marker value or metadata value.

``_record_entry`` JSON round-trips each entry as it is recorded, so every writer sees the
member's text. Without that, two writers disagreed with ``report.json``: ``str()`` of a
``(str, Enum)`` member is ``Cls.MEMBER`` on Python 3.11+, which OSCAL and the junit writer
wrote on serial runs; and under pytest-xdist the member reached ``item.user_properties``,
which xdist ships through execnet, which serializes only built-in types, so the first such
test aborted the whole session with an INTERNALERROR and no results were kept.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from tests.utils import make_config

ENUMS = """
from enum import Enum, StrEnum


class Standards(StrEnum):
    SOC2 = "SOC2-CC6.6"


class Evidence(StrEnum):
    BEHAVIORAL = "behavioral"


class LegacyEvidence(str, Enum):  # its str() is "LegacyEvidence.CONFIGURATION", unlike a StrEnum's
    CONFIGURATION = "configuration"
    GROUP = "Governance"
    STANDARD = "ISO-A.9"
"""


def write_project(pytester: pytest.Pytester, test_module: str) -> None:
    """A project with junit, OSCAL and JSON reporting, the ``evidence`` marker declared, and one test module."""
    reporting = {"report_formats": ["cats_json", "junit", "oscal_ar"], "custom_markers": ["evidence"]}
    config = make_config(accounts={"testaccount": "234567890123"}, reporting=reporting)
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile(ENUMS + test_module)


def read_junit_properties(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Each testcase's ``(name, value)`` properties, by test name."""
    testcases = ET.parse(path).getroot().iter("testcase")  # noqa: S314 - XML written by this test's own pytest run
    return {testcase.get("name", ""): [(prop.get("name", ""), prop.get("value", "")) for prop in testcase.iter("property")] for testcase in testcases}


def test_str_subclass_values_survive_xdist_into_junit_properties(pytester: pytest.Pytester) -> None:
    """Every path a str subclass can take into user_properties reaches the controller as plain text."""
    # arrange
    write_project(
        pytester,
        """
import pytest

from cats import cats


@cats(standard_name=Standards.SOC2)
def test_decorator_str_enum_standard_name():
    assert True


@cats(evidence=Evidence.BEHAVIORAL)
def test_decorator_str_enum_marker():
    assert True


@cats(evidence=[LegacyEvidence.CONFIGURATION])
def test_decorator_str_mixin_enum_in_list():
    assert True


@pytest.mark.evidence(kwarg=Evidence.BEHAVIORAL)
def test_direct_marker_and_metadata(cats_report):
    cats_report.add_report_metadata({"probe": Evidence.BEHAVIORAL})
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-n", "2", "-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=4)
    expected = {
        "test_decorator_str_enum_standard_name": [("cats:standard", "SOC2-CC6.6")],
        "test_decorator_str_enum_marker": [("evidence", "behavioral")],
        "test_decorator_str_mixin_enum_in_list": [("evidence", "configuration")],
        "test_direct_marker_and_metadata": [("evidence", "behavioral"), ("probe", "behavioral")],
    }
    assert expected == read_junit_properties(pytester.path / "junit.xml")


SERIAL_TEST_MODULE = """
import pytest

from cats import cats


@cats(standard_name=LegacyEvidence.STANDARD, control_group=LegacyEvidence.GROUP)
@pytest.mark.evidence(kwarg=LegacyEvidence.CONFIGURATION)
def test_str_mixin_everywhere(cats_report):
    cats_report.add_report_metadata({"probe": LegacyEvidence.CONFIGURATION})
    assert True
"""


def test_serial_run_writes_str_mixin_enum_text_to_oscal_props(pytester: pytest.Pytester) -> None:
    """The observation carries each member's value, where str() would have given Cls.MEMBER."""
    # arrange
    write_project(pytester, SERIAL_TEST_MODULE)

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:xdist")

    # assert
    result.assert_outcomes(passed=1)
    results = json.loads((pytester.path / "assessment-results.json").read_text())["assessment-results"]["results"][0]
    expected = {
        "target-ids": ["ISO-A.9"],
        "props": [
            {"name": "result", "value": "Pass"},
            {"name": "control-group", "value": "Governance"},
            {"name": "cats-standard", "value": "ISO-A.9"},
            {"name": "evidence", "value": "configuration"},
            {"name": "probe", "value": "configuration"},
        ],
    }
    assert expected == {
        "target-ids": [finding["target"]["target-id"] for finding in results["findings"]],
        "props": results["observations"][0]["props"],
    }


def test_serial_run_writes_str_mixin_enum_text_to_junit_properties(pytester: pytest.Pytester) -> None:
    """pytest's junitxml plugin str()s each property value, so the member must already be plain text."""
    # arrange
    write_project(pytester, SERIAL_TEST_MODULE)

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:xdist")

    # assert
    result.assert_outcomes(passed=1)
    expected = {"test_str_mixin_everywhere": [("cats:standard", "ISO-A.9"), ("evidence", "configuration"), ("probe", "configuration")]}
    assert expected == read_junit_properties(pytester.path / "junit.xml")
