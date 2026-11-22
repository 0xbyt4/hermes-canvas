// Geometry for the canvas board, and the camera that looks at it.
//
// Everything here is pure: give it boxes and a viewport and it returns positions and a view, so
// the pane only renders, and a test can pin the behaviour that matters. The one rule the whole
// file exists to protect: content growth never moves the camera the user set. A plan that gains
// boxes must stay at the scale you were reading it at.

export const BOX = { w: 184, h: 78 }
export const GAP = { x: 76, y: 46 }
export const MARGIN = 60
// Rendered under a box for the "who is working here" line, so bounds include it.
export const SUBLINE = 22
// Drift boxes are writes outside the plan. They wrap into a grid: one endless row widened the
// board with every unplanned write, and the fit-all view then shrank the whole canvas each time.
export const DRIFT = { perRow: 4, gapX: 24, gapY: 20 }
// Zoom limits as screen pixels per board unit.
export const ZOOM = { min: 0.2, max: 3 }

// Text metrics. Labels and answers are SVG text, which cannot be measured before it is laid out, so
// a wrap width is computed from the box width and a conservative glyph advance (advance is a
// fraction of the font size). Wrapping by character count alone is what pushed a question's title
// out of its own box: 40 characters at font-size 17 need ~350 units inside a 320-wide box, and a
// centred label then spilled over both edges.
export const TEXT = {
  label: 17,
  opt: 14,
  advance: 0.55,
  labelInset: 26,
  optInset: 18,
  optLine: 13.5,
  optRowMin: 30,
  optGap: 6,
}
export const QUESTION_W = 320

/** How many characters of `px` type fit across `width`, less `inset` of padding. */
export const charsFor = (width, px, inset) =>
  Math.max(8, Math.floor((width - inset) / (px * TEXT.advance)))

/** The question's own sentence, wrapped to its box. */
export function questionLabel(box, w = QUESTION_W) {
  return wrapText(box.label, charsFor(w, TEXT.label, TEXT.labelInset), 3)
}

/** Each answer, wrapped to the row it sits in. */
export function questionOptions(box, w = QUESTION_W) {
  return (box.options || []).map(opt => wrapText(opt, charsFor(w, TEXT.opt, TEXT.optInset), 2))
}

/** One row per answer, taller when that answer wraps. */
export function questionRows(box, w = QUESTION_W) {
  return questionOptions(box, w).map(lines => Math.max(TEXT.optRowMin, 6 + lines.length * TEXT.optLine))
}

/** Where the first answer row starts, below the wrapped question. */
export function questionTop(labelLines) {
  return 26 + (labelLines.length - 1) * TEXT.label * 1.15 + 20
}

/**
 * The question still worth looking at after one is answered, in plan order.
 * Answering must not leave the camera on the box just answered — that re-zooms a question the user
 * has finished with. Returns null when nothing is open, which is the cue to fit the whole plan.
 */
export function nextQuestion(boxes, answeredId) {
  return (
    (boxes || []).find(
      b => b.kind === 'question' && !b.removed && !b.answer && b.id !== answeredId
    ) || null
  )
}

export function wrapText(text, max, lines = 2) {
  const out = []
  let cur = ''
  for (const raw of String(text || '').split(/\s+/)) {
    let w = raw
    if (!w) continue
    // A single token longer than the line — a file path, a URL, a long flag — is hard-broken.
    // Leaving it whole made it overflow the box no matter what width the wrapper was given.
    while (w.length > max) {
      if (cur) {
        out.push(cur)
        cur = ''
      }
      out.push(w.slice(0, max))
      w = w.slice(max)
    }
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

/* ---------- box sizes ---------- */

export function boxSize(box) {
  if (box.kind === 'question') {
    // A question carries a full sentence plus up to four answers, so it is much wider and taller
    // than a component box. The height comes from the same wrapping the renderer uses, so a
    // wrapped answer can neither clip nor overlap the row below it.
    const w = QUESTION_W
    const label = questionLabel(box, w)
    const rows = questionRows(box, w)
    const h = questionTop(label) + rows.reduce((sum, r) => sum + r + TEXT.optGap, 0) + 6
    return { w, h }
  }
  if (box.kind === 'drift') return { w: 200, h: 64 }
  return { ...BOX }
}

/* ---------- layout ---------- */

// Every box gets an automatic slot by dependency depth: in columns (left to right) on a wide pane,
// in rows (top to bottom) on a tall one, so boxes stay readable. A box sits one step before the
// first box that waits for it, so a box with no dependencies is drawn next to its user, not far
// to the left. A box the user moved keeps its own x/y, but still holds its slot, so moving one box
// never shifts the others.
export function layout(boxes, vertical = false) {
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

  const auto = new Map()
  const drift = []
  // Slots follow each box's own size. A uniform pitch only works while every box is BOX; a
  // question box is 320 wide and up to ~180 tall, so a fixed pitch overlapped its neighbours.
  const levelSize = new Map()
  for (const box of boxes) {
    if (box.kind === 'drift') continue
    const level = levels.get(box.id)
    const s = boxSize(box)
    const m = levelSize.get(level) || { w: 0, h: 0 }
    levelSize.set(level, { w: Math.max(m.w, s.w), h: Math.max(m.h, s.h) })
  }
  const levelStart = axis => {
    const out = new Map()
    let at = 0
    for (const level of [...levelSize.keys()].sort((a, b) => a - b)) {
      out.set(level, at)
      const size = levelSize.get(level)
      at += axis === 'x' ? size.w + GAP.x + 40 : size.h + GAP.y + 30
    }
    return out
  }
  const levelX = levelStart('x')
  const levelY = levelStart('y')
  const cursor = new Map() // level -> next slot offset along the stacking axis
  for (const box of boxes) {
    if (box.kind === 'drift') {
      drift.push(box)
      continue
    }
    const level = levels.get(box.id)
    const s = boxSize(box)
    const at = cursor.get(level) || 0
    if (vertical) {
      auto.set(box.id, { x: at, y: levelY.get(level), ...s })
      cursor.set(level, at + s.w + 44)
    } else {
      auto.set(box.id, { x: levelX.get(level), y: at, ...s })
      cursor.set(level, at + s.h + GAP.y + 30)
    }
  }

  // Drift wraps: a fixed number per row, so unplanned writes cannot widen the board.
  let maxY = 0
  auto.forEach(p => {
    maxY = Math.max(maxY, p.y + p.h)
  })
  const driftW = boxSize({ kind: 'drift' }).w
  const driftH = boxSize({ kind: 'drift' }).h
  const top = maxY ? maxY + GAP.y + 30 : 0
  drift.forEach((box, i) => {
    const col = i % DRIFT.perRow
    const row = Math.floor(i / DRIFT.perRow)
    auto.set(box.id, {
      x: col * (driftW + DRIFT.gapX),
      y: top + row * (driftH + DRIFT.gapY),
      ...boxSize(box),
    })
  })

  const placed = new Map()
  for (const box of boxes) {
    const p = auto.get(box.id)
    if (!p) continue
    const pinned = typeof box.x === 'number' && typeof box.y === 'number'
    placed.set(box.id, pinned ? { ...p, x: box.x, y: box.y } : p)
  }
  return placed
}

export function bounds(placed) {
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  placed.forEach(p => {
    x0 = Math.min(x0, p.x)
    y0 = Math.min(y0, p.y)
    x1 = Math.max(x1, p.x + p.w)
    y1 = Math.max(y1, p.y + p.h + SUBLINE)
  })
  if (!isFinite(x0)) return { x: 0, y: 0, w: 600, h: 400 }
  return { x: x0 - MARGIN, y: y0 - MARGIN, w: x1 - x0 + MARGIN * 2, h: y1 - y0 + MARGIN * 2 }
}

/* ---------- camera ---------- */

// Screen pixels per board unit, which is what the zoom readout shows.
export function scaleOf(view, viewport) {
  return viewport.w / Math.max(1, view.w)
}

// The whole plan, in a window of the viewport's aspect. This is the only view content may pick.
export function fitView(content, viewport) {
  const ar = Math.max(0.2, viewport.w / Math.max(1, viewport.h))
  let w = content.w
  let h = content.h
  if (w / h > ar) h = w / ar
  else w = h * ar
  return { x: content.x + content.w / 2 - w / 2, y: content.y + content.h / 2 - h / 2, w, h }
}

// Zoom by `factor` while the board point under the pointer stays under the pointer.
export function zoomAt(view, factor, px, py, viewport) {
  const w0 = Math.max(1, view.w)
  const minW = viewport.w / ZOOM.max
  const maxW = viewport.w / ZOOM.min
  const w = Math.min(maxW, Math.max(minW, w0 / factor))
  const k = w / w0
  return { x: px - (px - view.x) * k, y: py - (py - view.y) * k, w, h: view.h * k }
}

// Drag the board by a pointer delta in screen pixels. The view keeps its size: panning never zooms.
export function panByPixels(view, dx, dy, viewport) {
  const scale = scaleOf(view, viewport)
  return { ...view, x: view.x - dx / scale, y: view.y - dy / scale }
}

// Keep the view on the plan: slide it back inside the content, and centre it when the view is
// bigger than the content. Size is never touched, so a clamp cannot zoom.
export function clampView(view, content) {
  const x0 = content.x - MARGIN
  const y0 = content.y - MARGIN
  const x1 = content.x + content.w + MARGIN
  const y1 = content.y + content.h + MARGIN
  const x = view.w >= x1 - x0 ? (x0 + x1) / 2 - view.w / 2 : Math.min(Math.max(view.x, x0), x1 - view.w)
  const y = view.h >= y1 - y0 ? (y0 + y1) / 2 - view.h / 2 : Math.min(Math.max(view.y, y0), y1 - view.h)
  return { ...view, x, y }
}

export function visible(view, box, pad = 24) {
  if (!box) return true
  return (
    box.x + box.w >= view.x + pad &&
    box.x <= view.x + view.w - pad &&
    box.y + box.h >= view.y + pad &&
    box.y <= view.y + view.h - pad
  )
}

// Slide the view the least it can to show a box, at the scale it already has.
export function revealBox(view, box, content) {
  if (visible(view, box)) return view
  let x = view.x
  let y = view.y
  if (box.x < view.x) x = box.x - MARGIN
  else if (box.x + box.w > view.x + view.w) x = box.x + box.w - view.w + MARGIN
  if (box.y < view.y) y = box.y - MARGIN
  else if (box.y + box.h > view.y + view.h) y = box.y + box.h - view.h + MARGIN
  return clampView({ ...view, x, y }, content)
}

export const REFIT_ON = ['open', 'fit', 'question']

// The camera policy, in one place. `reason` is why the view is being reconsidered:
//   'open'     a different canvas was opened  -> fit the plan
//   'fit'      the user pressed Fit           -> fit the plan
//   'question' a question box arrived         -> reveal it, at the scale already in use
//   'content'  boxes appeared, moved or went  -> do nothing at all
// That last line is the point: content never changes the zoom you are reading at.
export function nextView(prev, content, reason, viewport, opts = {}) {
  const fit = clampView(fitView(content, viewport), content)
  if (!prev) return fit
  if (reason === 'open' || reason === 'fit') return fit
  if (reason === 'question' && opts.box) return revealBox(prev, opts.box, content)
  return prev
}
