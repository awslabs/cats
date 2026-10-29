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

from functools import cache
from pathlib import Path

from cats.config import override_config_path
from cats.exceptions import CatsConfigError, CatsUsageError
from cats.filepath import get_cats_config_filepath
from cats.resources.account_under_test import AccountUnderTest


def _resolve_config_path(config_path: str | Path | None) -> Path:
    """None falls back to the existing chain: CATS_CONFIG env var, then ./cats.yaml."""
    if config_path is not None:
        return Path(config_path).resolve()
    return get_cats_config_filepath().resolve()


def get_account(config_path: str | Path | None, name: str) -> AccountUnderTest:
    """Factory for AccountUnderTest, cached by (resolved_path, mtime_ns, name).

    First call per key pays the assume-role + describe-stacks cost. Subsequent calls
    are instant. Edits to cats.yaml invalidate the cache via mtime.

    Raises:
        CatsConfigError: If the config file does not exist, or `name` is not an
            account keyname in it.
    """
    resolved = _resolve_config_path(config_path)
    if resolved.is_file() is False:
        raise CatsConfigError(f"Config file not found: {resolved}")
    mtime_ns = resolved.stat().st_mtime_ns
    return _get_account_cached(resolved, mtime_ns, name)


@cache
def _get_account_cached(resolved_path: Path, mtime_ns: int, name: str) -> AccountUnderTest:
    with override_config_path(resolved_path):
        return AccountUnderTest(name)


def prepare_udp_receiver(config_path: str | Path | None, receiving_account: str, host: str) -> AccountUnderTest:
    """Construct the account that owns the CATS listener at `host`, so udp_call can find its receipt token.

    A CATS listener drops any datagram that does not carry its account's receipt token, and
    udp_call learns that token from the registry AccountUnderTest.__init__ fills. In pytest the
    receiving account's fixture does this; `cats run` and the MCP tools are one process per
    call (or a long-lived server that may never have constructed the receiver), so they take
    the receiving account by name and construct it here first.

    Raises:
        CatsUsageError: If `host` is not the ec2_dns_name of any of that account's listeners.
            Sending anyway would produce SENT and then received=False for a datagram CATS
            itself discarded, which is the misreport this argument exists to prevent.
    """
    receiver = get_account(config_path, receiving_account)
    listeners = sorted(deployed.ec2.dns_name for deployed in receiver._deployed_network_handlers.values())
    if host not in listeners:
        raise CatsUsageError(
            f"host {host!r} is not a CATS listener of receiving account '{receiving_account}'. "
            + f"Address a listener by its ec2_dns_name: {', '.join(listeners) or '(no listeners deployed)'}. "
            + "For a UDP target that is not a CATS listener, omit the receiving account."
        )
    return receiver
