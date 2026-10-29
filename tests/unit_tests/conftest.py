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

"""Shared unit-test fixtures."""

from collections.abc import Generator

import pytest

import cats.config
import cats.filepath
import cats.pytest_plugin
from cats.cfn.session import get_current_account_id
from cats.resources import _get_account_cached
from cats.resources.network_interact import _ssm_unregistered_instances, _udp_receipt_tokens
from cats.resources.session_interact import DEFAULT_ROLE_SESSION_NAME, set_role_session_name
from tests.logging_isolation import preserve_root_logger


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, object, object]:
    """Contain root-logger changes made by a test to that test.

    `cats mcp` replaces the root logger's handlers wholesale (see `cats.cli.mcp.main`);
    the replacement then writes INFO lines into every later CliRunner capture and fails
    the `test_infra` snapshots. `pytest-randomly` decides whether that happens in a given
    run, so the failure looked like a flake:

        uv run pytest tests/unit_tests/test_mcp_server_starts.py tests/unit_tests/test_infra.py -p no:randomly

    This is a protocol-level wrapper rather than an autouse fixture because a fixture
    snapshots the handlers during setup and restores them during teardown - two phases
    with different pytest capture handlers attached - so it would re-attach pytest's
    finished setup handler to the root logger on every test.
    """
    with preserve_root_logger():
        return (yield)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's shell must not leak into unit tests.

    - AWS_PROFILE naming a missing profile fails every moto test with ProfileNotFound;
      a valid one would let any un-mocked boto3 path reach a real account.
    - Fake credentials + AWS_EC2_METADATA_DISABLED keep un-mocked paths off the network.
    - CATS_CONFIG from the shell leaks a real config into isolated tests.
    - COLUMNS changes Rich wrapping; consoles built at import time also captured it, so
      their width is forced instead, making exact-output assertions terminal-independent.

    The CLI modules are imported lazily: importing cats.cli at conftest-import time
    would run base.py's logging.basicConfig before pytest owns the root logger, and
    the CLI's stderr log lines would start appearing inside CliRunner output.
    """
    import cats.cli.console
    import cats.cli.infra

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("CATS_CONFIG", raising=False)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    for console in (
        cats.cli.console.stdout_console,
        cats.cli.console.stderr_console,
        cats.cli.infra.console,
        cats.config.stdout_console,
    ):
        monkeypatch.setattr(console, "_width", 1000)


@pytest.fixture(autouse=True)
def _reset_cats_global_state() -> None:
    """Reset module-level globals between tests.

    - `_override_filepath` on `cats.filepath` is sticky once set by `set_cats_config_filepath`
      (e.g. via `get_account`). Without reset, one test leaks its config path into others.
    - `_settings` on `cats.config` caches the first load of `cats.yaml`.
    - `_get_account_cached` is an lru_cache that would otherwise return stale instances.
    - `_udp_receipt_tokens` is populated by AccountUnderTest.__init__ and would leak
      token prefixes into other tests' udp_call transport.
    - `_ssm_unregistered_instances` remembers a spent registration window per instance;
      every handler fixture shares one instance id, so a test that spends the window
      would make the next test's first SendCommand refusal fail fast.
    - `get_current_account_id` is a functools.cache: a test running under broad boto3
      mocking caches a MagicMock account id, which later tests then receive from the
      real call path.
    - `entries` / `_entries_by_nodeid` on `cats.pytest_plugin` accumulate evidence
      entries; a test driving the hooks in-process would otherwise leak entries into
      later tests' reports.
    - `_role_session_name` on `cats.resources.session_interact` is set by every
      `pytest_configure`. A test calling it with a `MagicMock` config stores the mock's
      `stash.get().role_session_name`, and every later AssumeRole fails parameter
      validation (reproduce with the reset removed):

        uv run pytest tests/unit_tests/test_pytest_plugin.py tests/unit_tests/test_session_interact.py -p no:randomly

    The root logger is not reset here: see `pytest_runtest_protocol` above.
    """
    cats.filepath._override_filepath = None
    cats.config.reset_settings()
    _get_account_cached.cache_clear()
    _udp_receipt_tokens.clear()
    _ssm_unregistered_instances.clear()
    get_current_account_id.cache_clear()
    cats.pytest_plugin.entries.clear()
    cats.pytest_plugin._entries_by_nodeid.clear()
    cats.pytest_plugin._selection.clear()
    set_role_session_name(DEFAULT_ROLE_SESSION_NAME)
