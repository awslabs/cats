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

// A three-step demo: deploy, test, destroy. Each step is a <pre data-step> under the stage. The
// player types the step's commands and reveals its output, then stops and shows Next; the step
// buttons in the title bar jump to any step. Reduced motion shows the transcript of every step.
export function setupTerminal(signal) {
  const terminal = document.querySelector("[data-mdx-terminal]")
  if (!terminal) return
  const stage = terminal.querySelector(".mdx-terminal__stage")
  const bodies = [...terminal.querySelectorAll(".mdx-terminal__body")]
  const stepButtons = [...terminal.querySelectorAll(".mdx-terminal__step-btn")]
  const next = terminal.querySelector(".mdx-terminal__next")
  const replay = terminal.querySelector(".mdx-terminal__replay")
  if (!stage || bodies.length === 0 || !next || !replay) return

  if (reducedMotion() || !("IntersectionObserver" in window)) {
    terminal.classList.add("is-static")
    return
  }

  const transcript = document.createElement("pre")
  transcript.className = "mdx-terminal__transcript"
  transcript.textContent = bodies.map((b) => b.textContent).join("\n")
  stage.after(transcript)
  stage.setAttribute("aria-hidden", "true")

  const steps = bodies.map((body) => {
    const lines = [...body.querySelectorAll(".mdx-terminal__line")]
    const commands = new Map(lines.filter((l) => l.matches(".mdx-terminal__cmd")).map((l) => [l, l.textContent]))
    return { body, lines, commands }
  })

  // Every step gets the tallest step's height, so Next and Replay sit in one place and the
  // page does not reflow between steps. Measured from the rendered content, then re-measured
  // on resize since the lines wrap differently at different widths.
  const sizeStage = () => {
    const hiddenBefore = bodies.map((body) => body.hidden)
    for (const body of bodies) body.hidden = false
    stage.style.minHeight = ""
    const tallest = Math.max(...bodies.map((body) => body.scrollHeight))
    stage.style.minHeight = `${tallest}px`
    for (const [i, body] of bodies.entries()) body.hidden = hiddenBefore[i]
  }
  sizeStage()
  window.addEventListener("resize", sizeStage, { signal })

  const timers = new Set()
  const wait = (ms) =>
    new Promise((resolve) => {
      const id = setTimeout(() => timers.delete(id) && resolve(), ms)
      timers.add(id)
    })
  signal.addEventListener("abort", () => timers.forEach(clearTimeout))

  let run = 0 // bumped on every (re)start so a superseded play loop stops at its next await
  let current = -1

  const resetLines = () => {
    for (const { lines, commands } of steps) {
      for (const line of lines) {
        line.classList.remove("is-shown", "is-current")
        const text = commands.get(line)
        if (text !== undefined) line.textContent = text
      }
    }
  }

  const showStep = (index) => {
    current = index
    for (const [i, step] of steps.entries()) step.body.hidden = i !== index
    for (const [i, button] of stepButtons.entries()) {
      button.classList.toggle("is-active", i === index)
      button.classList.toggle("is-done", i < index)
      button.setAttribute("aria-pressed", String(i === index))
    }
  }

  const play = async (index) => {
    const mine = ++run
    next.hidden = true
    replay.hidden = true
    resetLines() // a superseded run may have left a half-typed command or a cursor behind
    showStep(index)
    const { lines, commands } = steps[index]
    terminal.classList.add("is-typing")
    for (const line of lines) {
      if (mine !== run) return
      line.classList.add("is-shown", "is-current")
      const text = commands.get(line)
      if (text !== undefined) {
        line.textContent = ""
        for (const char of text) {
          if (mine !== run) return
          line.textContent += char
          await wait(70)
        }
        await wait(500)
      } else {
        await wait(220)
      }
      line.classList.remove("is-current")
    }
    if (mine !== run) return
    terminal.classList.remove("is-typing")
    const last = index === steps.length - 1
    next.hidden = last
    replay.hidden = !last
  }

  showStep(0)
  terminal.classList.add("is-typing")
  const observer = new IntersectionObserver(
    ([entry]) => {
      if (entry.isIntersecting) {
        observer.disconnect()
        play(0)
      }
    },
    { threshold: 0.5 },
  )
  observer.observe(terminal)

  next.addEventListener("click", () => play(current + 1), { signal })
  replay.addEventListener("click", () => play(0), { signal })
  for (const [i, button] of stepButtons.entries()) {
    button.addEventListener("click", () => play(i), { signal })
  }
  signal.addEventListener("abort", () => observer.disconnect())
}
