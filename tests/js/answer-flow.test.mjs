// Answering a question is a step forward: the camera must look at the next question that is still
// open, and go back to the whole plan when that was the last one. It must never keep the close-up
// on the question just answered.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { nextQuestion, nextView, layout, bounds, visible } from '../../src/camera.js'

const root = fileURLToPath(new URL('../../', import.meta.url))
const pane = readFileSync(root + 'desktop/plugin.js', 'utf8')
const V = { w: 500, h: 320 }

const q = (id, extra = {}) => ({ id, kind: 'question', label: `Question ${id}?`, options: ['yes', 'no'], ...extra })

test('the next open question is the one after the answered box', () => {
  const boxes = [
    q('a', { answer: 'yes' }), // already answered
    q('b'),
    { id: 'work', kind: 'component' },
    q('c'),
  ]
  assert.equal(nextQuestion(boxes, 'b').id, 'c', 'it skips answered, non-question and the answered box itself')
  assert.equal(nextQuestion(boxes, 'c').id, 'b', 'an earlier question that is still open is still worth visiting')
  assert.equal(
    nextQuestion([q('a', { answer: 'yes' }), q('b')], 'b'),
    null,
    'no open question left means go back to the plan'
  )
  assert.equal(nextQuestion([q('a'), q('x', { removed: true })], 'a'), null, 'a removed question is not next')
  assert.equal(nextQuestion([], 'a'), null)
})

test('answering the last question shows the whole plan, not a close-up', () => {
  // The failure the user saw: the camera stayed on the answered box, so the view kept showing a
  // finished question. The property that matters is that the plan view contains every box.
  const placed = layout([q('a'), { id: 'b', after: ['a'] }, q('c', { after: ['b'] }), { id: 'd', after: ['c'] }])
  const content = bounds(placed)
  const plan = nextView(null, content, 'fit', V)
  const shown = [...placed.values()].filter(r => visible(plan, r)).length
  assert.equal(shown, placed.size, `the fitted view must contain all ${placed.size} boxes, it shows ${shown}`)
  assert.ok(plan.w >= content.w, 'the fitted view is the plan, never a close-up of one box')
})

test('the pane zooms to the next question after an answer', () => {
  // Wiring: answer() must not leave `zoom` pointing at the box it just answered, and it must make a
  // visible move. Revealing the next box is not enough — at fit scale it is usually already on
  // screen, so the camera would not appear to move at all.
  assert.match(pane, /const lookPastAnswered = answeredId =>/, 'the pane needs a post-answer camera move')
  assert.match(pane, /const answer = async \(box, option\) => \{[\s\S]*?lookPastAnswered\(box\.id\)/, 'answer() must call it')
  const move = pane.slice(pane.indexOf('const lookPastAnswered'), pane.indexOf('const selectBox = box'))
  assert.match(move, /setZoom\(next\.id\)/, 'the next question must be zoomed to, not merely revealed')
  assert.match(move, /nextView\(null, content, 'fit', viewport\)/, 'the last answer fits the whole plan')
  assert.match(move, /const next = nextQuestion\(/, 'it must ask the camera module which question is next')
})
