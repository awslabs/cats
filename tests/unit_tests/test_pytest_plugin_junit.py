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

"""With junit in reporting.report_formats, _configure_junit_xml switches on pytest's junitxml
plugin at configure time and _record_junit_properties attaches CATS data to each
testcase. This exercises the whole chain through a real pytest run.
"""

import xml.etree.ElementTree as ET

import pytest
import yaml

from tests.utils import make_config


def test_junit_xml_carries_cats_properties(pytester: pytest.Pytester) -> None:
    """CATS's own properties carry the cats: prefix; user markers and metadata keep their bare names (the next test)."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"report_formats": ["cats_json", "junit"]}
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile("""
import pytest

@pytest.mark.cats_standard(name="SOC2-CC6.6")
def test_with_standard():
    \"\"\"Proves the control holds.\"\"\"
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    testcase = ET.parse(pytester.path / "junit.xml").getroot().find("./testsuite/testcase")  # noqa: S314 - XML written by this test's own pytest run
    assert testcase is not None
    properties = {prop.get("name"): prop.get("value") for prop in testcase.iter("property")}
    expected = {"cats:standard": "SOC2-CC6.6", "cats:description": "Proves the control holds."}
    assert expected == properties


def test_junit_list_markers_and_metadata_are_one_property_per_value(pytester: pytest.Pytester) -> None:
    """A list-valued marker is one property per element and non-string metadata is JSON, never a Python repr."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"}, reporting={"report_formats": ["cats_json", "junit"], "custom_markers": ["ticket"]})
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile("""
from cats import cats


@cats(ticket=["SEC-1", "SEC-2"])
def test_with_list_marker(cats_report):
    cats_report.add_report_metadata({"resources_checked": 42, "regions": ["us-east-1", "eu-west-1"], "flags": {"a": 1}})
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    testcase = ET.parse(pytester.path / "junit.xml").getroot().find("./testsuite/testcase")  # noqa: S314 - XML written by this test's own pytest run
    assert testcase is not None
    properties = [(prop.get("name"), prop.get("value")) for prop in testcase.iter("property")]
    expected = [
        ("ticket", "SEC-1"),
        ("ticket", "SEC-2"),
        ("resources_checked", "42"),
        ("regions", "us-east-1"),
        ("regions", "eu-west-1"),
        ("flags", '{"a": 1}'),
    ]
    assert expected == properties


def test_user_supplied_junit_xml_keeps_host_junit_family(pytester: pytest.Pytester) -> None:
    """An explicit --junit-xml is the host project's junit output: CATS leaves its junit_family alone and writes to the user's path."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"}, reporting={"report_formats": ["cats_json", "junit"]})
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config), encoding="utf-8")
    pytester.makeini("""
[pytest]
junit_family = xunit2
""")
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile("""
def test_family(pytestconfig):
    assert pytestconfig.getini("junit_family") == "xunit2"
""")

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "--junit-xml", str(pytester.path / "user.xml"))

    # assert
    result.assert_outcomes(passed=1)
    assert (pytester.path / "user.xml").is_file()
    assert (pytester.path / "junit.xml").is_file() is False


def test_cats_chosen_junit_path_forces_xunit1(pytester: pytest.Pytester) -> None:
    """When CATS picks the junit path it needs xunit1 for per-testcase properties, whatever the host configured."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"}, reporting={"report_formats": ["cats_json", "junit"]})
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config), encoding="utf-8")
    pytester.makeini("""
[pytest]
junit_family = xunit2
""")
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile("""
def test_family(pytestconfig):
    assert pytestconfig.getini("junit_family") == "xunit1"
""")

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    assert (pytester.path / "junit.xml").is_file()
