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

from enum import StrEnum
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

PROMPTS_DIR = Path(__file__).parent


class PromptName(StrEnum):
    OVERVIEW = "overview"
    SETUP = "setup"
    TESTING = "testing"
    AGENTIC = "agentic"


def get_prompt(name: PromptName, *, standalone: bool = True) -> str:
    """Render a prompt template by name.

    Every prompt opens with the shared base (what ``cats prompts overview`` prints) so that
    it stands alone in an agent's context. ``standalone=False`` renders only the prompt's
    own body, for combining several prompts under one copy of the base. The overview has
    no body of its own: it is the base, whichever way it is rendered.
    """
    env = Environment(loader=FileSystemLoader(PROMPTS_DIR), autoescape=True)
    template = env.get_template(f"{name.value}.md.j2")
    return template.render(standalone=standalone)


def get_all_prompts() -> str:
    """Every prompt in one document, with the shared base once.

    The overview supplies the base; the other prompts contribute their bodies. Concatenating
    the standalone prompts instead repeated the base once per prompt, a third of the document.
    """
    parts = [get_prompt(name, standalone=name is PromptName.OVERVIEW) for name in PromptName]
    return "\n\n---\n\n".join(parts)
