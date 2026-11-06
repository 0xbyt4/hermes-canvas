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
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
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

// Diagonal sketch lines inside a box, for work in progress.
function hatchPath(id, w, h, step = 10, pad = 5) {
  const r = mulberry(hash(id + 'hatch'))
  let d = ''
  for (let k = -h; k < w; k += step) {
    const lo = Math.max(pad, k + h - (w - pad))
    const hi = Math.min(h - pad, k + h - pad)
    if (hi - lo < 6) continue
    d += sline(r, k + h - lo, lo, k + h - hi, hi, 0.8)
  }
  return d
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
// in rows (top to bottom) on a tall one, so boxes stay readable. A box sits one step before the
// first box that waits for it, so a box with no dependencies is drawn next to its user, not far
// to the left. A box the user moved keeps its own x/y, but still holds its slot, so moving one box
// never shifts the others.
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
  const users = new Map() // box id -> the boxes that wait for it
  for (const box of boxes) {
    for (const a of box.after || []) if (byId.has(a)) users.set(a, [...(users.get(a) || []), box.id])
  }
  // Latest level: users first (they are deeper), then each box one level before its nearest user.
  const levels = new Map()
  const order = boxes.filter(b => b.kind !== 'drift').sort((a, b) => depthOf(b.id) - depthOf(a.id))
  for (const box of order) {
    const deps = (users.get(box.id) || []).filter(id => levels.has(id))
    const latest = deps.length ? Math.min(...deps.map(id => levels.get(id))) - 1 : depthOf(box.id)
    levels.set(box.id, box.kind === 'question' ? 0 : Math.max(depthOf(box.id), latest))
  }
  const rows = new Map()
  const auto = new Map()
  const driftRow = []
  for (const box of boxes) {
    if (box.kind === 'drift') {
      driftRow.push(box)
      continue
    }
    const level = levels.get(box.id)
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

const CAMERA_MS = 650
const FOCUS_MS = 2600
// The camera holds still while the pointer moves on the board, and this long after it stops.
const HOLD_MS = 1500

// A view centered on one box, with the fit view's aspect, or null when zooming would not help.
function focusView(fit, p) {
  if (!p) return null
  const w = Math.max(BOX.w * 3.2, fit.w * 0.55)
  if (w >= fit.w * 0.92) return null
  const h = (w * fit.h) / fit.w
  return { x: p.x + p.w / 2 - w / 2, y: p.y + p.h / 2 - h / 2, w, h }
}

const easeInOut = t => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2)

function mixView(a, b, t) {
  return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, w: a.w + (b.w - a.w) * t, h: a.h + (b.h - a.h) * t }
}

function sameView(a, b) {
  return Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5 && Math.abs(a.w - b.w) < 0.5 && Math.abs(a.h - b.h) < 0.5
}

const reducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches

// Eases the shown view toward `target`. Returns the view to draw.
function useCamera(target) {
  const [cam, setCam] = useState(target)
  const camRef = useRef(target)
  useEffect(() => {
    const from = camRef.current
    if (sameView(from, target)) return
    if (reducedMotion()) {
      camRef.current = target
      setCam(target)
      return
    }
    let raf = 0
    const start = performance.now()
    const tick = now => {
      const t = Math.min(1, (now - start) / CAMERA_MS)
      const v = mixView(from, target, easeInOut(t))
      camRef.current = v
      setCam(v)
      if (t < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [target.x, target.y, target.w, target.h])
  return cam
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

const STATUS_TEXT = { todo: 'planned', doing: 'in progress', done: 'done', failed: 'failing', removed: 'removed' }

function lastLine(text) {
  const lines = String(text || '')
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
  return lines.length ? lines[lines.length - 1] : ''
}

// The line of a failing check that says what failed: an error line first, then a FAIL line,
// else the last line (test runners often end with a timing summary).
function failLine(text) {
  const lines = String(text || '')
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
  return (
    lines.find(l => /^[\w.]*(Error|Exception)\b:?/.test(l) || /^E\s{2,}/.test(l)) ||
    lines.find(l => /\b(FAIL|FAILED)\b|^[×✗]/.test(l)) ||
    lastLine(text)
  )
}

function revealPane() {
  if (typeof host.revealPane === 'function') host.revealPane(PANE_ID)
}

// One short line on where the build stands, for the card and the pane header.
function statusLine(doc) {
  const parts = doc.boxes.filter(b => b.kind === 'component' && !b.removed)
  const failed = parts.filter(b => b.status === 'failed').length
  const doing = parts.filter(b => b.status === 'doing').length
  const open = doc.boxes.filter(b => b.kind === 'question' && !b.removed && !b.answer).length
  const drift = doc.boxes.filter(b => b.kind === 'drift' && !b.removed).length
  if (parts.length && parts.every(b => b.status === 'done')) return { text: 'Shipped: every box passed its check', tone: 'done' }
  const out = []
  if (open) out.push(`${open} question${open === 1 ? '' : 's'} to answer`)
  if (failed) out.push(`${failed} failing`)
  if (doing) out.push(`${doing} in progress`)
  if (drift) out.push(`${drift} file${drift === 1 ? '' : 's'} outside the plan`)
  return { text: out.join(' · ') || 'Planned', tone: failed ? 'failed' : open ? 'ask' : '' }
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
.hc-chip.failed { color: var(--hc-fail); border-color: var(--hc-fail); }
.hc-chip.removed { color: var(--hc-muted); text-decoration: line-through; opacity: 0.7; }
.hc-k { font-size: 10px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--hc-muted); }
.hc-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
.hc-pre { margin: 0; padding: 6px 8px; border-radius: 6px; background: var(--ui-bg-tertiary); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 160px; overflow: auto; }
.hc-pre.err { color: var(--hc-fail); }
.hc-btn { font: inherit; font-size: 11.5px; color: var(--hc-ink); background: transparent; border: 1px solid var(--hc-line); border-radius: 6px; padding: 3px 9px; cursor: pointer; white-space: nowrap; }
.hc-btn:hover:not(:disabled) { border-color: var(--hc-muted); }
.hc-btn:disabled { opacity: .45; cursor: not-allowed; }
.hc-btn.danger { color: var(--hc-fail); }
.hc-input { font: inherit; font-family: var(--hc-hand); font-size: 15px; color: var(--hc-ink); background: var(--hc-paper); border: 1px solid var(--hc-accent); border-radius: 6px; padding: 2px 6px; flex: 1; min-width: 80px; }
.hc-diff { margin: 0; border-radius: 6px; background: var(--ui-bg-tertiary); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px; line-height: 1.45; max-height: 220px; overflow: auto; padding: 4px 0; }
.hc-diff div { white-space: pre-wrap; overflow-wrap: anywhere; padding: 0 8px; }
.hc-diff .add { color: var(--hc-done); background: color-mix(in srgb, var(--hc-done) 10%, transparent); }
.hc-diff .del { color: var(--hc-fail); background: color-mix(in srgb, var(--hc-fail) 10%, transparent); }
.hc-diff .hunk { color: var(--hc-muted); }
.hc-file { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
.hc-notetag rect { fill: color-mix(in srgb, var(--hc-note) 70%, var(--hc-paper)); stroke: color-mix(in srgb, var(--hc-note) 60%, var(--hc-ink)); stroke-width: 1; }
.hc-notetag text { font-family: var(--hc-hand); font-size: 11px; font-weight: 700; fill: var(--hc-ink); }
.hc-note-text { font-family: var(--hc-hand); font-size: 14px; padding: 4px 8px; border-radius: 6px; background: color-mix(in srgb, var(--hc-note) 22%, transparent); overflow-wrap: anywhere; }
.hc-textarea { font: inherit; font-family: var(--hc-hand); font-size: 14px; color: var(--hc-ink); background: var(--hc-paper); border: 1px solid var(--hc-accent); border-radius: 6px; padding: 4px 6px; width: 100%; box-sizing: border-box; resize: vertical; min-height: 48px; }
.hc-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.hc-ask { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.hc-ask-q { font-family: var(--hc-hand); font-size: 14px; font-weight: 700; margin-right: 4px; }
.hc-foot { display: flex; align-items: center; gap: 10px; padding: 6px 10px; border-top: 1px solid var(--hc-line); color: var(--hc-muted); font-size: 11px; flex-wrap: wrap; }
.hc-foot .hc-src { font-style: italic; flex: 1; min-width: 0; }
.hc-error { color: var(--hc-fail); }
.hc-card { margin: 4px 0; border: 1px solid var(--hc-line); border-radius: 10px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; max-width: 520px; }
.hc-card-head { display: flex; align-items: center; gap: 8px; }
.hc-bar { height: 5px; border-radius: 999px; background: var(--ui-bg-tertiary); overflow: hidden; }
.hc-bar i { display: block; height: 100%; background: var(--hc-done); }
.hc-status { font-size: 11.5px; color: var(--hc-muted); }
.hc-status.done { color: var(--hc-done); font-weight: 600; }
.hc-status.failed { color: var(--hc-fail); font-weight: 600; }
.hc-status.ask { color: color-mix(in srgb, var(--hc-note) 70%, var(--hc-ink)); font-weight: 600; }
.hc-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.hc-hatch { fill: none; stroke: var(--hc-doing); stroke-width: 1.3; stroke-linecap: round; opacity: .32; stroke-dasharray: 7 4; animation: hc-hatch 1.4s linear infinite; }
.hc-comet circle { fill: var(--hc-done); }
.hc-pop { transform-box: fill-box; transform-origin: center; animation: hc-pop .5s ease-out; }
.hc-follow.on { color: var(--hc-accent); border-color: var(--hc-accent); }
.hc-finale { position: absolute; inset: 0; pointer-events: none; overflow: hidden; display: grid; place-items: center; }
.hc-finale i { position: absolute; top: -12px; width: 7px; height: 11px; border-radius: 1px; opacity: 0; animation-name: hc-fall; animation-timing-function: cubic-bezier(.3,.6,.5,1); animation-fill-mode: forwards; }
.hc-finale b { font-family: var(--hc-hand); font-size: 44px; color: var(--hc-done); transform: rotate(-6deg); text-shadow: 0 2px 0 var(--hc-paper); animation: hc-stamp .6s cubic-bezier(.2,1.6,.4,1) both; }
.hc-born .hc-ink { stroke-dasharray: 1; stroke-dashoffset: 1; animation: hc-draw .75s ease-out var(--hc-delay, 0s) forwards; }
.hc-born .hc-fill, .hc-born text, .hc-born .hc-opt, .hc-born .hc-notetag { opacity: 0; animation: hc-fadein .35s ease-out calc(var(--hc-delay, 0s) + .4s) forwards; }
.hc-edge.hc-born-edge { stroke-dasharray: 1; stroke-dashoffset: 1; animation: hc-draw .5s ease-out var(--hc-delay, 0s) forwards; }
.hc-arrow.hc-born-edge { opacity: 0; animation: hc-fadein .2s ease-out calc(var(--hc-delay, 0s) + .45s) forwards; }
.hc-agent { pointer-events: none; transition: transform .7s cubic-bezier(.3,.7,.3,1); }
.hc-agent.idle { opacity: .45; }
.hc-agent-arrow { stroke: var(--hc-paper); stroke-width: 1.4; stroke-linejoin: round; }
.hc-agent-name { font-family: ui-sans-serif, system-ui, sans-serif; font-size: 10.5px; font-weight: 700; fill: #fff; }
.hc-bubble { animation: hc-fadein .25s ease-out both; }
.hc-bubble rect { fill: var(--hc-paper); stroke-width: 1.3; }
.hc-bubble text { font-family: var(--hc-hand); font-size: 12.5px; fill: var(--hc-ink); }
.hc-ripple { fill: none; stroke-width: 2; transform-box: fill-box; transform-origin: center; animation: hc-ripple .8s ease-out forwards; }
.hc-gate { position: absolute; left: 50%; transform: translateX(-50%); max-width: calc(100% - 24px); box-sizing: border-box; font-size: 11.5px; font-weight: 600; padding: 4px 12px; border-radius: 999px; color: var(--hc-fail); background: color-mix(in srgb, var(--hc-fail) 12%, var(--hc-paper)); border: 1px solid var(--hc-fail); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none; animation: hc-fadein .3s ease-out both; }
.hc-link-line { fill: none; stroke: var(--hc-accent); stroke-width: 2; stroke-dasharray: 6 5; pointer-events: none; }
.hc-link-head { fill: var(--hc-accent); pointer-events: none; }
.hc-link-target { fill: none; stroke: var(--hc-accent); stroke-width: 2.2; pointer-events: none; }
.hc-linking .hc-el { cursor: crosshair; }
.hc-dep { display: inline-flex; align-items: center; gap: 4px; margin: 2px 6px 2px 0; padding: 1px 4px 1px 8px; border: 1px solid var(--hc-line); border-radius: 999px; font-size: 11px; }
.hc-dep button { font: inherit; border: 0; background: transparent; color: var(--hc-muted); cursor: pointer; padding: 0 3px; line-height: 1; }
.hc-dep button:hover:not(:disabled) { color: var(--hc-fail); }
.hc-edits { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.hc-edits li { display: flex; align-items: baseline; gap: 6px; font-size: 11.5px; min-width: 0; }
.hc-edit-mark { width: 12px; flex: none; text-align: center; font-weight: 700; }
.hc-edits li.got .hc-edit-mark { color: var(--hc-done); }
.hc-edits li.wait .hc-edit-mark { color: color-mix(in srgb, var(--hc-note) 70%, var(--hc-ink)); }
.hc-edit-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hc-edit-state { color: var(--hc-muted); font-size: 10.5px; flex: none; }
.hc-edits-panel { border-top: 1px solid var(--hc-line); padding: 6px 10px; max-height: 32%; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.hc-textbtn { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; text-decoration: underline dotted; text-underline-offset: 2px; }
.hc-history { border-top: 1px solid var(--hc-line); padding: 6px 10px; display: flex; flex-direction: column; gap: 4px; }
.hc-range { flex: 1; min-width: 80px; accent-color: var(--hc-accent); }
.hc-past { position: absolute; top: 8px; left: 50%; transform: translateX(-50%); font-size: 11px; padding: 2px 10px; border-radius: 999px; background: var(--hc-accent); color: var(--hc-paper); pointer-events: none; }
.hc-head .hc-btn { padding: 2px 7px; font-size: 11px; }
@keyframes hc-fall { 0% { opacity: 1; transform: translate(0, 0) rotate(0); } 100% { opacity: 0; transform: translate(var(--hc-drift), 105%) translateY(420px) rotate(var(--hc-rot)); } }
@keyframes hc-stamp { 0% { opacity: 0; transform: rotate(-6deg) scale(2.2); } 100% { opacity: 1; transform: rotate(-6deg) scale(1); } }
@keyframes hc-spin { to { transform: rotate(360deg); } }
@keyframes hc-hatch { to { stroke-dashoffset: -22; } }
@keyframes hc-draw { to { stroke-dashoffset: 0; } }
@keyframes hc-fadein { from { opacity: 0; } to { opacity: 1; } }
@keyframes hc-ripple { 0% { opacity: .9; transform: scale(1); } 100% { opacity: 0; transform: scale(4.5); } }
@keyframes hc-pop { 0% { transform: scale(1); } 35% { transform: scale(1.07); } 100% { transform: scale(1); } }
@media (prefers-reduced-motion: reduce) { .hc-ring, .hc-hatch, .hc-pop, .hc-bubble, .hc-gate { animation: none; } .hc-comet, .hc-finale i, .hc-ripple { display: none; } .hc-finale b { animation: none; }
  .hc-born .hc-ink, .hc-born .hc-fill, .hc-born text, .hc-born .hc-opt, .hc-born .hc-notetag, .hc-born-edge { animation: none; opacity: 1; stroke-dasharray: none; stroke-dashoffset: 0; }
  .hc-agent { transition: none; } }
`

function Style() {
  return jsx('style', { children: STYLE })
}

/* ---------- board pieces ---------- */

function edgeGeometry(from, to) {
  const a = edgePoint(from, to)
  const b = edgePoint(to, from)
  const r = mulberry(hash(`${from.id}>${to.id}`))
  return { a, b, d: sline(r, a.x, a.y, b.x, b.y, 1.2) }
}

// A short light that runs along an edge when the box it starts from is done.
const COMET_MS = 900
const COMET_TRAIL = [0, 0.06, 0.12]

function Comet({ d }) {
  const pathRef = useRef(null)
  const [t, setT] = useState(0)
  useEffect(() => {
    let raf = 0
    const begin = performance.now()
    const tick = now => {
      const v = Math.min(1, (now - begin) / COMET_MS)
      setT(v)
      if (v < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])
  const path = pathRef.current
  const len = path ? path.getTotalLength() : 0
  const dots = len
    ? COMET_TRAIL.map((lag, i) => {
        const at = easeInOut(Math.max(0, t - lag) / (1 - lag)) * len
        const pt = path.getPointAtLength(at)
        return jsx('circle', { cx: pt.x, cy: pt.y, r: 4.6 - i * 1.4, opacity: (1 - i * 0.32) * (t < 1 ? 1 : 0) }, i)
      })
    : []
  return jsxs('g', {
    className: 'hc-comet',
    children: [jsx('path', { ref: pathRef, d, fill: 'none', stroke: 'none' }), ...dots]
  })
}

function Edge({ from, to, ok, born }) {
  const { b, d, a } = edgeGeometry(from, to)
  const ang = Math.atan2(b.y - a.y, b.x - a.x)
  const head = [
    [b.x, b.y],
    [b.x - 11 * Math.cos(ang - 0.4), b.y - 11 * Math.sin(ang - 0.4)],
    [b.x - 11 * Math.cos(ang + 0.4), b.y - 11 * Math.sin(ang + 0.4)]
  ]
    .map(p => p.map(f1).join(','))
    .join(' ')
  const drawn = born === undefined ? '' : ' hc-born-edge'
  return jsxs('g', {
    style: born === undefined ? undefined : { '--hc-delay': `${born}s` },
    children: [
      jsx('path', { className: `hc-edge${ok ? ' ok' : ''}${drawn}`, d, pathLength: born === undefined ? undefined : 1 }),
      jsx('polygon', { className: `hc-arrow${ok ? ' ok' : ''}${drawn}`, points: head })
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

// Hermes names subagents `sa-<index>-<hex>`. The index restarts at 0 for each delegate_task call,
// so it repeats. The hex suffix is unique: show its first four characters.
function ownerName(owner) {
  const m = /^sa-\d+-([0-9a-f]{4})/.exec(owner || '')
  return m ? `subagent ${m[1]}` : owner
}

function clip(text, max) {
  return text.length > max ? text.slice(0, max - 1) + '…' : text
}

function clipStart(text, max) {
  return text.length > max ? '…' + text.slice(1 - max) : text
}

function subLine(box) {
  if (box.removed) {
    return box.writes ? `removed · ${box.writes} write${box.writes === 1 ? '' : 's'}` : 'removed from the plan'
  }
  if (box.kind === 'drift') return 'written outside every box'
  if (box.owner) return `${ownerName(box.owner)} · ${box.writes} write${box.writes === 1 ? '' : 's'}`
  if (box.writes) return `${box.writes} write${box.writes === 1 ? '' : 's'}`
  if (box.check) return box.check
  return (box.files || []).join(', ')
}

// `born` is the draw-on delay in seconds for a box that just appeared, else undefined.
function BoxEl({ box, pos, selected, popped, born, onPointerDown, onDoubleClick, onAnswer }) {
  const { w, h } = pos
  const state = boxState(box)
  const drawing = born !== undefined
  const cls = [
    'hc-el',
    `hc-${state}`,
    box.kind === 'question' ? 'hc-question' : '',
    box.kind === 'drift' ? 'hc-drift' : '',
    drawing ? 'hc-born' : ''
  ]
  const children = [
    jsx('rect', { className: 'hc-fill', x: 2, y: 2, width: w - 4, height: h - 4, rx: 6 }, 'fill'),
    box.status === 'doing' && !box.removed && box.kind === 'component'
      ? jsx('path', { className: 'hc-hatch', d: hatchPath(box.id, w, h) }, 'hatch')
      : null,
    // pathLength turns the sketch into one stroke of length 1 for the draw-on dash.
    jsx('path', { className: 'hc-ink', d: srect(hash(box.id), 0, 0, w, h), pathLength: drawing ? 1 : undefined }, 'ink')
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
    children.push(
      jsx('text', { x: 12, y: 24, fontSize: 14, className: 'hc-err', children: box.revert_asked ? 'drift · undo asked' : 'drift' }, 'tag')
    )
    children.push(
      jsx('text', { x: 12, y: 44, className: 'hc-sub', children: clipStart(box.files[0] || box.label, 34) }, 'path')
    )
  } else {
    children.push(jsx(BoxLabel, { box, w, top: 34, size: 17, max: 18 }, 'label'))
    children.push(
      jsx('text', { x: w / 2, y: h - 10, textAnchor: 'middle', className: 'hc-sub', children: clip(subLine(box), 28) }, 'sub')
    )
  }
  children.push(jsx(Badge, { box, w }, 'badge'))
  if (box.note && !box.removed) {
    children.push(
      jsxs(
        'g',
        {
          className: 'hc-notetag',
          transform: 'translate(-8 -11) rotate(-4)',
          children: [jsx('rect', { width: 40, height: 17, rx: 2 }), jsx('text', { x: 20, y: 12.5, textAnchor: 'middle', children: 'note' })]
        },
        'note'
      )
    )
  }
  if (box.removed) children.push(jsx('path', { className: 'hc-strike', d: scribble(box.id, w, h) }, 'strike'))
  if (box.status === 'failed' && !box.removed && box.kind === 'component') {
    const err = failLine(box.check_output) || `exit ${box.check_exit}`
    const short = err.length > 32 ? err.slice(0, 31) + '…' : err
    children.push(jsx('text', { x: 2, y: h + 16, className: 'hc-err', children: '✗ ' + short }, 'err'))
  }
  if (selected) {
    children.push(jsx('rect', { className: 'hc-sel', x: -6, y: -6, width: w + 12, height: h + 12, rx: 8 }, 'sel'))
  }
  return jsx('g', {
    className: cls.filter(Boolean).join(' '),
    transform: `translate(${f1(pos.x)} ${f1(pos.y)})`,
    style: drawing ? { '--hc-delay': `${born}s` } : undefined,
    onPointerDown: e => onPointerDown(e, box),
    onDoubleClick: () => onDoubleClick(box),
    'data-box': box.id,
    // A CSS transform on this group would replace its translate, so the pop runs on an inner group.
    children: jsx('g', { className: popped ? 'hc-pop' : undefined, children }, popped || 'still')
  })
}

// The arrow the user is drawing in link mode. It snaps to the target box when over one.
function LinkDraft({ from, to, x, y }) {
  if (!from) return null
  const end = to ? edgePoint(to, from) : { x, y }
  const start = edgePoint(from, to || { x: x - 0.5, y: y - 0.5, w: 1, h: 1 })
  const ang = Math.atan2(end.y - start.y, end.x - start.x)
  const head = [
    [end.x, end.y],
    [end.x - 11 * Math.cos(ang - 0.4), end.y - 11 * Math.sin(ang - 0.4)],
    [end.x - 11 * Math.cos(ang + 0.4), end.y - 11 * Math.sin(ang + 0.4)]
  ]
    .map(p => p.map(f1).join(','))
    .join(' ')
  return jsxs('g', {
    children: [
      to ? jsx('rect', { className: 'hc-link-target', x: to.x - 5, y: to.y - 5, width: to.w + 10, height: to.h + 10, rx: 8 }) : null,
      jsx('path', { className: 'hc-link-line', d: `M${f1(start.x)} ${f1(start.y)} L${f1(end.x)} ${f1(end.y)}` }),
      jsx('polygon', { className: 'hc-link-head', points: head })
    ]
  })
}

/* ---------- agents: who did the latest real work (the hooks' activity log) ---------- */

const MAIN_ACTOR = 'main'
const BUBBLE_MS = 4000
const IDLE_REVISIONS = 10

function actorName(actor) {
  return actor === MAIN_ACTOR ? 'Hermes' : ownerName(actor)
}

function actorColor(actor) {
  return actor === MAIN_ACTOR ? 'var(--hc-accent)' : `hsl(${hash(actor) % 360} 60% 44%)`
}

function baseName(path) {
  const parts = String(path || '').split('/')
  return parts[parts.length - 1] || String(path || '')
}

function activityText(a) {
  const boxes = a.boxes || []
  switch (a.kind) {
    case 'start':
      return 'took this box'
    case 'write': {
      // The hooks keep two file names and count the rest: "a.py, b.py +3".
      const m = /^(.*?)( \+\d+)?$/.exec(String(a.detail || ''))
      const files = m[1].split(', ').filter(Boolean)
      return `wrote ${files.map(baseName).join(', ')}${m[2] || ''}`
    }
    case 'check':
      return a.detail === 'exit 0' ? 'check passed' : `check failed (${a.detail})`
    case 'veto':
      return `write blocked: ${baseName(a.detail)}`
    case 'gate':
      return `cannot finish: ${boxes.length} box${boxes.length === 1 ? '' : 'es'} not passing`
    default:
      return ''
  }
}

// Each agent at its latest activity. A subagent that stopped leaves the canvas.
function agentsOf(doc) {
  const latest = new Map()
  for (const a of doc.activity || []) latest.set(a.actor, a)
  const out = []
  latest.forEach(a => {
    if (a.kind === 'stop') return
    const box = (a.boxes || []).map(id => doc.boxes.find(b => b.id === id)).find(Boolean)
    if (box) out.push({ actor: a.actor, entry: a, box: box.id, idle: doc.revision - a.revision > IDLE_REVISIONS })
  })
  return out
}

// The newest finish hold whose agent still runs and that still holds a box that has not passed.
// A subagent's run ends with its stop entry. The main agent's run is its current turn, which only
// the host knows: `mainSince` is the revision the turn started at, or null between turns.
function gateOf(doc, mainSince) {
  const acts = doc.activity || []
  for (let i = acts.length - 1; i >= 0; i--) {
    const entry = acts[i]
    if (entry.kind !== 'gate') continue
    const ended =
      entry.actor === MAIN_ACTOR
        ? mainSince === null || entry.revision <= mainSince
        : acts.slice(i + 1).some(a => a.actor === entry.actor && a.kind === 'stop')
    if (ended) continue
    const held = (entry.boxes || []).map(id => doc.boxes.find(b => b.id === id)).filter(b => b && !b.removed && b.status !== 'done')
    if (held.length) return { entry, held }
  }
  return null
}

const AGENT_ARROW = 'M0 0 L0 17 L4.5 12.5 L8 20 L11 18.6 L7.6 11.4 L13.5 11.4 Z'

function AgentMarker({ agent, pos, slot, bubble, ripple }) {
  const color = actorColor(agent.actor)
  const name = actorName(agent.actor)
  const nameW = name.length * 6.4 + 12
  const say = bubble ? clip(activityText(agent.entry), 34) : ''
  const sayW = say.length * 5.7 + 18
  // At the box's lower-left corner, the bubble under the name: both stay near the box's own
  // width, so they fit in the view margin. A second agent on the same box sits to the right.
  const x = pos.x + 12 + slot * 96
  const y = pos.y + pos.h - 8
  return jsxs('g', {
    className: `hc-agent${agent.idle ? ' idle' : ''}`,
    style: { transform: `translate(${f1(x)}px, ${f1(y)}px)` },
    'aria-label': `${name}: ${activityText(agent.entry)}`,
    children: [
      ripple ? jsx('circle', { className: 'hc-ripple', r: 4, style: { stroke: color } }, `r${agent.entry.revision}`) : null,
      jsx('path', { className: 'hc-agent-arrow', d: AGENT_ARROW, style: { fill: color } }),
      jsxs('g', {
        transform: 'translate(12 16)',
        children: [
          jsx('rect', { width: nameW, height: 16, rx: 8, style: { fill: color } }),
          jsx('text', { className: 'hc-agent-name', x: nameW / 2, y: 11.5, textAnchor: 'middle', children: name })
        ]
      }),
      say
        ? jsxs(
            'g',
            {
              className: 'hc-bubble',
              transform: 'translate(12 36)',
              children: [
                jsx('rect', { width: sayW, height: 22, rx: 9, style: { stroke: color } }),
                jsx('text', { x: 8, y: 15.5, children: say })
              ]
            },
            `b${agent.entry.revision}`
          )
        : null
    ]
  })
}

/* ---------- answering a question ---------- */

// Is the agent of this chat in a turn? busyBySession is keyed by runtime ids; a card only knows
// the stored id, which matches when its chat is the focused one.
function sessionBusy(ids) {
  const map = host.state.busyBySession.get() || {}
  if (ids.some(id => map[id])) return true
  const focused = [host.state.focusedStoredSessionId.get(), host.state.focusedSessionId.get()]
  return ids.some(id => id && focused.includes(id)) && Boolean(host.state.busy.get())
}

// After an answer, send the turn for the user when the agent is idle, so the build goes on.
// A busy agent gets the answer on its next tool result instead (transform_tool_result).
function continueTurn(ids, box, option) {
  if (!ids.length || sessionBusy(ids)) return
  const text = `${box.label} → ${option}. Continue with the plan on the canvas.`
  if (!host.composer.submit(ids[0], text)) {
    host.notify && host.notify({ kind: 'info', message: 'Answer saved. Send a message to continue the build.' })
  }
}

/* ---------- detail strip for the selected box ---------- */

const DIFF_STATUS = {
  modified: 'changed',
  added: 'new file',
  unchanged: 'no change since the last commit',
  missing: 'deleted or not found',
  outside: 'outside the project: not shown',
  binary: 'binary file: not shown',
  too_large: 'too large to show',
  error: 'no diff'
}

function diffLineClass(line) {
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return ''
}

function DiffBody({ text }) {
  // Skip git's file header (everything before the first hunk): the file name is shown above.
  const all = text.split('\n')
  const first = all.findIndex(l => l.startsWith('@@'))
  const lines = first < 0 ? all : all.slice(first)
  if (lines.length && lines[lines.length - 1] === '') lines.pop()
  return jsx('div', {
    className: 'hc-diff',
    children: lines.map((line, i) => jsx('div', { className: diffLineClass(line), children: line || ' ' }, i))
  })
}

// Loads the git diff of the files the box wrote. Reloads when the box gets a new write.
function Changes({ canvasId, box }) {
  const [open, setOpen] = useState(false)
  const [state, setState] = useState({ key: '', files: null, error: '' })
  const key = `${canvasId}|${box.id}|${box.writes}`
  useEffect(() => {
    if (!open || state.key === key) return
    let alive = true
    rest(`/diff?id=${encodeURIComponent(canvasId)}&box=${encodeURIComponent(box.id)}`)
      .then(res => alive && setState({ key, files: res.files || [], error: '' }))
      .catch(err => alive && setState({ key, files: null, error: errorText(err) }))
    return () => {
      alive = false
    }
  }, [open, key, canvasId, box.id, state.key])
  useEffect(() => {
    setOpen(false)
  }, [box.id])
  if (!box.written || !box.written.length) return null
  const loading = open && state.key !== key
  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'hc-file',
        children: [
          jsx('div', { className: 'hc-k', style: { flex: 1 }, children: 'Changes' }),
          jsx('button', {
            type: 'button',
            className: 'hc-btn',
            onClick: () => setOpen(v => !v),
            children: open ? 'Hide diff' : 'Show diff'
          })
        ]
      }),
      open && loading ? jsx('span', { className: 'hc-meta', children: 'Loading diff…' }) : null,
      open && state.error ? jsx('span', { className: 'hc-error', children: state.error }) : null,
      ...(open && !loading && state.files
        ? state.files.map(f =>
            jsxs(
              'div',
              {
                children: [
                  jsxs('div', {
                    className: 'hc-file',
                    children: [
                      jsx('span', { className: 'hc-mono', style: { flex: 1 }, children: f.path }),
                      jsx('span', { className: 'hc-chip', children: DIFF_STATUS[f.status] || f.status })
                    ]
                  }),
                  f.diff && (f.status === 'modified' || f.status === 'added' || f.status === 'missing')
                    ? jsx(DiffBody, { text: f.diff })
                    : f.status === 'error' && f.diff
                      ? jsx('span', { className: 'hc-meta', children: f.diff })
                      : null,
                  f.truncated ? jsx('span', { className: 'hc-meta', children: 'Diff cut at 20,000 characters.' }) : null
                ]
              },
              f.path
            )
          )
        : [])
    ]
  })
}

function NoteEditor({ box, busy, onSave }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(box.note || '')
  useEffect(() => {
    setEditing(false)
  }, [box.id])
  useEffect(() => {
    if (!editing) setDraft(box.note || '')
  }, [box.note, editing])
  if (box.removed || box.kind === 'drift') {
    return box.note ? jsx('div', { className: 'hc-note-text', children: box.note }) : null
  }
  if (!editing) {
    return jsxs('div', {
      className: 'hc-row',
      children: [
        box.note ? jsx('div', { className: 'hc-note-text', style: { flex: 1 }, children: box.note }) : null,
        jsx('button', {
          type: 'button',
          className: 'hc-btn',
          disabled: busy,
          onClick: () => setEditing(true),
          children: box.note ? 'Edit note' : 'Add note for the agent'
        })
      ]
    })
  }
  const save = async text => {
    if (await onSave(text)) setEditing(false)
  }
  return jsxs('form', {
    onSubmit: e => {
      e.preventDefault()
      save(draft.trim())
    },
    children: [
      jsx('textarea', {
        className: 'hc-textarea',
        value: draft,
        maxLength: 500,
        autoFocus: true,
        placeholder: 'For example: use the brand colors here',
        'aria-label': 'Note for the agent',
        onChange: e => setDraft(e.target.value),
        onKeyDown: e => {
          if (e.key === 'Escape') setEditing(false)
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            save(draft.trim())
          }
        }
      }),
      jsxs('div', {
        className: 'hc-row',
        children: [
          jsx('button', { type: 'submit', className: 'hc-btn', disabled: busy || draft.trim() === (box.note || ''), children: 'Save note' }),
          box.note ? jsx('button', { type: 'button', className: 'hc-btn danger', disabled: busy, onClick: () => save(''), children: 'Clear' }) : null,
          jsx('button', { type: 'button', className: 'hc-btn', onClick: () => setEditing(false), children: 'Cancel' })
        ]
      })
    ]
  })
}

function Detail({
  canvasId,
  box,
  labels,
  renaming,
  busy,
  onRename,
  onCancelRename,
  onStartRename,
  onRemove,
  onRestore,
  onAdopt,
  onRevert,
  onUnlink,
  onNote
}) {
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
  if (box.owner) rows.push(['Built by', jsx('span', { children: `${ownerName(box.owner)} (${box.owner})` })])
  if (box.files && box.files.length) rows.push(['Files', jsx('span', { className: 'hc-mono', children: box.files.join('  ') })])
  if (box.check) rows.push(['Check', jsx('span', { className: 'hc-mono', children: box.check })])
  if (box.written && box.written.length) {
    rows.push(['Written', jsx('span', { className: 'hc-mono', children: box.written.slice(-6).join('  ') })])
  }
  if (box.blocked) rows.push(['Blocked writes', jsx('span', { children: String(box.blocked) })])
  if (box.after && box.after.length) {
    rows.push([
      'Waits for',
      jsx('div', {
        children: box.after.map(dep =>
          jsxs(
            'span',
            {
              className: 'hc-dep',
              children: [
                labels.get(dep) || dep,
                jsx('button', {
                  type: 'button',
                  disabled: busy,
                  title: `Let this box start without ${labels.get(dep) || dep}`,
                  'aria-label': `Remove the dependency on ${labels.get(dep) || dep}`,
                  onClick: () => onUnlink(dep),
                  children: '×'
                })
              ]
            },
            dep
          )
        )
      })
    ])
  }
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
          box.kind === 'drift' && !box.removed
            ? jsx('button', {
                type: 'button',
                className: 'hc-btn',
                disabled: busy,
                onClick: onAdopt,
                title: 'Keep this file as part of the plan',
                children: 'Add to plan'
              })
            : null,
          box.kind === 'drift' && !box.removed
            ? box.revert_asked
              ? jsx('span', { className: 'hc-chip', title: 'The agent puts the file back, then removes this box', children: 'undo asked' })
              : jsx('button', {
                  type: 'button',
                  className: 'hc-btn',
                  disabled: busy,
                  onClick: onRevert,
                  title: 'Ask the agent to put this file back as it was',
                  children: 'Revert'
                })
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
      jsx(NoteEditor, { box, busy, onSave: onNote }),
      ...rows.map(([k, v]) =>
        jsxs('div', { children: [jsx('div', { className: 'hc-k', children: k }), v] }, k)
      ),
      output
        ? jsxs('div', {
            children: [jsx('div', { className: 'hc-k', children: `Check output (exit ${box.check_exit})` }), output]
          })
        : null,
      jsx(Changes, { canvasId, box })
    ]
  })
}

/* ---------- export ---------- */

const MERMAID_CLASS = { todo: 'todo', doing: 'doing', done: 'done', failed: 'failed', removed: 'removed', drift: 'drift' }

function mermaidText(doc) {
  const ids = new Map(doc.boxes.map((b, i) => [b.id, `n${i}`]))
  const esc = text => String(text).replace(/"/g, '#quot;').replace(/[\r\n]+/g, ' ')
  const lines = ['flowchart TD', `  %% ${esc(doc.title)} (revision ${doc.revision})`]
  for (const b of doc.boxes) {
    const state = boxState(b)
    const text = b.kind === 'question' && b.answer ? `${b.label}: ${b.answer}` : b.label
    const shape = b.kind === 'question' ? [`{"`, `"}`] : [`["`, `"]`]
    lines.push(`  ${ids.get(b.id)}${shape[0]}${esc(text)}${shape[1]}:::${MERMAID_CLASS[state] || 'todo'}`)
  }
  for (const b of doc.boxes) {
    for (const a of b.after || []) if (ids.has(a)) lines.push(`  ${ids.get(a)} --> ${ids.get(b.id)}`)
  }
  lines.push(
    '  classDef todo fill:#f4f4f5,stroke:#71717a',
    '  classDef doing fill:#dbeafe,stroke:#2563eb',
    '  classDef done fill:#dcfce7,stroke:#16a34a',
    '  classDef failed fill:#fee2e2,stroke:#dc2626',
    '  classDef removed fill:#fafafa,stroke:#a1a1aa,stroke-dasharray:4 3,color:#a1a1aa',
    '  classDef drift fill:#fff,stroke:#71717a,stroke-dasharray:6 4'
  )
  return lines.join('\n') + '\n'
}

async function copyMermaid(doc) {
  const text = mermaidText(doc)
  try {
    await navigator.clipboard.writeText(text)
    host.notify && host.notify({ kind: 'success', message: 'Canvas copied as a Mermaid flowchart.' })
  } catch (err) {
    console.error('[hermes-canvas] clipboard write failed', err)
    host.notify && host.notify({ kind: 'error', message: 'Could not copy the Mermaid text to the clipboard.' })
  }
}

/* ---------- finale ---------- */

const FINALE_MS = 3600

function Finale() {
  const r = mulberry(7)
  const colors = ['var(--hc-done)', 'var(--hc-doing)', 'var(--hc-note)', 'var(--hc-accent)', 'var(--hc-fail)']
  const bits = Array.from({ length: 46 }, (_, i) => ({
    left: `${(r() * 100).toFixed(1)}%`,
    delay: `${(r() * 0.6).toFixed(2)}s`,
    dur: `${(1.6 + r() * 1.2).toFixed(2)}s`,
    rot: `${Math.round(r() * 720 - 360)}deg`,
    drift: `${Math.round(r() * 80 - 40)}px`,
    color: colors[i % colors.length]
  }))
  return jsxs('div', {
    className: 'hc-finale',
    'aria-live': 'polite',
    children: [
      ...bits.map((b, i) =>
        jsx(
          'i',
          {
            style: {
              left: b.left,
              background: b.color,
              animationDelay: b.delay,
              animationDuration: b.dur,
              '--hc-rot': b.rot,
              '--hc-drift': b.drift
            }
          },
          i
        )
      ),
      jsx('b', { children: 'Shipped' })
    ]
  })
}

/* ---------- history ---------- */

function HistoryBar({ list, n, latest, playing, onPick, onPlay, onClose }) {
  const item = list.find(h => h.revision === n)
  const when = item ? new Date(item.at * 1000).toLocaleTimeString() : ''
  return jsxs('div', {
    className: 'hc-history',
    children: [
      jsxs('div', {
        className: 'hc-row',
        children: [
          jsx('button', { type: 'button', className: 'hc-btn', onClick: onPlay, children: playing ? 'Pause' : 'Play' }),
          jsx('input', {
            type: 'range',
            className: 'hc-range',
            min: 1,
            max: latest,
            step: 1,
            value: n,
            'aria-label': 'Revision',
            onChange: e => onPick(Number(e.target.value))
          }),
          jsx('span', { className: 'hc-meta', children: `rev ${n}/${latest}` }),
          jsx('button', { type: 'button', className: 'hc-btn', onClick: onClose, children: 'Live' })
        ]
      }),
      item
        ? jsx('span', {
            className: 'hc-meta',
            children: `${item.author === 'user' ? 'You' : item.author === 'agent' ? 'Agent' : 'Hooks'} · ${when} · ${item.reason}`
          })
        : null
    ]
  })
}

/* ---------- adding a box ---------- */

function slugId(label, boxes) {
  const base =
    label
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40) || 'box'
  const taken = new Set(boxes.map(b => b.id))
  let id = base
  for (let n = 2; taken.has(id); n++) id = `${base}-${n}`
  return id
}

function AddBox({ after, busy, onAdd, onCancel }) {
  const [label, setLabel] = useState('')
  return jsxs('form', {
    className: 'hc-detail',
    onSubmit: e => {
      e.preventDefault()
      if (label.trim()) onAdd(label.trim())
    },
    children: [
      jsx('div', { className: 'hc-k', children: after ? `New box after "${after.label}"` : 'New box' }),
      jsxs('div', {
        className: 'hc-row',
        children: [
          jsx('input', {
            className: 'hc-input',
            value: label,
            maxLength: 200,
            autoFocus: true,
            placeholder: 'What should be built?',
            'aria-label': 'New box name',
            onChange: e => setLabel(e.target.value),
            onKeyDown: e => {
              if (e.key === 'Escape') onCancel()
            }
          }),
          jsx('button', { type: 'submit', className: 'hc-btn', disabled: busy || !label.trim(), children: 'Add' }),
          jsx('button', { type: 'button', className: 'hc-btn', onClick: onCancel, children: 'Cancel' })
        ]
      }),
      jsx('span', { className: 'hc-meta', children: 'The agent picks its files and check, then builds it.' })
    ]
  })
}

/* ---------- the pane ---------- */

const FOLLOW_KEY = 'hermes-canvas.follow'

function readFollow() {
  try {
    return window.localStorage.getItem(FOLLOW_KEY) !== 'off'
  } catch (err) {
    console.warn('[hermes-canvas] could not read the Follow setting', err)
    return true
  }
}

function saveFollow(on) {
  try {
    window.localStorage.setItem(FOLLOW_KEY, on ? 'on' : 'off')
  } catch (err) {
    console.warn('[hermes-canvas] could not save the Follow setting', err)
  }
}

// What changed between two revisions of the same canvas: boxes with a new status or new writes,
// and the boxes that just became done.
function diffBoxes(prev, boxes) {
  const changed = []
  const done = []
  for (const b of boxes) {
    const old = prev.get(b.id)
    if (!old) continue
    if (old.status !== b.status || old.writes !== b.writes || old.removed !== b.removed) changed.push(b)
    if (old.status !== 'done' && b.status === 'done' && !b.removed) done.push(b)
  }
  return { changed, done }
}

function useSessionId() {
  const stored = useValue(host.state.focusedStoredSessionId)
  const runtime = useValue(host.state.focusedSessionId)
  return { primary: stored || runtime || null, ids: [stored, runtime].filter(Boolean) }
}

function CanvasPane() {
  const session = useSessionId()
  const visible = useValue(host.paneVisibility(PANE_ID))
  // Subscribed so that the pane redraws when the agent's turn starts or ends (the finish banner).
  useValue(host.state.busyBySession)
  useValue(host.state.busy)
  const mainBusy = sessionBusy(session.ids)
  // The revision at which the agent's current turn started: a finish hold from an earlier turn
  // does not hold this one.
  const turnRef = useRef({ busy: false, doc: null, since: null })
  const [doc, setDoc] = useState(null)
  const [pending, setPending] = useState(0)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [live, setLive] = useState(false)
  const [selected, setSelected] = useState(null)
  const [renaming, setRenaming] = useState(false)
  const [adding, setAdding] = useState(false)
  const [hist, setHist] = useState(null) // { list, n, doc, playing } while viewing past revisions
  const [finale, setFinale] = useState(false)
  const [busy, setBusy] = useState(false)
  const [showEdits, setShowEdits] = useState(false)
  // The drag lives in a ref: pointer events can arrive before React re-renders, so handlers must
  // not read it from a render's closure. The state copy only redraws the dragged box.
  const dragRef = useRef(null) // { id, dx, dy, startX, startY, moved }
  // Link mode: drag from a box to the box that must wait for it.
  const [linking, setLinking] = useState(false)
  const linkRef = useRef(null) // { from, x, y, over }
  const [link, setLinkState] = useState(null)
  const setLink = l => {
    linkRef.current = l
    setLinkState(l)
  }
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
    setAdding(false)
    setHist(null)
    setFinale(false)
    setBorn({})
    setSaid({})
    setShowEdits(false)
    firstRef.current = true
    revSeq.current++
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

  const shown = (hist && hist.doc) || doc
  const past = Boolean(hist)
  const boxes = shown ? shown.boxes : []
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
  // Motion: a box that changes gets a pop, a box that gets done sends a comet to the boxes after
  // it, and the camera (with Follow on) moves to the box that changed, then back to the whole plan.
  const [follow, setFollow] = useState(readFollow)
  const [hover, setHover] = useState(false)
  const holdRef = useRef(null)
  // A pointer that rests on the board (after a click, say) must not stop the camera for good.
  const holdCamera = () => {
    setHover(true)
    clearTimeout(holdRef.current)
    holdRef.current = setTimeout(() => {
      if (!dragRef.current && !linkRef.current) setHover(false)
    }, HOLD_MS)
  }
  useEffect(() => () => clearTimeout(holdRef.current), [])
  const [focus, setFocus] = useState(null) // box id
  const [comets, setComets] = useState([]) // { key, d }
  const [pops, setPops] = useState({}) // box id -> stamp
  const [born, setBorn] = useState({}) // box id -> [draw-on delay (s), stamp], while it draws itself
  const [said, setSaid] = useState({}) // actor -> revision of the activity it shows in a bubble
  const seenRef = useRef(null) // { id, boxes: Map, acts: Map }
  const firstRef = useRef(true) // the first canvas after a chat switch draws itself
  const bornSeq = useRef(0)
  const timersRef = useRef(new Set())
  const later = (fn, ms) => {
    const t = setTimeout(() => {
      timersRef.current.delete(t)
      fn()
    }, ms)
    timersRef.current.add(t)
  }
  useEffect(() => () => timersRef.current.forEach(clearTimeout), [])
  // New boxes draw themselves, one after another in plan order.
  const drawOn = list => {
    if (!list.length) return
    const stamp = ++bornSeq.current
    setBorn(cur => ({ ...cur, ...Object.fromEntries(list.map((b, i) => [b.id, [Math.min(i * 0.12, 1.2), stamp]])) }))
    later(() => setBorn(cur => Object.fromEntries(Object.entries(cur).filter(([, v]) => v[1] !== stamp))), 2800)
  }
  // Entering or leaving the replay jumps between revisions: that jump is not a change to animate.
  useLayoutEffect(() => {
    seenRef.current = null
  }, [past])
  // A layout effect: a new box gets its draw-on class before the first paint shows it drawn.
  useLayoutEffect(() => {
    if (!shown) return
    const seen = seenRef.current
    const now = new Map(shown.boxes.map(b => [b.id, { status: b.status, writes: b.writes, removed: b.removed }]))
    const acts = new Map(agentsOf(shown).map(a => [a.actor, a.entry.revision]))
    seenRef.current = { id: shown.id, boxes: now, acts }
    const first = firstRef.current && !past
    if (!seen || seen.id !== shown.id) {
      firstRef.current = false
      if (first) drawOn(shown.boxes)
      return
    }
    drawOn(shown.boxes.filter(b => !seen.boxes.has(b.id)))
    // A bubble says what an agent just did: one per new activity.
    const spoke = [...acts].filter(([actor, rev]) => seen.acts.get(actor) !== rev)
    if (spoke.length) {
      setSaid(cur => ({ ...cur, ...Object.fromEntries(spoke) }))
      spoke.forEach(([actor, rev]) =>
        later(() => setSaid(cur => (cur[actor] === rev ? Object.fromEntries(Object.entries(cur).filter(([k]) => k !== actor)) : cur)), BUBBLE_MS)
      )
    }
    const { changed, done } = diffBoxes(seen.boxes, shown.boxes)
    if (!changed.length) return
    const stamp = Date.now()
    setPops(cur => {
      const next = { ...cur }
      changed.forEach(b => (next[b.id] = stamp))
      return next
    })
    later(() => setPops(cur => Object.fromEntries(Object.entries(cur).filter(([, v]) => v !== stamp))), 600)
    const fresh = []
    for (const src of done) {
      for (const dst of shown.boxes) {
        if (dst.removed || !(dst.after || []).includes(src.id)) continue
        const a = placed.get(src.id)
        const b = placed.get(dst.id)
        if (a && b) fresh.push({ key: `${src.id}>${dst.id}@${stamp}`, d: edgeGeometry({ ...a, id: src.id }, { ...b, id: dst.id }).d })
      }
    }
    if (fresh.length) {
      setComets(cur => [...cur, ...fresh])
      const keys = new Set(fresh.map(c => c.key))
      later(() => setComets(cur => cur.filter(c => !keys.has(c.key))), COMET_MS + 150)
    }
    const target = [...changed].reverse().find(b => b.kind === 'component' || b.kind === 'drift')
    if (target && !hover) {
      setFocus(target.id)
      later(() => setFocus(cur => (cur === target.id ? null : cur)), FOCUS_MS)
    }
    // Finale: the live plan just became all done.
    if (!past && statusLine(shown).tone === 'done' && done.length) {
      setFinale(true)
      later(() => setFinale(false), FINALE_MS)
    }
  }, [shown])

  const edits = useEdits(doc ? doc.id : '', `${doc ? doc.revision : 0}|${pending}`, 8)

  // History: scrub or play the saved revisions. The live canvas keeps updating underneath.
  // Only the newest request may change the replay: a slow older response must not land last.
  const revSeq = useRef(0)
  const showRevision = useCallback(async n => {
    const current = docRef.current
    if (!current) return
    const seq = ++revSeq.current
    try {
      const res = await rest(`/revision?id=${encodeURIComponent(current.id)}&n=${n}`)
      if (seq !== revSeq.current || !docRef.current || docRef.current.id !== current.id) return
      setHist(h => (h ? { ...h, n, doc: res.canvas } : h))
    } catch (err) {
      if (seq !== revSeq.current) return
      setError(errorText(err))
      setHist(h => h && { ...h, playing: false })
    }
  }, [])
  const openHistory = async () => {
    const current = docRef.current
    if (!current) return
    try {
      const res = await rest(`/history?id=${encodeURIComponent(current.id)}`)
      setSelected(null)
      setAdding(false)
      setRenaming(false)
      setHist({ list: res.revisions || [], n: current.revision, doc: current, playing: false })
    } catch (err) {
      setError(errorText(err))
    }
  }
  useEffect(() => {
    if (!hist || !hist.playing) return
    const latest = Math.max(docRef.current ? docRef.current.revision : 1, hist.list.length ? hist.list[hist.list.length - 1].revision : 1)
    if (hist.n >= latest) {
      setHist(h => h && { ...h, playing: false })
      return
    }
    const t = setTimeout(() => showRevision(hist.n + 1), 750)
    return () => clearTimeout(t)
  }, [hist, showRevision])
  const target = (follow && !hover && focus && focusView(view, placed.get(focus))) || view
  const cam = useCamera(target)

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

  // The box under a board point that can wait for others (link mode). The topmost box wins.
  const boxAt = p => {
    for (const box of [...shown.boxes].reverse()) {
      if (box.kind !== 'component' || box.removed) continue
      const r = posOf(box.id)
      if (r && p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h) return box
    }
    return null
  }

  const onBoxPointerDown = (e, box) => {
    if (e.button !== 0 || past) return
    e.stopPropagation()
    const p = toBoard(e)
    if (linking) {
      if (busy || box.removed || (box.kind !== 'component' && box.kind !== 'question')) return
      setLink({ from: box.id, x: p.x, y: p.y, over: null })
      if (svgRef.current) svgRef.current.setPointerCapture(e.pointerId)
      return
    }
    // Select on release, not here: the detail strip resizes the board, which must not happen
    // under the pointer while a drag is in progress.
    setDrag({ id: box.id, dx: 0, dy: 0, startX: p.x, startY: p.y, moved: false })
    if (svgRef.current) svgRef.current.setPointerCapture(e.pointerId)
  }

  const onPointerMove = e => {
    const l = linkRef.current
    if (l) {
      const p = toBoard(e)
      const over = boxAt(p)
      setLink({ ...l, x: p.x, y: p.y, over: over && over.id !== l.from ? over.id : null })
      return
    }
    const d = dragRef.current
    if (!d) return
    const p = toBoard(e)
    const dx = p.x - d.startX
    const dy = p.y - d.startY
    setDrag({ ...d, dx, dy, moved: d.moved || Math.hypot(dx, dy) > 4 })
  }

  const onPointerUp = async () => {
    const l = linkRef.current
    if (l) {
      setLink(null)
      // The arrow points from the box that comes first to the box that waits for it.
      if (l.over && (await send([{ op: 'link', id: l.over, after: l.from }]))) setLinking(false)
      return
    }
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
  const adopt = () => selBox && send([{ op: 'adopt', id: selBox.id }])
  const revert = () => selBox && send([{ op: 'revert', id: selBox.id }])
  const unlink = dep => selBox && send([{ op: 'unlink', id: selBox.id, after: dep }])
  const note = text => (selBox ? send([{ op: 'note', id: selBox.id, text }]) : Promise.resolve(false))
  const addAfter = selBox && selBox.kind === 'component' && !selBox.removed ? selBox : null
  const addBox = async label => {
    const id = slugId(label, doc.boxes)
    const box = { id, label, after: addAfter ? [addAfter.id] : [] }
    if (await send([{ op: 'add', box }])) {
      setAdding(false)
      setSelected(id)
    }
  }
  const answer = async (box, option) => {
    if (past || box.answer === option) return
    if (await send([{ op: 'answer', id: box.id, answer: option }])) continueTurn(session.ids, box, option)
  }

  const onKeyDown = e => {
    if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return
    if (linking && e.key === 'Escape') {
      setLink(null)
      setLinking(false)
      return
    }
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

  const progress = progressOf(shown)
  const latestRev = hist && hist.list.length ? Math.max(doc.revision, hist.list[hist.list.length - 1].revision) : doc.revision
  const edges = []
  for (const box of shown.boxes) {
    for (const dep of box.after || []) {
      const a = posOf(dep)
      const b = posOf(box.id)
      const src = shown.boxes.find(x => x.id === dep)
      if (a && b && src) {
        const drawn = born[box.id] !== undefined || born[dep] !== undefined
        const delay = Math.max(born[box.id] ? born[box.id][0] : 0, born[dep] ? born[dep][0] : 0) + 0.35
        edges.push(
          jsx(
            Edge,
            { from: { ...a, id: dep }, to: { ...b, id: box.id }, ok: src.status === 'done', born: drawn ? delay : undefined },
            `${dep}>${box.id}`
          )
        )
      }
    }
  }

  const agents = agentsOf(shown)
  const slots = new Map()
  const markers = agents.map(agent => {
    const pos = posOf(agent.box)
    if (!pos) return null
    const slot = slots.get(agent.box) || 0
    slots.set(agent.box, slot + 1)
    // In the replay, each step shows what happened in that revision.
    const fresh = past ? agent.entry.revision === shown.revision : said[agent.actor] === agent.entry.revision
    return jsx(AgentMarker, { agent, pos, slot, bubble: fresh && !agent.idle, ripple: fresh }, agent.actor)
  })
  const turn = turnRef.current
  if (!mainBusy) turnRef.current = { busy: false, doc: null, since: null }
  else if (doc && (!turn.busy || turn.doc !== doc.id)) turnRef.current = { busy: true, doc: doc.id, since: doc.revision }
  const gate = gateOf(shown, past ? -1 : turnRef.current.since)

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
          jsx('span', { className: 'hc-meta', children: `${progress.done}/${progress.total} done · rev ${shown.revision}` }),
          jsx('button', {
            type: 'button',
            className: `hc-btn${past ? ' on hc-follow' : ''}`,
            title: 'Replay how the plan changed',
            onClick: () => (past ? setHist(null) : openHistory()),
            children: 'History'
          }),
          jsx('button', {
            type: 'button',
            className: 'hc-btn',
            title: 'Copy the canvas as a Mermaid flowchart',
            onClick: () => copyMermaid(shown),
            children: 'Mermaid'
          }),
          jsx('button', {
            type: 'button',
            className: `hc-btn hc-follow${follow ? ' on' : ''}`,
            'aria-pressed': follow,
            title: 'Move the view to the box that changes',
            onClick: () => {
              saveFollow(!follow)
              setFollow(!follow)
              setFocus(null)
            },
            children: 'Follow'
          }),
          jsx('button', {
            type: 'button',
            className: 'hc-btn',
            disabled: busy || past,
            title: 'Add a box to the plan',
            onClick: () => {
              setAdding(true)
              setRenaming(false)
            },
            children: '+ Box'
          }),
          jsx('button', {
            type: 'button',
            className: `hc-btn hc-follow${linking ? ' on' : ''}`,
            'aria-pressed': linking,
            disabled: busy || past,
            title: 'Draw an arrow from a box to a box that must wait for it',
            onClick: () => {
              setLink(null)
              setLinking(!linking)
            },
            children: '→ Link'
          })
        ]
      }),
      jsxs('div', {
        className: `hc-board${linking ? ' hc-linking' : ''}`,
        ref: boardRef,
        // The camera holds still under a moving pointer, so a box never moves away from a click.
        onPointerEnter: () => {
          holdCamera()
          setFocus(null)
        },
        onPointerMove: holdCamera,
        onPointerLeave: () => {
          clearTimeout(holdRef.current)
          if (!dragRef.current) setHover(false)
        },
        onPointerUp: holdCamera,
        children: [
          jsxs('svg', {
            ref: svgRef,
            viewBox: `${f1(cam.x)} ${f1(cam.y)} ${f1(cam.w)} ${f1(cam.h)}`,
            preserveAspectRatio: 'xMidYMid meet',
            role: 'img',
            'aria-label': `Canvas: ${doc.title}`,
            onPointerMove,
            onPointerUp,
            onPointerCancel: () => {
              setDrag(null)
              setLink(null)
            },
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
              jsx('g', { children: comets.map(c => jsx(Comet, { d: c.d }, c.key)) }),
              jsx('g', {
                children: shown.boxes.map(box => {
                  const pos = posOf(box.id)
                  if (!pos) return null
                  return jsx(
                    BoxEl,
                    {
                      box,
                      pos,
                      selected: box.id === selected,
                      popped: pops[box.id] || 0,
                      born: born[box.id] ? born[box.id][0] : undefined,
                      onPointerDown: onBoxPointerDown,
                      onDoubleClick: b => {
                        if (past) return
                        setSelected(b.id)
                        if (b.kind !== 'drift') setRenaming(true)
                      },
                      onAnswer: answer
                    },
                    box.id
                  )
                })
              }),
              jsx('g', { children: markers }),
              link ? jsx(LinkDraft, { from: posOf(link.from), to: link.over ? posOf(link.over) : null, x: link.x, y: link.y }) : null
            ]
          }),
          past ? jsx('span', { className: 'hc-past', children: `Replay · revision ${shown.revision}` }) : null,
          gate
            ? jsx('div', {
                className: 'hc-gate',
                role: 'status',
                style: { top: past ? 34 : 8 },
                title: gate.held.map(b => b.label).join(', '),
                children: `Finish held: ${actorName(gate.entry.actor)} waits for ${gate.held.map(b => b.label).join(', ')} to pass`
              })
            : null,
          finale && !past ? jsx(Finale, {}) : null
        ]
      }),
      hist
        ? jsx(HistoryBar, {
            list: hist.list,
            n: hist.n,
            latest: latestRev,
            playing: hist.playing,
            onPick: n => {
              setHist(h => h && { ...h, playing: false })
              showRevision(n)
            },
            onPlay: () => {
              if (hist.playing) return setHist(h => h && { ...h, playing: false })
              // Play from the start when the slider is at the end.
              const restart = hist.n >= latestRev
              setHist(h => h && { ...h, n: restart ? 1 : h.n, playing: true })
              if (restart) showRevision(1)
            },
            onClose: () => setHist(null)
          })
        : adding
        ? jsx(AddBox, { after: addAfter, busy, onAdd: addBox, onCancel: () => setAdding(false) })
        : selBox
        ? jsx(Detail, {
            canvasId: doc.id,
            box: selBox,
            renaming,
            busy,
            onRename: rename,
            onCancelRename: () => setRenaming(false),
            onStartRename: () => setRenaming(true),
            onRemove: remove,
            onRestore: restore,
            onAdopt: adopt,
            onRevert: revert,
            onUnlink: unlink,
            labels: new Map(doc.boxes.map(b => [b.id, b.label])),
            onNote: note
          })
        : null,
      showEdits && edits.length
        ? jsxs('div', {
            className: 'hc-edits-panel',
            children: [jsx('div', { className: 'hc-k', children: 'Your edits · hover to see what the agent reads' }), jsx(EditList, { edits })]
          })
        : null,
      jsxs('div', {
        className: 'hc-foot',
        children: [
          jsx('span', {
            className: 'hc-src',
            children: linking
              ? 'Drag from a box to a box that must wait for it. Esc to stop.'
              : 'Box status comes from hooks, not from the model.'
          }),
          edits.length
            ? jsx('button', {
                type: 'button',
                className: 'hc-textbtn',
                'aria-expanded': showEdits,
                title: 'Show your edits and whether the agent got them',
                onClick: () => setShowEdits(!showEdits),
                children: pending ? `${pending} edit${pending === 1 ? '' : 's'} waiting for the agent` : 'Your edits'
              })
            : null,
          error ? jsx('span', { className: 'hc-error', children: error }) : null
        ]
      })
    ]
  })
}

/* ---------- the user's edits, and whether the agent got them ---------- */

function firstSentence(text) {
  const t = String(text || '')
  const i = t.indexOf('. ')
  return i > 0 ? t.slice(0, i + 1) : t
}

// The latest edits of a canvas. It loads again when `key` changes (a new revision or pending
// count), and on the poll tick while an edit still waits for the agent: delivery saves no revision.
function useEdits(canvasId, key, limit) {
  const [edits, setEdits] = useState([])
  const seq = useRef(0)
  const load = useCallback(async () => {
    if (!canvasId) return
    const n = ++seq.current
    try {
      const res = await rest(`/edits?id=${encodeURIComponent(canvasId)}&limit=${limit}`)
      if (n === seq.current) setEdits(res.edits || [])
    } catch (err) {
      console.warn('[hermes-canvas] could not load the edits', err)
    }
  }, [canvasId, limit])
  useEffect(() => {
    setEdits([])
  }, [canvasId])
  useEffect(() => {
    load()
  }, [load, key])
  const waiting = edits.some(e => !e.delivered_at)
  useCanvasEvents(event => {
    if (!event && waiting) load()
  })
  return edits
}

function EditList({ edits }) {
  if (!edits.length) return null
  return jsx('ul', {
    className: 'hc-edits',
    children: edits.map(e =>
      jsxs(
        'li',
        {
          className: e.delivered_at ? 'got' : 'wait',
          title: `The agent reads: ${e.text}`,
          children: [
            jsx('span', { className: 'hc-edit-mark', 'aria-hidden': true, children: e.delivered_at ? '✓' : '…' }),
            jsx('span', { className: 'hc-edit-text', children: firstSentence(e.text) }),
            jsx('span', { className: 'hc-edit-state', children: e.delivered_at ? 'delivered' : 'waiting' })
          ]
        },
        e.id
      )
    )
  })
}

/* ---------- the chat card ---------- */

function CanvasCard({ attrs, sessionId }) {
  const id = attrs && typeof attrs.id === 'string' ? attrs.id : ''
  const valid = CANVAS_ID_RE.test(id)
  const [doc, setDoc] = useState(null)
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const edits = useEdits(valid ? id : '', doc ? doc.revision : 0, 3)
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
  const answer = async (box, option) => {
    if (sending || box.answer === option) return
    setSending(true)
    try {
      const res = await rest('/edit', { method: 'POST', body: { canvas_id: id, ops: [{ op: 'answer', id: box.id, answer: option }] } })
      setDoc(res.canvas || null)
      setError('')
      continueTurn(sessionId ? [sessionId] : [], box, option)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setSending(false)
    }
  }
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
  const open = doc.boxes.filter(b => b.kind === 'question' && !b.removed && !b.answer && b.options.length)
  const status = statusLine(doc)
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
      jsx('span', { className: `hc-status ${status.tone}`, children: status.text }),
      jsx('div', {
        className: 'hc-chips',
        children: parts.map(b => {
          const state = boxState(b)
          return jsx('span', { className: `hc-chip ${state}`, title: STATUS_TEXT[state] || state, children: b.label }, b.id)
        })
      }),
      ...open.map(q =>
        jsxs(
          'div',
          {
            className: 'hc-ask',
            children: [
              jsx('span', { className: 'hc-ask-q', children: q.label }),
              ...q.options.map(opt =>
                jsx('button', { type: 'button', className: 'hc-btn', disabled: sending, onClick: () => answer(q, opt), children: opt }, opt)
              )
            ]
          },
          q.id
        )
      ),
      edits.length ? jsx('div', { className: 'hc-k', children: 'Your edits' }) : null,
      jsx(EditList, { edits }),
      error ? jsx('span', { className: 'hc-error', children: error }) : null,
      jsxs('div', {
        className: 'hc-row',
        children: [
          typeof host.revealPane === 'function'
            ? jsx('button', { type: 'button', className: 'hc-btn', onClick: revealPane, children: 'Open canvas' })
            : null,
          jsx('span', { className: 'hc-meta', children: 'Steer the plan on the canvas: edits reach the agent at once.' })
        ]
      })
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
