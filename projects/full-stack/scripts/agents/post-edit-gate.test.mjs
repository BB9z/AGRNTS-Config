// Black-box tests for post-edit-gate.mjs: spawn the real script, feed it a hook payload on stdin, assert on what it actually prints.
//
// Fixture text lives under post-edit-gate.fixtures/ so cases can be added without touching this file.
//
// Run with: node --test scripts/agents/post-edit-gate.test.mjs

import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { join } from 'node:path'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'

const SCRIPT_PATH = fileURLToPath(
  new URL('./post-edit-gate.mjs', import.meta.url),
)
const FIXTURES_DIR = fileURLToPath(
  new URL('./post-edit-gate.fixtures/', import.meta.url),
)
const TMP_DIR = join(process.cwd(), 'tmp', 'post-edit-gate-test')

before(() => mkdirSync(TMP_DIR, { recursive: true }))
after(() => rmSync(TMP_DIR, { recursive: true, force: true }))

/**
 * @typedef {Object} GatePayload
 * @property {string} tool_name
 * @property {{file_path?: string, new_string?: string, file_content?: string}} tool_input
 */

/**
 * @param {GatePayload} payload
 * @returns {{status: number, stdout: string, stderr: string}}
 */
function runGate(payload) {
  const result = spawnSync(process.execPath, [SCRIPT_PATH], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
  })
  return {
    status: result.status ?? -1,
    stdout: result.stdout,
    stderr: result.stderr,
  }
}

// Each case maps to fixtures/<name>.input.txt and fixtures/<name>.expected.txt (a substring the hint must contain).
//
// No expected file means no output. `ext` picks the language.
const CONTENT_CASES = [
  { name: 'hard-wrap', ext: 'ts' },
  { name: 'verbose-comment', ext: 'ts' },
  { name: 'past-narration', ext: 'ts' },
  { name: 'clean-comment', ext: 'ts' },
  // Same prose as verbose-comment, but heading the file: the larger header budget clears it.
  { name: 'header-comment', ext: 'ts' },
  { name: 'hard-wrap-py', ext: 'py' },
  { name: 'clean-comment-py', ext: 'py' },
  { name: 'clean-comment-yaml', ext: 'yml' },
]

/** @param {string} name */
function readFixture(name) {
  return readFileSync(join(FIXTURES_DIR, `${name}.input.txt`), 'utf8')
}

for (const { name, ext } of CONTENT_CASES) {
  test(`content check: ${name}`, () => {
    const content = readFixture(name)
    const filePath = join(TMP_DIR, `${name}.${ext}`)
    // The hook runs post-edit, so the check reads the already-written file from disk.
    writeFileSync(filePath, content)

    const expectedPath = join(FIXTURES_DIR, `${name}.expected.txt`)
    const expected = existsSync(expectedPath)
      ? readFileSync(expectedPath, 'utf8').trim()
      : null

    const { status, stdout, stderr } = runGate({
      tool_name: 'Edit',
      tool_input: { file_path: filePath, new_string: content },
    })

    assert.equal(status, 0)
    assert.equal(stderr, '')
    if (expected) {
      assert.ok(
        stdout.includes(expected),
        `expected stdout to include "${expected}", got: ${stdout}`,
      )
    } else {
      assert.equal(stdout, '')
    }
  })
}

test('Write payload (full file content) hits the same checks as Edit', () => {
  const content = readFixture('hard-wrap')
  const filePath = join(TMP_DIR, 'hard-wrap-write.ts')
  writeFileSync(filePath, content)

  const { status, stdout, stderr } = runGate({
    tool_name: 'Write',
    tool_input: { file_path: filePath, file_content: content },
  })

  assert.equal(status, 0)
  assert.equal(stderr, '')
  assert.ok(stdout.includes('hard-wrapped'))
})

test('non-matching file extension is skipped without touching disk', () => {
  const content = readFixture('hard-wrap')

  const { status, stdout, stderr } = runGate({
    tool_name: 'Edit',
    tool_input: { file_path: join(TMP_DIR, 'notes.md'), new_string: content },
  })

  assert.equal(status, 0)
  assert.equal(stdout, '')
  assert.equal(stderr, '')
})

test('missing file_path is skipped', () => {
  const { status, stdout, stderr } = runGate({
    tool_name: 'Edit',
    tool_input: {},
  })

  assert.equal(status, 0)
  assert.equal(stdout, '')
  assert.equal(stderr, '')
})
