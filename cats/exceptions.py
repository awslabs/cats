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

"""Exceptions raised by CATS.

Everything CATS raises deliberately is a `CatsError`, so `except CatsError` catches all
of it. Callers see three kinds for their own mistakes and their environment:

- `CatsUsageError` for an argument CATS cannot act on (also a `ValueError`).
- `CatsConfigError` for their configuration: an invalid or missing `cats.yaml` (pydantic's
  report is embedded in the message), an unknown account name, a feature the config does
  not enable, a Region that is set nowhere.
- `CatsInfrastructureError` for AWS infrastructure that is missing, from another release,
  or unreachable, including the environment's own credential and endpoint failures at
  the session boundary.

The one exception: `CatsTestPack` is a pydantic model, so constructing one with bad
fields raises pydantic's `ValidationError`, as its docstring states.
"""


class CatsError(Exception):
    """Base for all CATS exceptions."""


class CatsUsageError(CatsError, ValueError):
    """The caller passed an argument CATS cannot act on: a bad port, a URL where a hostname was expected, an undeclared @cats keyword."""


class CatsInfrastructureError(CatsError):
    """CATS infrastructure is missing, misconfigured or unreachable. Deploy or redeploy, or fix the environment's AWS access."""


class CatsVersionMismatchError(CatsInfrastructureError):
    """Deployed stack was created by a different CATS release; destroy and deploy."""


class CatsSSMError(CatsError):
    """SSM command execution failed."""


class CatsLambdaInvocationError(CatsError):
    """Lambda invocation returned an error instead of a result."""


class CatsConfigError(CatsError):
    """The configuration is invalid, missing, or does not enable this action: an invalid cats.yaml, an unknown account, an unset Region."""
