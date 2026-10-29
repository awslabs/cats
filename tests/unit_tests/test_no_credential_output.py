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

"""CATS never prints, returns or stores a credential (docs/security.md).

`get_frozen_credentials` is the one botocore call that yields raw keys from a session. Every
session CATS builds stays in process memory and refreshes itself; the profile writer names a
role and a parent identity, not keys. If a change needs raw keys, this test is the place where
that decision is made deliberately, with the security page updated in the same change.
"""

from pathlib import Path

import cats


def test_package_never_freezes_credentials() -> None:
    # arrange
    package = Path(cats.__file__).parent

    # act
    offenders = sorted(str(path.relative_to(package)) for path in package.rglob("*.py") if "get_frozen_credentials" in path.read_text())

    # assert
    assert offenders == []
