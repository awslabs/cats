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

"""Tests for cats.report_run.run_properties: the one table of run facts the OSCAL and JUnit writers both carry."""

from datetime import UTC, datetime
from pathlib import Path

from cats.report_run import ReportRun, run_properties
from cats.user_agent import CatsTestPack
from tests.utils import make_run_manifest

RUN_ID = "20260924T105317Z"
COMMIT = "3f2a9c1e0d4b7f8a9c2e1d3f5b6a7c8d9e0f1a2b"
CONFIG_SHA256 = "71aa0c2d6e8f1b3a5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b"


def test_every_run_fact_is_listed_in_order() -> None:
    # arrange
    manifest = make_run_manifest(run_id=RUN_ID)

    # act
    properties = run_properties(manifest)

    # assert
    expected = [
        ("run_id", RUN_ID),
        ("role_session_name", f"cats-{RUN_ID}"),
        ("caller_identity_arn", "arn:aws:sts::999999999999:assumed-role/ci-runner/job-8812"),
        ("git_commit", COMMIT),
        ("git_dirty", "false"),
        ("config_sha256", CONFIG_SHA256),
        ("test_pack", "AwsCatsPack/acme-controls#0.99.4#framework#NIST-800-53r5"),
    ]
    assert expected == properties


def test_null_caller_identity_has_no_entry() -> None:
    """Neither OSCAL nor JUnit carries a null property, so the fact is absent rather than written as 'None'."""
    # arrange
    manifest = make_run_manifest(run_id=RUN_ID)
    manifest["caller_identity"] = None

    # act
    names = [name for name, _ in run_properties(manifest)]

    # assert
    expected = ["run_id", "role_session_name", "git_commit", "git_dirty", "config_sha256", "test_pack"]
    assert expected == names


def test_null_git_has_no_commit_and_no_dirty_entry() -> None:
    # arrange
    manifest = make_run_manifest(run_id=RUN_ID)
    manifest["git"] = None

    # act
    names = [name for name, _ in run_properties(manifest)]

    # assert
    expected = ["run_id", "role_session_name", "caller_identity_arn", "config_sha256", "test_pack"]
    assert expected == names


def test_dirty_checkout_is_the_json_true() -> None:
    """The same spelling as every other boolean CATS writes to a junit property (cats:xfail is 'true')."""
    # arrange
    manifest = make_run_manifest(run_id=RUN_ID)
    manifest["git"] = {"commit": COMMIT, "dirty": True}

    # act
    properties = dict(run_properties(manifest))

    # assert
    assert properties["git_dirty"] == "true"


def test_null_config_hash_has_no_entry() -> None:
    # arrange
    manifest = make_run_manifest(run_id=RUN_ID)
    manifest["config_sha256"] = None

    # act
    names = [name for name, _ in run_properties(manifest)]

    # assert
    expected = ["run_id", "role_session_name", "caller_identity_arn", "git_commit", "git_dirty", "test_pack"]
    assert expected == names


def test_one_test_pack_entry_per_pack_carrying_its_user_agent_token() -> None:
    """The value must be byte for byte the token CloudTrail's userAgent field holds, metadata included."""
    # arrange
    packs = [
        CatsTestPack(name="acme-controls", version="0.99.4", metadata={"framework": "NIST-800-53r5", "platform": "1.13.0"}),
        CatsTestPack(name="zeta-pack", version="2.0.0"),
    ]
    manifest = make_run_manifest(run_id=RUN_ID)
    manifest["test_packs"] = [{"name": pack.name, "version": pack.version, "metadata": dict(pack.metadata)} for pack in packs]

    # act
    tokens = [value for name, value in run_properties(manifest) if name == "test_pack"]

    # assert
    expected = [pack.user_agent_token for pack in packs]
    assert expected == tokens


def test_no_test_packs_means_no_test_pack_entry() -> None:
    # arrange
    manifest = make_run_manifest(run_id=RUN_ID)
    manifest["test_packs"] = []

    # act
    names = [name for name, _ in run_properties(manifest)]

    # assert
    expected = ["run_id", "role_session_name", "caller_identity_arn", "git_commit", "git_dirty", "config_sha256"]
    assert expected == names


def test_role_session_name_is_the_run_id_with_the_cats_prefix() -> None:
    # arrange - the -2 suffix a same-second run gets must survive, since it is what makes the id unique
    run = ReportRun(
        report_dir=Path("reports"),
        run_id=f"{RUN_ID}-2",
        started_at=datetime(2026, 9, 24, 10, 53, 17, tzinfo=UTC),
        latest_files=True,
        timestamped_files=True,
    )

    # act
    name = run.role_session_name

    # assert
    assert name == "cats-20260924T105317Z-2"
