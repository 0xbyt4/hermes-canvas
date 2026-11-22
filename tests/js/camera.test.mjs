// Camera and layout math: the behaviour the pane depends on, pinned without a browser.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  BOX,
  MARGIN,
  DRIFT,
  ZOOM,
  boxSize,
  layout,
  bounds,
  fitView,
  scaleOf,
  zoomAt,
  panByPixels,
  clampView,
  visible,
  revealBox,
  nextView,
} from '../../desktop/camera.js'

const VP = { w: 500, h: 300 }

const overlaps = (a, b) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

/* ---------- box sizes ---------- */

test('a component box is BOX, a drift box is smaller', () => {
  assert.deepEqual(boxSize({ kind: 'component' }), BOX)
  assert.deepEqual(boxSize({ kind: 'drift' }), { w: 200, h: 64 })
})

test('a question box is wide enough for a sentence and grows with its answers', () => {
  const one = boxSize({ kind: 'question', label: 'Which provider?', options: ['Stripe'] })
  const four = boxSize({
    kind: 'question',
    label: 'Which provider?',
    options: ['Stripe', 'Adyen', 'PayPal', 'Invoice'],
  })
  assert.equal(one.w, 320)
  assert.ok(one.h > BOX.h)
  assert.ok(four.h > one.h, 'more answers must make the box taller')
})

/* ---------- layout ---------- */

test('boxes in the same column stack without touching', () => {
  const placed = layout([
    { id: 'a' },
    { id: 'b', after: ['a'] },
    { id: 'c', after: ['a'] },
  ])
  assert.ok(!overlaps(placed.get('b'), placed.get('c')))
})

test('a question box never overlaps the column beside it', () => {
  const placed = layout([
    { id: 'q', kind: 'question', label: 'Which payment provider should checkout use?', options: ['Stripe', 'Adyen', 'Invoice'] },
    { id: 'api', after: ['q'] },
  ])
  assert.ok(!overlaps(placed.get('q'), placed.get('api')))
})

/* ---------- the drift grid (bug: one endless row) ---------- */

const driftBoxes = n => Array.from({ length: n }, (_, i) => ({ id: `d${i}`, kind: 'drift' }))

test('unplanned writes wrap into rows instead of widening the board', () => {
  const wide = bounds(layout([{ id: 'a' }, ...driftBoxes(8)]))
  const wider = bounds(layout([{ id: 'a' }, ...driftBoxes(12)]))
  assert.equal(wide.w, wider.w, 'past one row of drift boxes the board must stop widening')

  const cap = DRIFT.perRow * (boxSize({ kind: 'drift' }).w + DRIFT.gapX)
  assert.ok(wide.w <= cap + MARGIN * 2 + BOX.w, `board width ${wide.w} must stay within the grid cap`)
})

test('drift boxes past the first row move down, not out', () => {
  const placed = layout([{ id: 'a' }, ...driftBoxes(5)])
  const first = placed.get('d0')
  const fifth = placed.get('d4')
  assert.ok(fifth.y > first.y, 'the fifth drift box starts a new row')
  assert.equal(fifth.x, first.x, 'and sits under the first one')
})

/* ---------- the camera policy (bug: refit on every added box) ---------- */

test('content growth never moves the camera', () => {
  const prev = { x: 10, y: 20, w: 800, h: 480 }
  const huge = { x: 0, y: 0, w: 9000, h: 900 }
  assert.equal(nextView(prev, huge, 'content', VP), prev)
})

test('opening a canvas or pressing Fit fits the whole plan', () => {
  const content = { x: 0, y: 0, w: 400, h: 300 }
  const v = nextView({ x: 99, y: 99, w: 50, h: 30 }, content, 'fit', VP)
  const expected = fitView(content, VP)
  assert.deepEqual([v.w, v.h], [expected.w, expected.h])
  assert.equal(scaleOf(v, VP) > 0, true)
})

test('a new question is revealed without changing the zoom', () => {
  const content = { x: 0, y: 0, w: 4000, h: 600 }
  const prev = { x: 0, y: 0, w: 1000, h: 600 }
  const box = { x: 3500, y: 100, w: 320, h: 180 }
  const v = nextView(prev, content, 'question', VP, { box })
  assert.equal(v.w, prev.w, 'zoom must stay where the user left it')
  assert.equal(v.h, prev.h)
  assert.ok(visible(v, box), 'the question ends up on screen')
})

test('a question already on screen does not move the camera', () => {
  const content = { x: 0, y: 0, w: 4000, h: 600 }
  const prev = { x: 0, y: 0, w: 1000, h: 600 }
  assert.equal(nextView(prev, content, 'question', VP, { box: { x: 40, y: 40, w: 320, h: 180 } }), prev)
})

/* ---------- zoom and pan ---------- */

test('zoom keeps the board point under the pointer still', () => {
  const view = { x: 0, y: 0, w: 1000, h: 600 }
  const [px, py] = [200, 100]
  const v = zoomAt(view, 2, px, py, VP)
  assert.equal(v.w, 500)
  assert.equal(px, v.x + (px - view.x) * (v.w / view.w))
  assert.equal(py, v.y + (py - view.y) * (v.h / view.h))
})

test('zoom stops at the limits', () => {
  const view = { x: 0, y: 0, w: 1000, h: 600 }
  const tight = zoomAt(view, 1000, 0, 0, VP)
  assert.equal(tight.w, VP.w / ZOOM.max)
  const loose = zoomAt(view, 0.0001, 0, 0, VP)
  assert.equal(loose.w, VP.w / ZOOM.min)
})

test('panning moves by screen pixels and never changes the zoom', () => {
  const view = { x: 0, y: 0, w: 1000, h: 600 }
  const v = panByPixels(view, 50, 25, VP)
  assert.equal(scaleOf(view, VP), 0.5)
  assert.equal(v.x, -100)
  assert.equal(v.y, -50)
  assert.equal(v.w, view.w)
})

test('the view cannot be dragged off the plan', () => {
  const content = { x: 0, y: 0, w: 400, h: 300 }
  const v = clampView({ x: -1000, y: -1000, w: 200, h: 120 }, content)
  assert.equal(v.x, -MARGIN)
  assert.equal(v.y, -MARGIN)
  const far = clampView({ x: 9000, y: 9000, w: 200, h: 120 }, content)
  assert.equal(far.x, content.w + MARGIN - far.w)
})

test('reveal slides the least it can and keeps its size', () => {
  const content = { x: 0, y: 0, w: 4000, h: 600 }
  const view = { x: 0, y: 0, w: 1000, h: 600 }
  const box = { x: 1200, y: 40, w: 320, h: 180 }
  const v = revealBox(view, box, content)
  assert.equal(v.w, view.w)
  assert.ok(visible(v, box))
  assert.ok(v.x > view.x, 'it moved right, toward the box')
})
