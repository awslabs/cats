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

// Content is only hidden once this runs (via .mdx-reveal-ready), so the page reads fine without
// JavaScript.
export function setupReveal(signal) {
  const items = document.querySelectorAll(".mdx-reveal")
  if (!items.length) return
  if (reducedMotion() || !("IntersectionObserver" in window)) {
    for (const item of items) item.classList.add("is-visible")
    return
  }

  document.documentElement.classList.add("mdx-reveal-ready")
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible")
          observer.unobserve(entry.target)
        }
      }
    },
    { threshold: 0.15 },
  )
  for (const item of items) observer.observe(item)
  signal.addEventListener("abort", () => observer.disconnect())
}
