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
import { reducedMotion } from "./motion.js"

// data-depth is the fraction of scroll distance a layer lags behind the page (0 = moves with the
// page, 1 = pinned). Far layers get higher values so they appear to move slower.
export function setupParallax(signal) {
  const layers = document.querySelectorAll(".mdx-parallax__layer[data-depth]")
  if (!layers.length || reducedMotion()) return

  let queued = false
  const update = () => {
    const y = window.scrollY
    for (const layer of layers) {
      layer.style.transform = `translate3d(0, ${y * layer.dataset.depth}px, 0)`
    }
    queued = false
  }
  window.addEventListener(
    "scroll",
    () => {
      if (!queued) {
        queued = true
        requestAnimationFrame(update)
      }
    },
    { passive: true, signal },
  )
  update()
}
