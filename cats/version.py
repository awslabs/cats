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

"""CATS version - separate module to avoid circular imports."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("aws-cats")
except importlib.metadata.PackageNotFoundError as e:
    # A raw-checkout import (PYTHONPATH, no install) would otherwise report 0.0.0,
    # tag deployed stacks with it, and fail every later version check confusingly.
    raise RuntimeError(
        "The aws-cats distribution is not installed. Install the package (pip install aws-cats, "
        + "or `uv sync` in a checkout) instead of importing the source tree directly."
    ) from e
