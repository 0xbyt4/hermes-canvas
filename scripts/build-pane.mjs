#!/usr/bin/env node
// Builds the pane's inline copy of the camera math.
//
// A *runtime* desktop plugin is evaluated from a Blob with a strict import allowlist
// (@hermes/plugin-sdk, react, react/jsx-runtime) — see apps/desktop/src/contrib/runtime-loader.ts.
// A relative import therefore fails the whole pane with "unsupported import: ./camera.js".
//
// So the math lives in src/camera.js (pure, pinned by node --test) and is inlined into
// desktop/plugin.js between two markers. Never edit the generated block by hand.
//
//   node scripts/build-pane.mjs          rewrite the generated block
//   node scripts/build-pane.mjs --check  exit 1 when the block is stale
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
export const PANE = root + 'desktop/plugin.js'
export const SOURCE = root + 'src/camera.js'
export const BEGIN = '// ── BEGIN GENERATED: src/camera.js ──'
export const END = '// ── END GENERATED: src/camera.js ──'

/** src/camera.js with `export ` stripped, ready to sit in the pane's own module scope. */
export function inlineCamera(source = readFileSync(SOURCE, 'utf8')) {
  return source.replace(/^export (?=const |function |class |let )/gm, '').replace(/\s+$/, '')
}

export function renderBlock(source = readFileSync(SOURCE, 'utf8')) {
  return [BEGIN, inlineCamera(source), END].join('\n')
}

/** The pane with its generated block replaced by the current src/camera.js. */
export function build(pane, source = readFileSync(SOURCE, 'utf8')) {
  const begin = pane.indexOf(BEGIN)
  const end = pane.indexOf(END)
  if (begin < 0 || end < begin) throw new Error('generated-block markers are missing from desktop/plugin.js')
  return pane.slice(0, begin) + renderBlock(source) + pane.slice(end + END.length)
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  const pane = readFileSync(PANE, 'utf8')
  const next = build(pane)
  if (process.argv.includes('--check')) {
    if (next !== pane) {
      console.error('desktop/plugin.js is stale: run node scripts/build-pane.mjs')
      process.exit(1)
    }
    console.log('desktop/plugin.js matches src/camera.js')
  } else {
    writeFileSync(PANE, next)
    console.log(`inlined src/camera.js (${inlineCamera().split('\n').length} lines) into desktop/plugin.js`)
  }
}
