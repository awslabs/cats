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

from collections.abc import Callable

import pytest

# Must register before importing submodules to enable assertion rewriting
pytest.register_assert_rewrite("cats.resources", "cats.markers", "cats.constants")

from cats.config import CatsConfig  # noqa: E402
from cats.constants import CatsNetworkResponse, CatsNetworkResult, ShellResult, get_account_fixture_names, get_accounts_by_tag  # noqa: E402
from cats.exceptions import (  # noqa: E402
    CatsConfigError,
    CatsError,
    CatsInfrastructureError,
    CatsLambdaInvocationError,
    CatsSSMError,
    CatsUsageError,
    CatsVersionMismatchError,
)
from cats.hookspecs import StandardMetadata  # noqa: E402
from cats.markers import cats, cats_requires  # noqa: E402
from cats.reporting import CatsReporter  # noqa: E402
from cats.resources.account_under_test import AccountUnderTest  # noqa: E402
from cats.resources.network_interact import NetworkHandler  # noqa: E402
from cats.user_agent import CatsTestPack  # noqa: E402
from cats.version import __version__  # noqa: E402

GetAccountFixture = Callable[[str], AccountUnderTest]


__all__ = [
    "AccountUnderTest",
    "CatsConfig",
    "CatsConfigError",
    "CatsError",
    "CatsInfrastructureError",
    "CatsLambdaInvocationError",
    "CatsNetworkResponse",
    "CatsNetworkResult",
    "CatsReporter",
    "CatsSSMError",
    "CatsTestPack",
    "CatsUsageError",
    "CatsVersionMismatchError",
    "GetAccountFixture",
    "NetworkHandler",
    "ShellResult",
    "StandardMetadata",
    "__version__",
    "cats",
    "cats_requires",
    "get_account_fixture_names",
    "get_accounts_by_tag",
]
