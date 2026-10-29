/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
// Each card's text stays in the page on both sides, so it can be read, selected and copied.
// Each side has its own toggle; only the visible side's toggle is in the tab order.
export function setupFlipCards(signal) {
  for (const card of document.querySelectorAll(".mdx-flip")) {
    const front = card.querySelector(".mdx-flip__front .mdx-flip__toggle")
    const back = card.querySelector(".mdx-flip__back .mdx-flip__toggle")
    if (!front || !back) continue
    const flip = (flipped, focus) => {
      card.classList.toggle("is-flipped", flipped)
      front.tabIndex = flipped ? -1 : 0
      back.tabIndex = flipped ? 0 : -1
      focus.focus({ preventScroll: true })
    }
    front.addEventListener("click", () => flip(true, back), { signal })
    back.addEventListener("click", () => flip(false, front), { signal })
  }
}
