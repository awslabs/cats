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

"""The User-Agent suffix CATS adds to its AWS API calls, and the one place it is stamped.

CATS identifies itself as ``AwsCats/<version>``, as disclosed in the README. A test pack may
add its own name and version through the ``pytest_cats_test_pack`` hook (``cats.hookspecs``);
the pytest plugin registers the result here at configure time and every session built
afterwards carries both tokens. Token content is limited to RFC 7230 ``tchar`` characters
because botocore appends ``user_agent_extra`` to the header without sanitizing it.
"""

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Self

import boto3
import botocore.session
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from cats.version import __version__

logger = logging.getLogger(__name__)

USER_AGENT_EXTRA = f"AwsCats/{__version__}"
"""The CATS token, the single source of truth for code and tests."""

TEST_PACK_PRODUCT = "AwsCatsPack"
TEST_PACK_TOKEN_MAX_LENGTH = 128

# Lowercase is required, not applied: the value a pack writes is the value that ships.
Segment = Annotated[str, StringConstraints(pattern=r"^[a-z0-9._-]{1,64}$")]
Value = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._+-]{1,64}$")]


class CatsTestPack(BaseModel):
    """A test pack's identity, rendered as one User-Agent token.

    ``name`` and metadata keys are lowercase ``[a-z0-9._-]``; ``version`` and metadata values
    are ``[A-Za-z0-9._+-]``. ``#`` separates segments and may not appear inside one. Values
    identify a release, never a run.

    The model is frozen and ``metadata`` is a read-only mapping, so a registered pack cannot
    change after the hook returned it. Constructing one with a field that breaks these rules
    raises pydantic's ``ValidationError`` (the one CATS export whose errors are not a
    ``CatsError``).

    Example::

        CatsTestPack(name="acme-controls", version="0.99.4")
        # -> AwsCatsPack/acme-controls#0.99.4
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Segment
    version: Value
    metadata: Mapping[Segment, Value] = Field(default_factory=dict)

    @field_validator("metadata", mode="after")
    @classmethod
    def _freeze_metadata(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @property
    def user_agent_token(self) -> str:
        segments = [self.name, self.version]
        for key in sorted(self.metadata):
            segments.extend((key, self.metadata[key]))
        return f"{TEST_PACK_PRODUCT}/" + "#".join(segments)

    @model_validator(mode="after")
    def _token_fits(self) -> Self:
        length = len(self.user_agent_token)
        if length > TEST_PACK_TOKEN_MAX_LENGTH:
            raise ValueError(f"user_agent_token is {length} characters; the limit is {TEST_PACK_TOKEN_MAX_LENGTH}")
        return self

    def __hash__(self) -> int:
        # Pydantic cannot derive a hash for a model with a dict field; the token is the identity.
        return hash(self.user_agent_token)


_registered_test_packs: dict[str, CatsTestPack] = {}


def register_test_pack(pack: CatsTestPack) -> None:
    """Record a test pack for this process. One version per name; the first declaration wins."""
    existing = _registered_test_packs.get(pack.name)
    if existing is None:
        _registered_test_packs[pack.name] = pack
        return
    if existing != pack:
        logger.warning(
            "Test pack '%s' declared twice with different values; keeping %s, ignoring %s",
            pack.name,
            existing.user_agent_token,
            pack.user_agent_token,
        )


def registered_test_packs() -> tuple[CatsTestPack, ...]:
    """The packs registered in this process, sorted by name."""
    return tuple(_registered_test_packs[name] for name in sorted(_registered_test_packs))


def user_agent_extra() -> str:
    """The full suffix: the CATS token, then one token per registered pack."""
    return " ".join([USER_AGENT_EXTRA, *(pack.user_agent_token for pack in registered_test_packs())])


def attributed_botocore_session() -> botocore.session.Session:
    """A botocore session stamped with :func:`user_agent_extra`. Every CATS session is built from one."""
    botocore_session = botocore.session.get_session()
    botocore_session.user_agent_extra = user_agent_extra()
    return botocore_session


def attributed_session(
    *,
    region_name: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
) -> boto3.Session:
    """Build a boto3 Session whose AWS API calls carry :func:`user_agent_extra` in the User-Agent header."""
    return boto3.Session(
        botocore_session=attributed_botocore_session(),
        region_name=region_name,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        aws_session_token=aws_session_token,
    )
