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

"""Tests for _partitions: botocore's endpoint data, read without consulting the AWS profile chain."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from botocore.loaders import create_loader

from cats.config import _partitions


@pytest.fixture
def _uncached_partitions() -> Iterator[None]:
    """`_partitions` is read once per process; a test about how it reads must start cold and leave nothing behind."""
    _partitions.cache_clear()
    yield
    _partitions.cache_clear()


@pytest.mark.usefixtures("_uncached_partitions")
def test_missing_aws_profile_still_loads_partitions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`cats validate` promises no AWS credentials are used: a stale AWS_PROFILE must not reach config loading (ProfileNotFound)."""
    # arrange
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    expected = [partition["partition"] for partition in create_loader().load_data("endpoints")["partitions"]]

    # act
    result = [partition["partition"] for partition in _partitions()]

    # assert
    assert expected == result
