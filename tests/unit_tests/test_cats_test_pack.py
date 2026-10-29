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

"""Tests for cats.user_agent.CatsTestPack.

The model exists to keep the User-Agent header well-formed (every character an RFC 7230
tchar, no ``#`` inside a segment) and bounded in length. Each rule gets one test asserting
the exact validation error, in the codebase's validation-test style.
"""

import pytest
from pydantic import ValidationError

from cats.user_agent import TEST_PACK_TOKEN_MAX_LENGTH, CatsTestPack

SEGMENT_MISMATCH = "String should match pattern '^[a-z0-9._-]{1,64}$'"
VALUE_MISMATCH = "String should match pattern '^[A-Za-z0-9._+-]{1,64}$'"


def make_pack(name: str = "acme-controls", version: str = "0.99.4", **metadata: str) -> CatsTestPack:
    return CatsTestPack(name=name, version=version, metadata=metadata)


def first_error(exc_info: pytest.ExceptionInfo[ValidationError]) -> tuple[tuple[int | str, ...], str]:
    error = exc_info.value.errors()[0]
    return tuple(error["loc"]), error["msg"]


def test_name_and_version_render_as_one_token() -> None:
    # arrange
    expected = "AwsCatsPack/acme-controls#0.99.4"

    # act
    pack = make_pack()

    # assert
    assert expected == pack.user_agent_token


def test_metadata_renders_sorted_by_key_after_the_version() -> None:
    # arrange
    expected = "AwsCatsPack/acme-controls#0.99.4#framework#NIST-800-53r5#platform#1.13.0"

    # act
    pack = make_pack(platform="1.13.0", framework="NIST-800-53r5")

    # assert
    assert expected == pack.user_agent_token


def test_uppercase_name_is_rejected_not_lowered() -> None:
    # arrange
    expected = (("name",), SEGMENT_MISMATCH)

    # act
    with pytest.raises(ValidationError) as exc_info:
        make_pack(name="Acme-Controls")

    # assert
    assert expected == first_error(exc_info)


def test_hash_inside_version_is_rejected() -> None:
    # arrange
    expected = (("version",), VALUE_MISMATCH)

    # act
    with pytest.raises(ValidationError) as exc_info:
        make_pack(version="0.99#4")

    # assert
    assert expected == first_error(exc_info)


def test_slash_inside_name_is_rejected() -> None:
    # arrange
    expected = (("name",), SEGMENT_MISMATCH)

    # act
    with pytest.raises(ValidationError) as exc_info:
        make_pack(name="acme/controls")

    # assert
    assert expected == first_error(exc_info)


def test_empty_version_is_rejected() -> None:
    # arrange
    expected = (("version",), VALUE_MISMATCH)

    # act
    with pytest.raises(ValidationError) as exc_info:
        make_pack(version="")

    # assert
    assert expected == first_error(exc_info)


@pytest.mark.parametrize("character", [" ", "\r", "\n", "\t", "(", ")", "=", ":", ";", ",", "/", "#", "@", "\\", '"', "é"])
def test_non_tchar_character_in_version_is_rejected(character: str) -> None:
    # arrange
    expected = (("version",), VALUE_MISMATCH)

    # act
    with pytest.raises(ValidationError) as exc_info:
        make_pack(version=f"1.0{character}rc1")

    # assert
    assert expected == first_error(exc_info)


def test_metadata_key_is_constrained_like_a_name() -> None:
    # arrange
    expected = (("metadata", "Platform-Version", "[key]"), SEGMENT_MISMATCH)

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsTestPack(name="acme", version="1", metadata={"Platform-Version": "1.13.0"})

    # assert
    assert expected == first_error(exc_info)


def test_metadata_value_is_constrained_like_a_version() -> None:
    # arrange
    expected = (("metadata", "platform"), VALUE_MISMATCH)

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsTestPack(name="acme", version="1", metadata={"platform": "1.13 0"})

    # assert
    assert expected == first_error(exc_info)


def test_non_string_version_is_rejected_under_strict_mode() -> None:
    # arrange
    expected = (("version",), "Input should be a valid string")

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsTestPack(name="acme", version=1)  # type: ignore[arg-type]

    # assert
    assert expected == first_error(exc_info)


def test_unknown_field_is_rejected() -> None:
    # arrange
    expected = (("release",), "Extra inputs are not permitted")

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsTestPack(name="acme", version="1", release="0.99.4")  # type: ignore[call-arg]

    # assert
    assert expected == first_error(exc_info)


def test_positional_construction_is_a_type_error() -> None:
    # arrange
    expected = "BaseModel.__init__() takes 1 positional argument but 3 were given"

    # act
    with pytest.raises(TypeError) as exc_info:
        CatsTestPack("acme", "1")  # type: ignore[misc]

    # assert
    assert expected == str(exc_info.value)


def test_token_at_the_length_limit_is_accepted() -> None:
    # arrange
    # The product prefix is 12 characters and the separator 1; a 64-char name leaves 51 for the version.
    version = "v" * (TEST_PACK_TOKEN_MAX_LENGTH - 12 - 64 - 1)
    expected = TEST_PACK_TOKEN_MAX_LENGTH

    # act
    pack = CatsTestPack(name="n" * 64, version=version)

    # assert
    assert expected == len(pack.user_agent_token)


def test_token_over_the_length_limit_is_rejected() -> None:
    # arrange
    version = "v" * (TEST_PACK_TOKEN_MAX_LENGTH - 12 - 64)
    expected = ((), f"Value error, user_agent_token is {TEST_PACK_TOKEN_MAX_LENGTH + 1} characters; the limit is {TEST_PACK_TOKEN_MAX_LENGTH}")

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsTestPack(name="n" * 64, version=version)

    # assert
    assert expected == first_error(exc_info)


def test_packs_with_equal_tokens_hash_equal() -> None:
    # arrange
    first = make_pack(b="2", a="1")
    second = make_pack(a="1", b="2")
    expected = 1

    # act
    distinct = len({first, second})

    # assert
    assert expected == distinct


def test_pack_is_frozen() -> None:
    # arrange
    pack = make_pack()
    expected = "frozen_instance"

    # act
    with pytest.raises(ValidationError) as exc_info:
        pack.name = "other"  # type: ignore[misc]

    # assert
    assert expected == exc_info.value.errors()[0]["type"]


def test_metadata_is_read_only() -> None:
    # arrange
    pack = make_pack(platform="1.13.0")
    expected_message = "'mappingproxy' object does not support item assignment"

    # act
    with pytest.raises(TypeError) as exc_info:
        pack.metadata["platform"] = "2.0.0"  # type: ignore[index]

    # assert
    assert expected_message == str(exc_info.value)


def test_metadata_reads_as_a_mapping() -> None:
    # arrange
    pack = make_pack(platform="1.13.0", team="core")

    # act
    items = dict(pack.metadata)

    # assert
    assert items == {"platform": "1.13.0", "team": "core"}
