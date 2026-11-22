// The pane is 2,500 lines of hand-written DOM in desktop/plugin.js. There is no bundler in front
// of it: the desktop app imports the file as an ES module, so a syntax error there takes the whole
// canvas pane down with no build step to catch it. This is that build step.
import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const tmp = mkdtempSync(join(tmpdir(), 'canvas-parse-'))

// `node --check` parses without running, but only treats a file as an ES module when the name says
// so, and plugin.js is a .js file with no package.json beside it. Copy it to .mjs and check that.
function parseAsModule(relPath) {
  const code = readFileSync(join(root, relPath), 'utf8')
  const file = join(tmp, relPath.replace(/[\\/]/g, '_') + '.mjs')
  writeFileSync(file, code)
  const res = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' })
  return { ok: res.status === 0, stderr: res.stderr, code }
}

test('desktop/plugin.js parses as an ES module', () => {
  const { ok, stderr } = parseAsModule('desktop/plugin.js')
  assert.ok(ok, `plugin.js does not parse:\n${stderr}`)
})

test('src/camera.js parses as an ES module', () => {
  const { ok, stderr } = parseAsModule('src/camera.js')
  assert.ok(ok, `camera.js does not parse:\n${stderr}`)
})
