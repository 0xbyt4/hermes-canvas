// The pane's wiring: it must take its geometry and its camera from desktop/camera.js rather than
// carrying a second copy of the math. This is what keeps the regression tests above relevant to the
// code the desktop app actually runs.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const pane = readFileSync(root + 'desktop/plugin.js', 'utf8')

test('the pane imports the camera module', () => {
  assert.match(pane, /from '\.\/camera\.js'/, 'plugin.js must import ./camera.js')
})

test('the pane keeps no second copy of the layout or camera math', () => {
  for (const fn of ['layout', 'bounds', 'boxSize', 'wrapText', 'zoomAt', 'panByPixels']) {
    assert.doesNotMatch(
      pane,
      new RegExp(`^function\\s+${fn}\\s*\\(`, 'm'),
      `plugin.js still defines its own ${fn}()`
    )
  }
})

test('the pane never refits the view because content changed', () => {
  // stickyView() is what used to union every new fit into the shown view, so each added box zoomed
  // the plan out a step further and nothing ever zoomed back in.
  assert.doesNotMatch(pane, /function\s+stickyView\s*\(/, 'the monotonic view merge must be gone')
  assert.match(pane, /nextView\(/, 'the view must come from the camera policy')
})
