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

"""Retry utilities for AWS API calls."""

import logging
import random
import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

MAX_RETRIES = 10
BASE_DELAY = 1.0

P = ParamSpec("P")
T = TypeVar("T")


def retry_on_throttle(func: Callable[P, T]) -> Callable[P, T]:
    """Decorator to retry on CloudFormation throttling errors."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except ClientError as e:
                if "Throttling" in str(e) or "Rate exceeded" in str(e):
                    if attempt == MAX_RETRIES - 1:
                        raise
                    # Security rationale: jitter affects retry timing only; it never creates credentials, tokens, or security decisions.
                    delay = BASE_DELAY * (2**attempt) + random.uniform(0, 1)  # noqa: S311  # nosec B311  # nosemgrep: python_random_rule-random
                    logger.debug(f"Throttled, retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                    time.sleep(delay)
                else:
                    raise
        raise RuntimeError("Unreachable")

    return wrapper
