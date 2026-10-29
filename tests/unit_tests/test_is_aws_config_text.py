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

"""Tests for cats.utils.aws_profile._is_aws_config_text: the shape guard before a splice."""

import pytest

from cats.utils.aws_profile import _is_aws_config_text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty file"),
        pytest.param("[default]\nregion = us-east-1\n", id="section and key value"),
        pytest.param("[profile work]\nregion=eu-west-2\n", id="compact key value"),
        pytest.param("# comment\n; another\n[default]\n", id="both comment markers"),
        pytest.param("[default]\nca_bundle =\n", id="empty value"),
        pytest.param("[default]\ns3 =\n  max_concurrent_requests = 20\n", id="indented sub key"),
        pytest.param(
            "[sso-session my-sso]\nsso_region = us-east-1\nsso_start_url = https://my.awsapps.com/start\n",
            id="sso-session block (a common parent identity)",
        ),
        pytest.param("[default]\n\n\nregion = us-east-1\n", id="blank lines"),
        pytest.param("[default]\nregion = us-east-1", id="no trailing newline"),
        pytest.param("[services my-services]\ns3 =\n  endpoint_url = https://localhost:9000\n", id="services block"),
        pytest.param("[plugins]\ncli_legacy_plugin_path = /usr/lib\n", id="plugins block"),
        pytest.param("[preview]\ncloudfront = true\n", id="preview block"),
        pytest.param("  [profile indented]  \nregion = us-east-1\n", id="header with surrounding whitespace"),
    ],
)
def test_aws_config_shapes_are_accepted(text: str) -> None:
    # act
    result = _is_aws_config_text(text)

    # assert
    assert result is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("shopping list\n- eggs\n", id="prose"),
        pytest.param("[default]\nregion\n", id="bare word"),
        pytest.param('{"json": true}\n', id="json"),
        pytest.param("[default]\n= value\n", id="assignment without a key"),
        pytest.param("export AWS_PROFILE=x\n", id="shell export"),
        pytest.param("[user]\n\tname = Foo\n\temail = foo@example.com\n", id=".gitconfig"),
        pytest.param("[pypi]\nusername = __token__\npassword = pypi-x\n", id=".pypirc"),
        pytest.param("[tox]\nenvlist = py311\n", id="tox.ini"),
        pytest.param("[global]\nindex-url = https://example.invalid/simple\n", id="pip.conf"),
        pytest.param("[tool:pytest]\ntestpaths = tests\n", id="setup.cfg"),
        pytest.param("FOO=bar\nSECRET=hunter2\n", id=".env: keys before any header"),
        pytest.param("region = us-east-1\n[default]\n", id="key before the first header"),
        pytest.param("[default]\nregion = us-east-1\n[user]\nname = Foo\n", id="an AWS section followed by a foreign one"),
        pytest.param("[profile]\nregion = us-east-1\n", id="profile header with no name"),
    ],
)
def test_other_shapes_are_rejected(text: str) -> None:
    # act
    result = _is_aws_config_text(text)

    # assert
    assert result is False
