// Hermes Canvas: the desktop half.
//
// - A right-side pane draws the focused chat's canvas as a hand-drawn sketch. Box status comes
//   from the plugin's hooks (real file writes and check exit codes), never from this pane.
// - The user steers on the canvas: select, move, rename, remove or restore a box, answer a
//   question. Each change goes to POST /edit; the agent reads it on its next turn.
// - Live updates: the backend broadcasts plugin.hermes-canvas.changed. A slow poll is the fallback.
// - The ::hermes-canvas{id="cv-..."} transcript directive renders a small status card in chat.
//
// Plain ESM loaded uncompiled by Hermes Desktop: no JSX, and only the three allowed imports.
import { host, useValue, TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'hermes-canvas'
const PANE_ID = `${PLUGIN_ID}:pane`
const CHANGED_EVENT = `plugin.${PLUGIN_ID}.changed`
const POLL_MS = 4000
const CANVAS_ID_RE = /^cv-[0-9a-f]{10}$/
const BOX = { w: 184, h: 78 }
const GAP = { x: 76, y: 46 }
const MARGIN = 60

let pluginCtx = null
const subscribers = new Set() // mounted views: fn(event | null)

function notifyViews(event) {
  subscribers.forEach(fn => {
    try {
      fn(event)
    } catch (err) {
      console.error('[hermes-canvas] view update failed', err)
    }
  })
}

function useCanvasEvents(handler) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const fn = event => ref.current(event)
    subscribers.add(fn)
    return () => subscribers.delete(fn)
  }, [])
}

function rest(path, opts) {
  if (!pluginCtx) return Promise.reject(new Error('hermes-canvas is not loaded'))
  return pluginCtx.rest(path, opts)
}

function errorText(err) {
  if (!err) return 'unknown error'
  if (typeof err === 'string') return err
  return err.detail || err.message || String(err)
}

/* ---------- sketch geometry (deterministic per box id, so redraws do not wobble) ---------- */

function mulberry(a) {
  return function () {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function hash(s) {
  let h = 2166136261
  for (const c of String(s)) {
    h ^= c.charCodeAt(0)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

const f1 = n => n.toFixed(1)

function sline(r, x1, y1, x2, y2, amp = 1.6) {
  const j = () => (r() - 0.5) * amp * 2
  const dx = x2 - x1
  const dy = y2 - y1
  const len = Math.hypot(dx, dy) || 1
  const off = (r() - 0.5) * Math.min(7, len * 0.04) * 2
  const cx = (x1 + x2) / 2 - (dy / len) * off
  const cy = (y1 + y2) / 2 + (dx / len) * off
  return `M${f1(x1 + j())} ${f1(y1 + j())} Q${f1(cx)} ${f1(cy)} ${f1(x2 + j())} ${f1(y2 + j())}`
}

function srect(seed, x, y, w, h, passes = 2) {
  const r = mulberry(seed)
  let d = ''
  for (let p = 0; p < passes; p++) {
    d += sline(r, x, y, x + w, y) + sline(r, x + w, y, x + w, y + h)
    d += sline(r, x + w, y + h, x, y + h) + sline(r, x, y + h, x, y)
  }
  return d
}

function scribble(id, w, h) {
  const r = mulberry(hash(id + 'strike'))
  let d = ''
  const n = 5
  for (let i = 0; i < n; i++) {
    const x1 = 12 + ((w - 24) * i) / n
    const x2 = 12 + ((w - 24) * (i + 1)) / n
    const [y1, y2] = i % 2 ? [h * 0.74, h * 0.28] : [h * 0.28, h * 0.74]
    d += sline(r, x1, y1, x2, y2, 2.2)
  }
  return d + sline(r, 6, h * 0.56, w - 6, h * 0.44, 2)
}

function tickPath(id, w) {
  const r = mulberry(hash(id + 'tick'))
  const x = w - 30
  const j = () => (r() - 0.5) * 2
  return `M${f1(x + j())} ${f1(16 + j())} Q${f1(x + 5)} ${f1(24)} ${f1(x + 8 + j())} ${f1(27 + j())} Q${f1(x + 14)} ${f1(14)} ${f1(x + 24 + j())} ${f1(4 + j())}`
}

function wrapText(text, max, lines = 2) {
  const out = []
  let cur = ''
  for (const w of String(text || '').split(/\s+/)) {
    if (!w) continue
    if ((cur + ' ' + w).trim().length > max && cur) {
      out.push(cur)
      cur = w
    } else cur = (cur + ' ' + w).trim()
  }
  if (cur) out.push(cur)
  if (out.length > lines) {
    const kept = out.slice(0, lines)
    kept[lines - 1] = kept[lines - 1].slice(0, max - 1) + '…'
    return kept
  }
  return out
}

/* ---------- layout ---------- */

function boxSize(box) {
  if (box.kind === 'question') return { w: 220, h: 104 }
  if (box.kind === 'drift') return { w: 200, h: 64 }
  return BOX
}

// Every box gets an automatic slot by dependency depth: in columns (left to right) on a wide pane,
// in rows (top to bottom) on a tall one, so boxes stay readable. A box the user moved keeps its own
// x/y, but still holds its slot, so moving one box never shifts the others.
function layout(boxes, vertical) {
  const byId = new Map(boxes.map(b => [b.id, b]))
  const depth = new Map()
  const depthOf = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id)
    if (seen.has(id)) return 0
    seen.add(id)
    const box = byId.get(id)
    const deps = (box && box.after ? box.after : []).filter(a => byId.has(a))
    const d = deps.length ? 1 + Math.max(...deps.map(a => depthOf(a, seen))) : 0
    depth.set(id, d)
    return d
  }
  const rows = new Map()
  const auto = new Map()
  const driftRow = []
  for (const box of boxes) {
    if (box.kind === 'drift') {
      driftRow.push(box)
      continue
    }
    const level = box.kind === 'question' ? 0 : depthOf(box.id)
    const slot = rows.get(level) || 0
    rows.set(level, slot + 1)
    const x = vertical ? slot * (BOX.w + 44) : level * (BOX.w + GAP.x + 40)
    const y = vertical ? level * (BOX.h + GAP.y + 40) : slot * (BOX.h + GAP.y + 30)
    auto.set(box.id, { x, y, ...boxSize(box) })
  }
  let maxY = 0
  auto.forEach(p => {
    maxY = Math.max(maxY, p.y + p.h)
  })
  driftRow.forEach((box, i) => {
    auto.set(box.id, { x: i * (220 + 24), y: maxY + GAP.y + 30, ...boxSize(box) })
  })
  const placed = new Map()
  for (const box of boxes) {
    const p = auto.get(box.id)
    const pinned = typeof box.x === 'number' && typeof box.y === 'number'
    placed.set(box.id, pinned ? { ...p, x: box.x, y: box.y } : p)
  }
  return placed
}

// The view only grows while the same canvas is shown in the same orientation: a box moved inside
// the board must not rescale or shift the rest.
function stickyView(prev, next) {
  if (!prev) return next
  const x0 = Math.min(prev.x, next.x)
  const y0 = Math.min(prev.y, next.y)
  const x1 = Math.max(prev.x + prev.w, next.x + next.w)
  const y1 = Math.max(prev.y + prev.h, next.y + next.h)
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
}

function bounds(placed) {
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  placed.forEach(p => {
    x0 = Math.min(x0, p.x)
    y0 = Math.min(y0, p.y)
    x1 = Math.max(x1, p.x + p.w)
    y1 = Math.max(y1, p.y + p.h + 22)
  })
  if (!isFinite(x0)) return { x: 0, y: 0, w: 600, h: 400 }
  return { x: x0 - MARGIN, y: y0 - MARGIN, w: x1 - x0 + MARGIN * 2, h: y1 - y0 + MARGIN * 2 }
}

// Where the line from a's center toward b's center leaves a's rectangle.
function edgePoint(a, b) {
  const ax = a.x + a.w / 2
  const ay = a.y + a.h / 2
  const dx = b.x + b.w / 2 - ax
  const dy = b.y + b.h / 2 - ay
  const sx = dx ? a.w / 2 / Math.abs(dx) : Infinity
  const sy = dy ? a.h / 2 / Math.abs(dy) : Infinity
  const s = Math.min(sx, sy)
  return { x: ax + dx * s, y: ay + dy * s }
}

/* ---------- small helpers ---------- */

const STATUS_TEXT = { todo: 'planned', doing: 'in progress', done: 'done', failed: 'failing' }

function lastLine(text) {
  const lines = String(text || '')
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
  return lines.length ? lines[lines.length - 1] : ''
}

function progressOf(doc) {
  const parts = doc.boxes.filter(b => b.kind === 'component' && !b.removed)
  return { done: parts.filter(b => b.status === 'done').length, total: parts.length }
}

function boxState(box) {
  if (box.removed) return 'removed'
  if (box.kind === 'drift') return 'drift'
  return box.status
}

/* ---------- styles (scoped by the hc- prefix, theme variables only) ---------- */

const STYLE = `
.hc-root { --hc-ink: var(--ui-text-primary); --hc-muted: var(--ui-text-tertiary); --hc-line: var(--ui-stroke-secondary);
  --hc-paper: var(--ui-bg-editor); --hc-box: color-mix(in srgb, var(--hc-ink) 5%, var(--hc-paper)); --hc-doing: var(--ui-blue); --hc-done: var(--ui-green);
  --hc-fail: var(--ui-red); --hc-note: var(--ui-yellow); --hc-accent: var(--ui-accent);
  --hc-hand: "Bradley Hand", "Chalkboard SE", "Segoe Print", "Comic Sans MS", cursive;
  display: flex; flex-direction: column; height: 100%; min-height: 0; color: var(--hc-ink); font-size: 12px; }
.hc-head { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--hc-line); min-width: 0; }
.hc-title { font-family: var(--hc-hand); font-size: 16px; font-weight: 700; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hc-meta { color: var(--hc-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.hc-live { width: 7px; height: 7px; border-radius: 50%; background: var(--hc-done); flex: none; }
.hc-live.off { background: var(--hc-muted); }
.hc-board { position: relative; flex: 1; min-height: 160px; overflow: hidden; }
.hc-board svg { position: absolute; inset: 0; width: 100%; height: 100%; touch-action: none; user-select: none; }
.hc-dots { fill: var(--hc-line); opacity: .6; }
.hc-el { cursor: pointer; }
.hc-el.dragging { cursor: grabbing; }
.hc-fill { fill: var(--hc-box); }
.hc-ink { fill: none; stroke: var(--hc-ink); stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; opacity: .85; }
.hc-el text { font-family: var(--hc-hand); fill: var(--hc-ink); }
.hc-sub { font-family: ui-monospace, SFMono-Regular, Menlo, monospace !important; font-size: 10.5px; fill: var(--hc-muted) !important; }
.hc-err { font-family: ui-monospace, SFMono-Regular, Menlo, monospace !important; font-size: 10.5px; fill: var(--hc-fail) !important; }
.hc-doing .hc-fill { fill: color-mix(in srgb, var(--hc-doing) 14%, var(--hc-box)); }
.hc-doing .hc-ink { stroke: var(--hc-doing); stroke-width: 2.1; opacity: 1; }
.hc-done .hc-fill { fill: color-mix(in srgb, var(--hc-done) 14%, var(--hc-box)); }
.hc-done .hc-ink { stroke: var(--hc-done); opacity: 1; }
.hc-failed .hc-fill { fill: color-mix(in srgb, var(--hc-fail) 13%, var(--hc-box)); }
.hc-failed .hc-ink { stroke: var(--hc-fail); stroke-width: 2.1; opacity: 1; }
.hc-removed { opacity: .75; }
.hc-removed .hc-ink { stroke: var(--hc-muted); }
.hc-removed text { fill: var(--hc-muted); }
.hc-strike { fill: none; stroke: var(--hc-fail); stroke-width: 2.4; stroke-linecap: round; opacity: .85; }
.hc-drift .hc-fill { fill: transparent; }
.hc-drift .hc-ink { stroke: var(--hc-muted); stroke-dasharray: 7 5; }
.hc-question .hc-fill { fill: color-mix(in srgb, var(--hc-note) 30%, var(--hc-box)); }
.hc-question .hc-ink { stroke: color-mix(in srgb, var(--hc-note) 60%, var(--hc-ink)); }
.hc-opt { cursor: pointer; }
.hc-opt rect { fill: var(--hc-box); stroke: var(--hc-ink); stroke-width: 1.1; opacity: .9; }
.hc-opt:hover rect { stroke: var(--hc-accent); }
.hc-opt.chosen rect { fill: var(--hc-ink); }
.hc-opt.chosen text { fill: var(--hc-paper) !important; }
.hc-opt.dim { opacity: .45; }
.hc-opt text { font-weight: 700; font-size: 13px; }
.hc-sel { fill: none; stroke: var(--hc-accent); stroke-width: 1.5; stroke-dasharray: 5 4; }
.hc-edge { fill: none; stroke: var(--hc-muted); stroke-width: 1.6; stroke-linecap: round; }
.hc-edge.ok { stroke: var(--hc-done); }
.hc-arrow { fill: var(--hc-muted); }
.hc-arrow.ok { fill: var(--hc-done); }
.hc-ring { fill: none; stroke: var(--hc-doing); stroke-width: 2.4; stroke-dasharray: 26 12; transform-box: fill-box; transform-origin: center; animation: hc-spin .9s linear infinite; }
.hc-tick { fill: none; stroke: var(--hc-done); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.hc-xmark { fill: var(--hc-fail); }
.hc-xmark-ink { fill: none; stroke: var(--hc-paper); stroke-width: 2; stroke-linecap: round; }
.hc-owner { fill: var(--hc-accent); font-size: 12px !important; }
.hc-empty { position: absolute; inset: 0; display: grid; place-items: center; padding: 24px; text-align: center; color: var(--hc-muted); line-height: 1.5; }
.hc-empty b { display: block; font-family: var(--hc-hand); font-size: 18px; color: var(--hc-ink); margin-bottom: 4px; }
.hc-detail { border-top: 1px solid var(--hc-line); padding: 8px 10px 10px; max-height: 46%; overflow: auto; display: flex; flex-direction: column; gap: 6px; }
.hc-detail-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.hc-detail-title { font-family: var(--hc-hand); font-size: 15px; font-weight: 700; flex: 1; min-width: 80px; overflow-wrap: anywhere; }
.hc-chip { font-size: 10.5px; font-weight: 600; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--hc-line); color: var(--hc-muted); white-space: nowrap; }
.hc-chip.doing { color: var(--hc-doing); border-color: var(--hc-doing); }
.hc-chip.done { color: var(--hc-done); border-color: var(--hc-done); }
.hc-chip.failed, .hc-chip.removed { color: var(--hc-fail); border-color: var(--hc-fail); }
.hc-k { font-size: 10px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--hc-muted); }
.hc-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
.hc-pre { margin: 0; padding: 6px 8px; border-radius: 6px; background: var(--ui-bg-tertiary); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 160px; overflow: auto; }
.hc-pre.err { color: var(--hc-fail); }
.hc-btn { font: inherit; font-size: 11.5px; color: var(--hc-ink); background: transparent; border: 1px solid var(--hc-line); border-radius: 6px; padding: 3px 9px; cursor: pointer; white-space: nowrap; }
.hc-btn:hover:not(:disabled) { border-color: var(--hc-muted); }
.hc-btn:disabled { opacity: .45; cursor: not-allowed; }
.hc-btn.danger { color: var(--hc-fail); }
.hc-input { font: inherit; font-family: var(--hc-hand); font-size: 15px; color: var(--hc-ink); background: var(--hc-paper); border: 1px solid var(--hc-accent); border-radius: 6px; padding: 2px 6px; flex: 1; min-width: 80px; }
.hc-foot { display: flex; align-items: center; gap: 10px; padding: 6px 10px; border-top: 1px solid var(--hc-line); color: var(--hc-muted); font-size: 11px; flex-wrap: wrap; }
.hc-foot .hc-src { font-style: italic; flex: 1; min-width: 0; }
.hc-error { color: var(--hc-fail); }
.hc-card { margin: 4px 0; border: 1px solid var(--hc-line); border-radius: 10px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; max-width: 520px; }
.hc-card-head { display: flex; align-items: center; gap: 8px; }
.hc-bar { height: 5px; border-radius: 999px; background: var(--ui-bg-tertiary); overflow: hidden; }
.hc-bar i { display: block; height: 100%; background: var(--hc-done); }
.hc-chips { display: flex; flex-wrap: wrap; gap: 4px; }
@keyframes hc-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .hc-ring { animation: none; } }
`

function Style() {
  return jsx('style', { children: STYLE })
}

/* ---------- board pieces ---------- */

function Edge({ from, to, ok }) {
  const a = edgePoint(from, to)
  const b = edgePoint(to, from)
  const r = mulberry(hash(`${from.id}>${to.id}`))
  const d = sline(r, a.x, a.y, b.x, b.y, 1.2)
  const ang = Math.atan2(b.y - a.y, b.x - a.x)
  const head = [
    [b.x, b.y],
    [b.x - 11 * Math.cos(ang - 0.4), b.y - 11 * Math.sin(ang - 0.4)],
    [b.x - 11 * Math.cos(ang + 0.4), b.y - 11 * Math.sin(ang + 0.4)]
  ]
    .map(p => p.map(f1).join(','))
    .join(' ')
  return jsxs('g', {
    children: [
      jsx('path', { className: `hc-edge${ok ? ' ok' : ''}`, d }),
      jsx('polygon', { className: `hc-arrow${ok ? ' ok' : ''}`, points: head })
    ]
  })
}

function Badge({ box, w }) {
  if (box.removed || box.kind === 'drift' || box.kind === 'question') return null
  if (box.status === 'doing') {
    return jsx('circle', { className: 'hc-ring', cx: w - 15, cy: 15, r: 7 })
  }
  if (box.status === 'done') return jsx('path', { className: 'hc-tick', d: tickPath(box.id, w) })
  if (box.status === 'failed') {
    return jsxs('g', {
      transform: `translate(${w - 15} 15)`,
      children: [
        jsx('circle', { className: 'hc-xmark', r: 8.5 }),
        jsx('path', { className: 'hc-xmark-ink', d: 'M-3.2 -3.2 L3.2 3.2 M3.2 -3.2 L-3.2 3.2' })
      ]
    })
  }
  return null
}

function BoxLabel({ box, w, top, size, max }) {
  const lines = wrapText(box.label, max)
  return jsx('text', {
    x: w / 2,
    y: top,
    textAnchor: 'middle',
    fontSize: size,
    children: lines.map((line, i) => jsx('tspan', { x: w / 2, dy: i ? size * 1.15 : 0, children: line }, i))
  })
}

function subLine(box) {
  if (box.removed) return 'removed from the plan'
  if (box.kind === 'drift') return 'written outside every box'
  if (box.owner) return `${box.owner} · ${box.writes} write${box.writes === 1 ? '' : 's'}`
  if (box.writes) return `${box.writes} write${box.writes === 1 ? '' : 's'}`
  if (box.check) return box.check
  return (box.files || []).join(', ')
}

function BoxEl({ box, pos, selected, onPointerDown, onDoubleClick, onAnswer }) {
  const { w, h } = pos
  const state = boxState(box)
  const cls = ['hc-el', `hc-${state}`, box.kind === 'question' ? 'hc-question' : '', box.kind === 'drift' ? 'hc-drift' : '']
  const children = [
    jsx('rect', { className: 'hc-fill', x: 2, y: 2, width: w - 4, height: h - 4, rx: 6 }, 'fill'),
    jsx('path', { className: 'hc-ink', d: srect(hash(box.id), 0, 0, w, h) }, 'ink')
  ]
  if (box.kind === 'question') {
    children.push(jsx(BoxLabel, { box, w, top: 26, size: 15, max: 24 }, 'label'))
    const n = box.options.length || 1
    const pad = 14
    const gap = 8
    const ow = (w - pad * 2 - gap * (n - 1)) / n
    box.options.forEach((opt, i) => {
      const chosen = box.answer === opt
      const dim = !!box.answer && !chosen
      children.push(
        jsxs(
          'g',
          {
            className: `hc-opt${chosen ? ' chosen' : ''}${dim ? ' dim' : ''}`,
            transform: `translate(${pad + i * (ow + gap)} ${h - 42})`,
            onPointerDown: e => e.stopPropagation(),
            onClick: e => {
              e.stopPropagation()
              if (!box.removed) onAnswer(box, opt)
            },
            role: 'button',
            'aria-label': `Answer: ${opt}`,
            children: [
              jsx('rect', { width: ow, height: 28, rx: 6 }),
              jsx('text', { x: ow / 2, y: 19, textAnchor: 'middle', children: opt.length > 16 ? opt.slice(0, 15) + '…' : opt })
            ]
          },
          `opt-${i}`
        )
      )
    })
  } else if (box.kind === 'drift') {
    children.push(jsx('text', { x: 12, y: 24, fontSize: 14, className: 'hc-err', children: 'drift' }, 'tag'))
    children.push(
      jsx('text', { x: 12, y: 44, className: 'hc-sub', children: (box.files[0] || box.label).slice(-34) }, 'path')
    )
  } else {
    children.push(jsx(BoxLabel, { box, w, top: 34, size: 17, max: 18 }, 'label'))
    children.push(
      jsx('text', { x: w / 2, y: h - 10, textAnchor: 'middle', className: 'hc-sub', children: subLine(box).slice(0, 34) }, 'sub')
    )
  }
  children.push(jsx(Badge, { box, w }, 'badge'))
  if (box.removed) children.push(jsx('path', { className: 'hc-strike', d: scribble(box.id, w, h) }, 'strike'))
  if (box.status === 'failed' && !box.removed && box.kind === 'component') {
    const err = lastLine(box.check_output) || `exit ${box.check_exit}`
    children.push(jsx('text', { x: 2, y: h + 16, className: 'hc-err', children: '✗ ' + err.slice(0, 40) }, 'err'))
  }
  if (selected) {
    children.push(jsx('rect', { className: 'hc-sel', x: -6, y: -6, width: w + 12, height: h + 12, rx: 8 }, 'sel'))
  }
  return jsx('g', {
    className: cls.filter(Boolean).join(' '),
    transform: `translate(${f1(pos.x)} ${f1(pos.y)})`,
    onPointerDown: e => onPointerDown(e, box),
    onDoubleClick: () => onDoubleClick(box),
    'data-box': box.id,
    children
  })
}

/* ---------- detail strip for the selected box ---------- */

function Detail({ box, renaming, busy, onRename, onCancelRename, onStartRename, onRemove, onRestore }) {
  const [draft, setDraft] = useState(box.label)
  const inputRef = useRef(null)
  // A poll can change the label while the user types: keep the draft until renaming ends.
  useEffect(() => {
    if (!renaming) setDraft(box.label)
  }, [box.id, box.label, renaming])
  useEffect(() => {
    if (renaming && inputRef.current) {
      inputRef.current.focus()
      inputRef.current.select()
    }
  }, [renaming])
  const state = boxState(box)
  const rows = []
  if (box.owner) rows.push(['Built by', jsx('span', { className: 'hc-mono', children: box.owner })])
  if (box.files && box.files.length) rows.push(['Files', jsx('span', { className: 'hc-mono', children: box.files.join('  ') })])
  if (box.check) rows.push(['Check', jsx('span', { className: 'hc-mono', children: box.check })])
  if (box.written && box.written.length) {
    rows.push(['Written', jsx('span', { className: 'hc-mono', children: box.written.slice(-6).join('  ') })])
  }
  if (box.blocked) rows.push(['Blocked writes', jsx('span', { children: String(box.blocked) })])
  if (box.kind === 'question') {
    rows.push(['Answer', jsx('span', { children: box.answer || 'not answered yet: pick an option on the box' })])
  }
  const head = renaming
    ? jsxs('form', {
        className: 'hc-detail-head',
        onSubmit: e => {
          e.preventDefault()
          onRename(draft)
        },
        children: [
          jsx('input', {
            ref: inputRef,
            className: 'hc-input',
            value: draft,
            maxLength: 200,
            'aria-label': 'Box name',
            onChange: e => setDraft(e.target.value),
            onKeyDown: e => {
              if (e.key === 'Escape') onCancelRename()
            }
          }),
          jsx('button', { type: 'submit', className: 'hc-btn', disabled: busy || !draft.trim(), children: 'Save' }),
          jsx('button', { type: 'button', className: 'hc-btn', onClick: onCancelRename, children: 'Cancel' })
        ]
      })
    : jsxs('div', {
        className: 'hc-detail-head',
        children: [
          jsx('span', { className: 'hc-detail-title', children: box.label }),
          jsx('span', { className: `hc-chip ${state}`, children: STATUS_TEXT[state] || state }),
          box.kind !== 'drift'
            ? jsx('button', { type: 'button', className: 'hc-btn', disabled: busy, onClick: onStartRename, children: 'Rename' })
            : null,
          box.removed
            ? jsx('button', { type: 'button', className: 'hc-btn', disabled: busy, onClick: onRestore, children: 'Restore' })
            : jsx('button', { type: 'button', className: 'hc-btn danger', disabled: busy, onClick: onRemove, children: 'Remove' })
        ]
      })
  const output = box.check_output
    ? jsx('pre', { className: `hc-pre${box.status === 'failed' ? ' err' : ''}`, children: box.check_output.slice(-1200) })
    : null
  return jsxs('div', {
    className: 'hc-detail',
    children: [
      head,
      ...rows.map(([k, v]) =>
        jsxs('div', { children: [jsx('div', { className: 'hc-k', children: k }), v] }, k)
      ),
      output
        ? jsxs('div', {
            children: [jsx('div', { className: 'hc-k', children: `Check output (exit ${box.check_exit})` }), output]
          })
        : null
    ]
  })
}

/* ---------- the pane ---------- */

function useSessionId() {
  const stored = useValue(host.state.focusedStoredSessionId)
  const runtime = useValue(host.state.focusedSessionId)
  return { primary: stored || runtime || null, ids: [stored, runtime].filter(Boolean) }
}

function CanvasPane() {
  const session = useSessionId()
  const visible = useValue(host.paneVisibility(PANE_ID))
  const [doc, setDoc] = useState(null)
  const [pending, setPending] = useState(0)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [live, setLive] = useState(false)
  const [selected, setSelected] = useState(null)
  const [renaming, setRenaming] = useState(false)
  const [busy, setBusy] = useState(false)
  // The drag lives in a ref: pointer events can arrive before React re-renders, so handlers must
  // not read it from a render's closure. The state copy only redraws the dragged box.
  const dragRef = useRef(null) // { id, dx, dy, startX, startY, moved }
  const [drag, setDragState] = useState(null)
  const setDrag = d => {
    dragRef.current = d
    setDragState(d)
  }
  const svgRef = useRef(null)
  const boardRef = useRef(null)
  const [vertical, setVertical] = useState(true)
  const docRef = useRef(null)
  docRef.current = doc

  const sid = session.primary
  const sidRef = useRef(sid)
  sidRef.current = sid

  // Responses can arrive out of order (a poll and an edit in flight): never go back a revision.
  const accept = useCallback(canvas => {
    const current = docRef.current
    if (canvas && current && canvas.id === current.id && canvas.revision < current.revision) return false
    docRef.current = canvas
    setDoc(canvas)
    return true
  }, [])

  const load = useCallback(
    async ({ quiet, full } = {}) => {
      const target = sidRef.current
      if (!target) {
        setDoc(null)
        setLoaded(true)
        return
      }
      const current = docRef.current
      const since = !full && current && current.session_id === target ? `&since=${current.revision}` : ''
      try {
        const res = await rest(`/canvas?session_id=${encodeURIComponent(target)}${since}`)
        if (sidRef.current !== target) return
        if (res.unchanged ? true : accept(res.canvas || null)) setPending(res.pending_edits || 0)
        setError('')
      } catch (err) {
        if (!quiet) setError(errorText(err))
      } finally {
        setLoaded(true)
      }
    },
    [accept]
  )

  useEffect(() => {
    setDoc(null)
    setSelected(null)
    setRenaming(false)
    setLoaded(false)
    load()
  }, [sid, load])

  useCanvasEvents(event => {
    if (!event) {
      // poll tick
      if (visible) load({ quiet: true })
      return
    }
    setLive(true)
    const p = event.payload || {}
    const current = docRef.current
    if (session.ids.includes(p.session_id) || (current && p.canvas_id === current.id)) {
      if (!current || p.revision !== current.revision) load({ quiet: true })
    }
  })

  const send = useCallback(async ops => {
    const current = docRef.current
    if (!current) return false
    setBusy(true)
    try {
      const res = await rest('/edit', { method: 'POST', body: { canvas_id: current.id, ops } })
      if (accept(res.canvas)) setPending(res.pending_edits || 0)
      setError('')
      return true
    } catch (err) {
      const message = errorText(err)
      setError(message)
      host.notify && host.notify({ kind: 'error', message: `Canvas: ${message}` })
      // Drop optimistic changes (a dropped box) that the server did not save.
      load({ quiet: true, full: true })
      return false
    } finally {
      setBusy(false)
    }
  }, [accept, load])

  // Panes resize constantly: track the board and lay out for its shape.
  useEffect(() => {
    const el = boardRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(entries => {
      const r = entries[0] && entries[0].contentRect
      if (r && r.width > 0 && r.height > 0) setVertical(r.width < r.height * 1.1)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [doc ? doc.id : null])

  const boxes = doc ? doc.boxes : []
  const placed = useMemo(() => layout(boxes, vertical), [boxes, vertical])
  const viewRef = useRef(null) // { key, view }
  const view = useMemo(() => {
    const key = `${doc ? doc.id : ''}|${vertical}`
    const fit = bounds(placed)
    const prev = viewRef.current && viewRef.current.key === key ? viewRef.current.view : null
    const next = stickyView(prev, fit)
    viewRef.current = { key, view: next }
    return next
  }, [placed, doc ? doc.id : null, vertical])
  const posOf = id => {
    const p = placed.get(id)
    if (!p) return null
    if (drag && drag.id === id) return { ...p, x: p.x + drag.dx, y: p.y + drag.dy }
    return p
  }

  const toBoard = e => {
    const svg = svgRef.current
    if (!svg) return { x: 0, y: 0 }
    const pt = svg.createSVGPoint()
    pt.x = e.clientX
    pt.y = e.clientY
    const m = svg.getScreenCTM()
    return m ? pt.matrixTransform(m.inverse()) : { x: 0, y: 0 }
  }

  const onBoxPointerDown = (e, box) => {
    if (e.button !== 0) return
    e.stopPropagation()
    const p = toBoard(e)
    // Select on release, not here: the detail strip resizes the board, which must not happen
    // under the pointer while a drag is in progress.
    setDrag({ id: box.id, dx: 0, dy: 0, startX: p.x, startY: p.y, moved: false })
    if (svgRef.current) svgRef.current.setPointerCapture(e.pointerId)
  }

  const onPointerMove = e => {
    const d = dragRef.current
    if (!d) return
    const p = toBoard(e)
    const dx = p.x - d.startX
    const dy = p.y - d.startY
    setDrag({ ...d, dx, dy, moved: d.moved || Math.hypot(dx, dy) > 4 })
  }

  const onPointerUp = async () => {
    const d = dragRef.current
    if (!d) return
    const p = placed.get(d.id)
    if (d.moved && p) {
      const x = Math.round(p.x + d.dx)
      const y = Math.round(p.y + d.dy)
      // Keep the box where it was dropped until the saved revision arrives.
      setDoc(cur => cur && { ...cur, boxes: cur.boxes.map(b => (b.id === d.id ? { ...b, x, y } : b)) })
      setDrag(null)
      await send([{ op: 'move', id: d.id, x, y }])
      return
    }
    setDrag(null)
    setSelected(d.id)
    setRenaming(false)
  }

  const selBox = doc && selected ? doc.boxes.find(b => b.id === selected) : null

  const remove = () => selBox && send([{ op: 'remove', id: selBox.id }])
  const restore = () => selBox && send([{ op: 'restore', id: selBox.id }])
  const rename = async label => {
    if (!selBox) return
    const clean = label.trim()
    if (!clean || clean === selBox.label) {
      setRenaming(false)
      return
    }
    if (await send([{ op: 'rename', id: selBox.id, label: clean }])) setRenaming(false)
  }
  const answer = (box, option) => send([{ op: 'answer', id: box.id, answer: option }])

  const onKeyDown = e => {
    if (!selBox || renaming || busy) return
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault()
      if (!selBox.removed) remove()
    } else if (e.key === 'Enter' || e.key === 'F2') {
      e.preventDefault()
      if (selBox.kind !== 'drift') setRenaming(true)
    } else if (e.key === 'Escape') {
      setSelected(null)
    }
  }

  if (!sid || (loaded && !doc)) {
    return jsxs('div', {
      className: 'hc-root',
      children: [
        jsx(Style, {}),
        jsx('div', {
          className: 'hc-board',
          children: jsxs('div', {
            className: 'hc-empty',
            children: [
              jsxs('div', {
                children: [
                  jsx('b', { children: 'No canvas in this chat yet' }),
                  'Ask Hermes to plan the work first, for example: "Plan it on the canvas, then build it."',
                  error ? jsx('div', { className: 'hc-error', children: error }) : null
                ]
              })
            ]
          })
        })
      ]
    })
  }

  if (!doc) {
    return jsxs('div', {
      className: 'hc-root',
      children: [jsx(Style, {}), jsx('div', { className: 'hc-board', children: jsx('div', { className: 'hc-empty', children: 'Loading canvas…' }) })]
    })
  }

  const progress = progressOf(doc)
  const edges = []
  for (const box of doc.boxes) {
    for (const dep of box.after || []) {
      const a = posOf(dep)
      const b = posOf(box.id)
      const src = doc.boxes.find(x => x.id === dep)
      if (a && b && src) {
        edges.push(jsx(Edge, { from: { ...a, id: dep }, to: { ...b, id: box.id }, ok: src.status === 'done' }, `${dep}>${box.id}`))
      }
    }
  }

  return jsxs('div', {
    className: 'hc-root',
    tabIndex: 0,
    onKeyDown,
    children: [
      jsx(Style, {}),
      jsxs('div', {
        className: 'hc-head',
        children: [
          jsx('span', { className: `hc-live${live ? '' : ' off'}`, title: live ? 'Live updates' : 'Polling for updates' }),
          jsx('span', { className: 'hc-title', title: doc.title, children: doc.title }),
          jsx('span', { className: 'hc-meta', children: `${progress.done}/${progress.total} done · rev ${doc.revision}` })
        ]
      }),
      jsxs('div', {
        className: 'hc-board',
        ref: boardRef,
        children: [
          jsxs('svg', {
            ref: svgRef,
            viewBox: `${f1(view.x)} ${f1(view.y)} ${f1(view.w)} ${f1(view.h)}`,
            preserveAspectRatio: 'xMidYMid meet',
            role: 'img',
            'aria-label': `Canvas: ${doc.title}`,
            onPointerMove,
            onPointerUp,
            onPointerCancel: () => setDrag(null),
            onPointerDown: () => {
              setSelected(null)
              setRenaming(false)
            },
            children: [
              jsx('defs', {
                children: jsx('pattern', {
                  id: 'hc-dots',
                  width: 22,
                  height: 22,
                  patternUnits: 'userSpaceOnUse',
                  children: jsx('circle', { className: 'hc-dots', cx: 1.5, cy: 1.5, r: 1.2 })
                })
              }),
              jsx('rect', { x: view.x - 2000, y: view.y - 2000, width: view.w + 4000, height: view.h + 4000, fill: 'url(#hc-dots)' }),
              jsx('g', { children: edges }),
              jsx('g', {
                children: doc.boxes.map(box => {
                  const pos = posOf(box.id)
                  if (!pos) return null
                  return jsx(
                    BoxEl,
                    {
                      box,
                      pos,
                      selected: box.id === selected,
                      onPointerDown: onBoxPointerDown,
                      onDoubleClick: b => {
                        setSelected(b.id)
                        if (b.kind !== 'drift') setRenaming(true)
                      },
                      onAnswer: answer
                    },
                    box.id
                  )
                })
              })
            ]
          })
        ]
      }),
      selBox
        ? jsx(Detail, {
            box: selBox,
            renaming,
            busy,
            onRename: rename,
            onCancelRename: () => setRenaming(false),
            onStartRename: () => setRenaming(true),
            onRemove: remove,
            onRestore: restore
          })
        : null,
      jsxs('div', {
        className: 'hc-foot',
        children: [
          jsx('span', { className: 'hc-src', children: 'Box status comes from hooks, not from the model.' }),
          pending
            ? jsx('span', { children: `${pending} edit${pending === 1 ? '' : 's'} waiting for the agent` })
            : null,
          error ? jsx('span', { className: 'hc-error', children: error }) : null
        ]
      })
    ]
  })
}

/* ---------- the chat card ---------- */

function CanvasCard({ attrs }) {
  const id = attrs && typeof attrs.id === 'string' ? attrs.id : ''
  const valid = CANVAS_ID_RE.test(id)
  const [doc, setDoc] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    if (!valid) return
    try {
      const res = await rest(`/canvas?id=${encodeURIComponent(id)}`)
      setDoc(res.canvas || null)
      setError(res.canvas ? '' : 'This canvas no longer exists.')
    } catch (err) {
      setError(errorText(err))
    }
  }, [id, valid])
  useEffect(() => {
    load()
  }, [load])
  useCanvasEvents(event => {
    if (event && event.payload && event.payload.canvas_id === id) load()
  })
  if (!valid) return null
  if (!doc) {
    return jsxs('div', {
      className: 'hc-root hc-card',
      children: [jsx(Style, {}), jsx('span', { className: error ? 'hc-error' : 'hc-meta', children: error || 'Loading canvas…' })]
    })
  }
  const progress = progressOf(doc)
  const pct = progress.total ? Math.round((progress.done / progress.total) * 100) : 0
  const parts = doc.boxes.filter(b => b.kind !== 'drift')
  return jsxs('div', {
    className: 'hc-root hc-card',
    children: [
      jsx(Style, {}),
      jsxs('div', {
        className: 'hc-card-head',
        children: [
          jsx('span', { className: 'hc-title', children: doc.title }),
          jsx('span', { className: 'hc-meta', children: `${progress.done}/${progress.total} done` })
        ]
      }),
      jsx('div', { className: 'hc-bar', children: jsx('i', { style: { width: `${pct}%` } }) }),
      jsx('div', {
        className: 'hc-chips',
        children: parts.map(b => {
          const state = boxState(b)
          return jsx('span', { className: `hc-chip ${state}`, title: STATUS_TEXT[state] || state, children: b.label }, b.id)
        })
      }),
      jsx('span', { className: 'hc-meta', children: 'Open the Canvas pane to steer the plan.' })
    ]
  })
}

/* ---------- registration ---------- */

export default {
  id: PLUGIN_ID,
  name: 'Hermes Canvas',
  register(ctx) {
    pluginCtx = ctx
    ctx.onEvent(CHANGED_EVENT, event => notifyViews(event))
    ctx.setInterval(() => notifyViews(null), POLL_MS)
    ctx.onDispose(() => {
      subscribers.clear()
      pluginCtx = null
    })
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Canvas',
      data: { placement: 'right', width: '420px' },
      render: () => jsx(CanvasPane, {})
    })
    ctx.register({
      id: 'card',
      area: TRANSCRIPT_DIRECTIVE_AREA,
      data: { name: PLUGIN_ID, render: props => jsx(CanvasCard, props) }
    })
  }
}
