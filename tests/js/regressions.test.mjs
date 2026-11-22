// Regression tests for the three things that made the canvas unreadable during a real build.
// Each one is written as the symptom, so a future change that reintroduces it fails here first.
import test from 'node:test'
import assert from 'node:assert/strict'
import { bounds, layout, nextView, scaleOf } from '../../src/camera.js'

const VP = { w: 420, h: 300 }
const driftBoxes = n => Array.from({ length: n }, (_, i) => ({ id: `d${i}`, kind: 'drift' }))
const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

test('symptom 1: unplanned writes can never widen the board more than one drift row', () => {
  const few = bounds(layout([{ id: 'a' }, ...driftBoxes(8)]))
  const many = bounds(layout([{ id: 'a' }, ...driftBoxes(40)]))
  assert.equal(many.w, few.w, '40 drift boxes must not be wider than 8')
  assert.ok(many.h > few.h, 'they stack downward instead')
})

test('symptom 2: a build that keeps adding boxes never zooms the canvas out', () => {
  // A plan that grows the way a real one does: a level of boxes, then drift boxes, then more.
  const boxes = [{ id: 'a' }]
  let content = bounds(layout(boxes))
  let view = nextView(null, content, 'open', VP)
  const startScale = scaleOf(view, VP)

  for (let i = 0; i < 20; i++) {
    boxes.push({ id: `b${i}`, kind: i % 4 === 0 ? 'drift' : 'component', after: i % 4 === 0 ? [] : ['a'] })
    content = bounds(layout(boxes))
    view = nextView(view, content, 'content', VP)
    assert.equal(scaleOf(view, VP), startScale, `zoom changed after adding box ${i}`)
  }
})

test('symptom 3: a plan of questions and components has no overlapping boxes', () => {
  const boxes = [
    { id: 'q1', kind: 'question', label: 'Which payment provider should checkout use?', options: ['Stripe', 'Adyen', 'Invoice'] },
    { id: 'q2', kind: 'question', label: 'Should the founder testimonial go above the pricing?', options: ['Yes', 'No'] },
    { id: 'api', after: ['q1'] },
    { id: 'db', after: ['q1'] },
    { id: 'ui', after: ['api', 'q2'] },
    { id: 'check', after: ['ui'] },
    { id: 'docs', after: ['check'] },
    { id: 'drift1', kind: 'drift' },
  ]
  const placed = layout(boxes)
  const ids = [...placed.keys()]
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      assert.ok(
        !overlaps(placed.get(ids[i]), placed.get(ids[j])),
        `${ids[i]} overlaps ${ids[j]}`
      )
    }
  }
})

test('a wide plan still fits: fit is computed from all boxes, including drift', () => {
  const placed = layout([{ id: 'a' }, { id: 'b', after: ['a'] }, ...driftBoxes(9)])
  const content = bounds(placed)
  const view = nextView(null, content, 'open', VP)
  assert.ok(view.w >= content.w - 1, 'the fit view holds the whole board width')
})
