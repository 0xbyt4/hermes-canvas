// Tests for the desktop half's internationalisation.
//
// The pane resolves every visible string through ctx.i18n.t, which follows the
// app's active locale. These tests load desktop/plugin.js in a VM with a fake
// ctx and check that both bundles reach the app, every key is translated in
// both languages, and formatted messages keep their arguments in order.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const SOURCE = fs.readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')

/** Load the plugin with a fake `ctx` and a fake `ctx.i18n` that resolves keys
 *  the way the app does: active locale → this plugin's English → the key. */
function load(locale) {
  const source = SOURCE.replace(/import\s*\{[^}]+\}\s*from '@hermes\/plugin-sdk'\r?\n/, '')
    .replace(/import\s*\{[^}]+\}\s*from 'react'\r?\n/, '')
    .replace(/import\s*\{[^}]+\}\s*from 'react\/jsx-runtime'\r?\n/, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(`
      globalThis.__i18n = { MESSAGES, translate: (key, ...args) => tr(key, ...args) }
    `)

  const context = {
    clearTimeout: () => undefined,
    console,
    host: { navigate: () => undefined, notify: () => undefined },
    jsx: () => null,
    jsxs: () => null,
    Math,
    Object,
    setInterval: () => 1,
    setTimeout: () => 1,
    TRANSCRIPT_DIRECTIVE_AREA: 'transcript-directive',
  }

  context.globalThis = context
  vm.runInNewContext(source, context, { filename: 'desktop/plugin.js' })

  const registrations = []
  let bundles = null
  const ctx = {
    i18n: {
      register: value => {
        bundles = value

        return () => undefined
      },
      // Same lookup Hermes does (apps/desktop/src/i18n/runtime.ts): walk the key as a
      // dot-path into the registered tree, active locale first, then English, then key.
      t: (key, ...args) => {
        const walk = tree => key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), tree)

        for (const tree of [bundles?.[locale], bundles?.en]) {
          const value = walk(tree)

          if (typeof value === 'string') return value
          if (typeof value === 'function') return value(...args)
        }

        return key
      }
    },
    onDispose: () => undefined,
    onEvent: () => () => undefined,
    register: def => {
      registrations.push(def)

      return () => undefined
    },
    setInterval: () => 1,
  }

  context.plugin.register(ctx)

  return { bundles, registrations, translate: context.__i18n.translate }
}

const labelOf = (registrations, id) => registrations.find(entry => entry.id === id)

test('the plugin hands both locales to the app and the pane title follows it', () => {
  for (const [locale, expected] of [['en', 'Canvas'], ['zh', '画布']]) {
    const { bundles, registrations } = load(locale)

    assert.ok(bundles, 'the bundles reach ctx.i18n.register')
    assert.deepEqual(Object.keys(bundles).sort(), ['en', 'zh'])
    assert.equal(labelOf(registrations, 'pane').title, expected)
  }
})

test('every English key has a Chinese one, and neither bundle carries a stray key', () => {
  const flat = SOURCE.slice(SOURCE.indexOf('const MESSAGES = {'), SOURCE.indexOf('function messageTree'))
  const keys = [...new Set([...flat.matchAll(/^\s+'?([A-Za-z][\w.]*)'?: /gm)].map(match => match[1]))].filter(key => !['en', 'zh'].includes(key))

  assert.ok(keys.length > 50, 'the bundle covers the pane copy')

  // Deep-equal the flattened bundles, so a stray key in one locale is caught.
  const { bundles } = load('en')
  const flatten = (tree, prefix = '') => Object.entries(tree).flatMap(([key, value]) =>
    value && typeof value === 'object' ? flatten(value, `${prefix}${key}.`) : [`${prefix}${key}`])
  assert.deepEqual(flatten(bundles.zh).sort(), flatten(bundles.en).sort())
})

test('every key resolves through the nested lookup Hermes uses, in both languages', () => {
  for (const locale of ['en', 'zh']) {
    const { translate } = load(locale)
    const flat = SOURCE.slice(SOURCE.indexOf('const MESSAGES = {'), SOURCE.indexOf('function messageTree'))
    const keys = [...new Set([...flat.matchAll(/^\s+'?([A-Za-z][\w.]*)'?: /gm)].map(match => match[1]))].filter(key => !['en', 'zh'].includes(key))

    assert.ok(keys.length > 50)

    for (const key of keys) {
      // A key that came back as itself was not found in the tree.
      assert.notEqual(translate(key, 3), key, `${locale}: ${key}`)
    }
  }
})

test('formatted messages keep their arguments in the same order', () => {
  const en = load('en')
  const zh = load('zh')

  assert.equal(en.translate('card.done', 3, 7), '3/7 done')
  assert.equal(en.translate('status.questions', 1), '1 question to answer')
  assert.equal(en.translate('status.questions', 2), '2 questions to answer')
  assert.equal(zh.translate('card.done', 3, 7), '3/7 已完成')
  assert.equal(zh.translate('status.questions', 1), '有 1 个问题待回答')
  assert.equal(zh.translate('add.newAfter', '登录'), '在「登录」之后的新盒子')
})

test('the app name and the key UI copy are translated to Chinese', () => {
  const zh = load('zh')

  assert.equal(labelOf(zh.registrations, 'pane').title, '画布')
  assert.equal(zh.translate('app.name'), 'Hermes 画布')
  assert.equal(zh.translate('pane.addBox'), '+ 盒子')
  assert.equal(zh.translate('status.shipped'), '已交付：每个盒子都通过了检查')
  assert.equal(zh.translate('card.hint'), '在画布上调整计划：编辑会立即送达智能体。')
})
