// The pane's wiring. A runtime desktop plugin is evaluated from a Blob with a strict import
// allowlist (@hermes/plugin-sdk, react, react/jsx-runtime), so the camera math cannot be a relative
// import — it is generated into the pane by scripts/build-pane.mjs. These tests keep that honest:
// the pane imports nothing else, the generated block matches src/camera.js byte for byte, and the
// pane carries no second hand-written copy of the math.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { BEGIN, END, build, inlineCamera } from '../../scripts/build-pane.mjs'

const root = fileURLToPath(new URL('../../', import.meta.url))
const pane = readFileSync(root + 'desktop/plugin.js', 'utf8')
const source = readFileSync(root + 'src/camera.js', 'utf8')

const beginAt = pane.indexOf(BEGIN)
const endAt = pane.indexOf(END)
const generated = beginAt >= 0 && endAt > beginAt ? pane.slice(beginAt + BEGIN.length, endAt) : ''
const handWritten = beginAt >= 0 ? pane.slice(0, beginAt) + pane.slice(endAt + END.length) : pane

test('the pane imports only the plugin SDK and react', () => {
  const allowed = ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime']
  const specifiers = [...pane.matchAll(/from '([^']+)'/g)].map(m => m[1])
  assert.ok(specifiers.length > 0, 'expected the pane to import the plugin SDK')
  for (const s of specifiers) {
    assert.ok(allowed.includes(s), `unsupported import: ${s} — a runtime plugin may only import ${allowed.join(', ')}`)
  }
})

test('the pane carries the generated camera math byte for byte', () => {
  assert.ok(generated, 'the generated block is missing: run node scripts/build-pane.mjs')
  assert.equal(generated.trim(), inlineCamera(source).trim(), 'stale inline block: run node scripts/build-pane.mjs')
})

test('build() reproduces desktop/plugin.js exactly', () => {
  assert.equal(build(pane, source), pane)
})

test('the math is defined once: outside the generated block the pane only calls it', () => {
  for (const fn of ['layout', 'bounds', 'boxSize', 'wrapText', 'zoomAt', 'panByPixels', 'nextView']) {
    assert.doesNotMatch(
      handWritten,
      new RegExp(`function\\s+${fn}\\s*\\(`),
      `plugin.js defines its own ${fn}() outside the generated block`
    )
  }
})

test('the pane never refits the view because content changed', () => {
  // stickyView() is what used to union every new fit into the shown view, so each added box zoomed
  // the plan out a step further and nothing ever zoomed back in.
  assert.doesNotMatch(pane, /function\s+stickyView\s*\(/, 'the monotonic view merge must be gone')
  assert.match(pane, /nextView\(/, 'the view must come from the camera policy')
})

// Easing is for moves the user asks for once (Fit, a selected box, Follow). A gesture must land
// under the pointer immediately: easing every wheel tick made zoom and pan feel a step behind.
test('gestures are applied at once, not eased', () => {
  assert.match(
    pane,
    /const \{ cam, jump \} = useCamera\(target\)/,
    'useCamera must expose jump() so a gesture can bypass the easing'
  )
  for (const [name, needle] of [
    ['the zoom buttons', /zoomAt\(v, factor, v\.x \+ v\.w \/ 2, v\.y \+ v\.h \/ 2, vp\), liveContent\.current\), true\)/],
    ['the arrow keys', /panByPixels\(camRefLive\.current, dx, dy, liveViewport\.current\), liveContent\.current\), true\)/],
    ['the drag pan', /nudgeCamera\(clampView\(panByPixels\(pn\.view, dx, dy/],
    ['the wheel', /nudgeCamera\(clampView\(zoomAt\(v, factor, bx, by, vp\)/],
  ]) {
    assert.match(pane, needle, `${name} must move the camera immediately`)
  }
  const wheel = pane.slice(pane.indexOf('const onWheel'), pane.indexOf("el.addEventListener('wheel'"))
  assert.doesNotMatch(wheel, /takeCameraRef\.current\(/, 'the wheel must not glide over CAMERA_MS')
})
