// PostToolUse gate for Edit/Write. Checks only the text an edit introduced, so pre-existing violations never fire.
//
// It runs after the tool and cannot undo an edit: `hint` findings become additionalContext, `block` findings go to stderr with exit 2 — both nudge, neither vetoes.

import { appendFileSync, readFileSync } from 'node:fs'
import { basename } from 'node:path'
import { pathToFileURL } from 'node:url'

const THRESHOLD = 80

// Max width of an evidence snippet echoed back to the agent; unrelated to THRESHOLD.
const SNIPPET_MAX = 80

// When true, append each invocation's parsed input and emitted result to the jsonl beside this file.
const DEBUG = false
const DEBUG_LOG = new URL('./post-edit-gate.debug.jsonl', import.meta.url)

// MARK: types

/**
 * @typedef {Object} HookPayload
 * @property {string} [tool_name]
 * @property {{file_path?: string, file_content?: string, new_string?: string}} [tool_input]
 */

/**
 * A single hit, `line` always counted from the start of the file.
 * @typedef {Object} Finding
 * @property {number} line
 * @property {string} message
 */

/**
 * @typedef {Finding & {headline: string|null, severity: 'hint'|'block'}} EnrichedFinding
 */

/**
 * @typedef {Object} Check
 * @property {'hint'|'block'} severity
 * @property {string|null} headline
 * @property {(edit: Edit) => Finding[]} run
 */

/**
 * @typedef {Object} CommentToken
 * @property {'line'|'block'} type
 * @property {number} startLine
 * @property {number} startCol
 * @property {number} endLine
 * @property {number} endCol
 * @property {string} raw
 */

/**
 * @typedef {Object} EvalResult
 * @property {'skip'|'clean'|'block'|'hint'} kind
 * @property {number} exitCode
 * @property {EnrichedFinding[]} [findings]
 * @property {string} [stderr]
 * @property {string} [stdout]
 */

function main() {
  const payload = parsePayload(readStdin())
  const result = evaluate(payload)
  if (DEBUG) appendDebug(payload, result)
  if (result.stderr) process.stderr.write(result.stderr)
  if (result.stdout) process.stdout.write(result.stdout)
  process.exit(result.exitCode)
}

/**
 * @param {HookPayload | null} payload
 * @returns {EvalResult}
 */
function evaluate(payload) {
  const edit = Edit.from(payload)
  if (!edit) return { kind: 'skip', exitCode: 0 }

  /** @type {EnrichedFinding[]} */
  const findings = []
  for (const check of CHECKS)
    for (const finding of check.run(edit))
      findings.push({
        headline: check.headline,
        severity: check.severity,
        ...finding,
      })
  if (findings.length === 0) return { kind: 'clean', exitCode: 0 }

  const blocking = findings.filter((f) => f.severity === 'block')
  if (blocking.length > 0)
    return {
      kind: 'block',
      findings,
      exitCode: 2,
      stderr: `${render(blocking, edit.path)}\n`,
    }

  const stdout = JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext: render(findings, edit.path),
    },
  })
  return { kind: 'hint', findings, exitCode: 0, stdout }
}

// MARK: language

/**
 * @typedef {Object} LanguageSpec
 * @property {RegExp} extensions
 * @property {string} lineComment
 * @property {boolean} isCLike block comments and JSDoc markers
 * @property {RegExp} commentLinePattern opens or continues a comment
 * @property {RegExp} emptyCommentPattern a comment line with no prose
 */

/** Everything the checks need to know about one family of comment syntax. */
class Language {
  /** @param {LanguageSpec} spec */
  constructor(spec) {
    this.extensions = spec.extensions
    this.lineComment = spec.lineComment
    this.isCLike = spec.isCLike
    this.commentLinePattern = spec.commentLinePattern
    this.emptyCommentPattern = spec.emptyCommentPattern
  }

  /**
   * @param {string} path
   * @returns {Language | null}
   */
  static for(path) {
    return LANGUAGES.find((l) => l.extensions.test(path)) ?? null
  }

  /**
   * True when the line is a comment carrying prose, so neither code nor a bare delimiter.
   * @param {string} line
   * @returns {boolean}
   */
  isProseComment(line) {
    const t = line.trimStart()
    return this.commentLinePattern.test(t) && !this.emptyCommentPattern.test(t)
  }

  /**
   * Cheap pre-filter: no comment marker in the text means nothing to judge.
   * @param {string} text
   * @returns {boolean}
   */
  mayContainComment(text) {
    return (
      text.includes(this.lineComment) || (this.isCLike && text.includes('/*'))
    )
  }

  /**
   * Scans source into comment tokens, skipping string and template literals so a marker inside a string is never taken for a comment. Each token carries its line/column span and raw text.
   * @param {string} src
   * @returns {CommentToken[]}
   */
  tokenize(src) {
    /** @type {CommentToken[]} */
    const tokens = []
    const n = src.length
    let i = 0
    let line = 1
    let lineStart = 0
    /**
     * @param {'line'|'block'} type
     * @param {number} startLine
     * @param {number} startCol
     * @param {number} startIdx
     */
    const emit = (type, startLine, startCol, startIdx) =>
      tokens.push({
        type,
        startLine,
        startCol,
        endLine: line,
        endCol: i - lineStart,
        raw: src.slice(startIdx, i),
      })

    while (i < n) {
      const c = src[i]
      if (c === '\n') {
        line++
        i++
        lineStart = i
      } else if (src.startsWith(this.lineComment, i)) {
        const startCol = i - lineStart
        const startIdx = i
        while (i < n && src[i] !== '\n') i++
        emit('line', line, startCol, startIdx)
      } else if (this.isCLike && c === '/' && src[i + 1] === '*') {
        const startLine = line
        const startCol = i - lineStart
        const startIdx = i
        i += 2
        while (i < n && !(src[i] === '*' && src[i + 1] === '/')) {
          if (src[i] === '\n') {
            line++
            i++
            lineStart = i
          } else {
            i++
          }
        }
        i += 2
        emit('block', startLine, startCol, startIdx)
      } else if (c === "'" || c === '"' || c === '`') {
        i++
        while (i < n && src[i] !== c) {
          if (src[i] === '\\') i += 2
          else if (src[i] === '\n') {
            line++
            i++
            lineStart = i
          } else i++
        }
        i++
      } else {
        i++
      }
    }
    return tokens
  }

  /**
   * Splits a token into its physical lines, each stripped of indentation, comment markers, and a leading `*`, leaving only prose.
   * @param {CommentToken} token
   * @returns {string[]}
   */
  proseLines(token) {
    const lines = token.raw.split('\n')
    return lines.map((raw, idx) => {
      const isFirst = idx === 0
      const isLast = idx === lines.length - 1
      let s = raw.trim()
      if (isFirst)
        s = this.isCLike
          ? s.replace(/^(\/\*\*?|\/\/)/, '')
          : s.replace(/^#+/, '')
      if (this.isCLike) {
        if (isLast) s = s.replace(/\*\/$/, '')
        s = s.replace(/^\*\s?/, '')
      }
      return s.trim()
    })
  }
}

const LANGUAGES = [
  new Language({
    extensions: /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/,
    lineComment: '//',
    isCLike: true,
    commentLinePattern: /^(\/\/|\/\*|\*\/|\*)/,
    emptyCommentPattern: /^(?:\/\/|\*)\s*$|^(?:\/\*|\*\/)$/,
  }),
  new Language({
    extensions: /\.(py|ya?ml)$/,
    lineComment: '#',
    isCLike: false,
    commentLinePattern: /^#/,
    emptyCommentPattern: /^#\s*$/,
  }),
]

// MARK: edit

/** One hook invocation: the text an edit introduced, plus the post-edit file it landed in. Both disk reads are lazy and cached, so a clean edit never touches the filesystem. */
class Edit {
  /**
   * Returns null when there is nothing to check — no path, no introduced text, or an unsupported language.
   * @param {HookPayload | null} payload
   * @returns {Edit | null}
   */
  static from(payload) {
    const input = payload?.tool_input
    if (!input?.file_path) return null
    const toolName = payload?.tool_name
    const text = toolName === 'Write' ? input.file_content : input.new_string
    if (typeof text !== 'string' || text.length === 0) return null
    const lang = Language.for(input.file_path)
    if (!lang) return null
    return new Edit(input.file_path, text, lang, toolName)
  }

  /** @type {string | null | undefined} */
  #content

  /** @type {number | undefined} */
  #baseLine

  /**
   * @param {string} path
   * @param {string} text
   * @param {Language} lang
   * @param {string} [toolName]
   */
  constructor(path, text, lang, toolName) {
    this.path = path
    this.text = text
    this.lang = lang
    this.toolName = toolName
  }

  /**
   * The file as it stands after the edit; null when unreadable.
   * @returns {string | null}
   */
  get content() {
    if (this.#content === undefined) {
      try {
        this.#content = readFileSync(this.path, 'utf8')
      } catch {
        this.#content = null
      }
    }
    return this.#content
  }

  /**
   * Full lines preceding `text` in the file, so in-slice lines can be reported as file lines. Write carries the whole file, so its lines already match.
   * @returns {number}
   */
  get baseLine() {
    if (this.#baseLine === undefined) {
      const content = this.toolName === 'Write' ? null : this.content
      // indexOf takes the first occurrence, so a snippet repeated earlier in the file skews the count — acceptable for a hint.
      const idx = content?.indexOf(this.text) ?? -1
      this.#baseLine =
        content && idx > 0 ? content.slice(0, idx).split('\n').length - 1 : 0
    }
    return this.#baseLine
  }
}

// MARK: checks

/** @type {Check} */
const hardWrapCheck = {
  severity: 'hint',
  headline:
    'REVISE NOW: comment is hard-wrapped — reflow each paragraph to one line (project rule).',
  run: findHardWraps,
}

// `headline: null` so findings render as standalone lines, each carrying its own rule text.
/** @type {Check} */
const commentQualityCheck = {
  severity: 'hint',
  headline: null,
  run: findCommentIssues,
}

const CHECKS = [hardWrapCheck, commentQualityCheck]

/**
 * Detects hard-wrapped comments: a comment line filled to THRESHOLD and immediately continued by another comment line. A natural paragraph break never lands at the margin, so a filled line followed by more comment text is a wrap; a line closing on terminal punctuation is a finished sentence instead.
 * @param {Edit} edit
 * @returns {Finding[]}
 */
function findHardWraps(edit) {
  const lines = edit.text.split('\n')
  /** @type {Finding[]} */
  const findings = []
  for (const [i, line] of lines.entries()) {
    const next = lines[i + 1]
    if (next === undefined) break
    if (!edit.lang.isProseComment(line)) continue
    if (line.length < THRESHOLD) continue
    if (/[.!?]$/.test(line.trimEnd())) continue
    if (!edit.lang.isProseComment(next)) continue
    findings.push({
      line: edit.baseLine + i + 1,
      message: line.trim().slice(0, SNIPPET_MAX),
    })
  }
  return findings
}

const ORDINARY_BUDGET = 200
const HEADER_BUDGET = 300
const JSDOC_BASE_BUDGET = 220
const JSDOC_PER_TAG = 90

// Phrases that narrate a past change rather than the code as it stands. Vetted against the whole repo: these two run ~67% precision, the wider set was mostly false positives.
const COMMENT_KEYWORDS = [
  { label: 'no longer', re: /no longer/i },
  { label: 'old behavior', re: /old behavior/i },
]

/**
 * Flags every comment block the introduced text overlaps that exceeds its length budget or narrates a past change.
 * @param {Edit} edit
 * @returns {Finding[]}
 */
function findCommentIssues(edit) {
  if (!edit.lang.mayContainComment(edit.text)) return []
  const content = edit.content
  if (content === null) return []

  const start = edit.baseLine + 1
  const end = edit.baseLine + edit.text.split('\n').length
  /** @type {Finding[]} */
  const findings = []
  for (const block of buildCommentBlocks(content, edit.lang)) {
    if (!block.overlaps(start, end)) continue
    if (block.chars > block.budget)
      findings.push({
        line: block.startLine,
        message:
          "REVISE NOW: comment too verbose — trim to what's necessary for the long term",
      })
    if (block.keywords.length > 0)
      findings.push({
        line: block.startLine,
        message:
          'REVISE NOW: comment narrates a past change — rewrite it from a global perspective, not this patch',
      })
  }
  return findings
}

/** One run of comment prose: adjacent line comments merged, or a single block comment, classified and measured. */
class CommentBlock {
  /**
   * @param {[CommentToken, ...CommentToken[]]} tokens
   * @param {Language} lang
   * @param {string[]} srcLines whole file, for deciding whether the block heads it
   */
  constructor(tokens, lang, srcLines) {
    const first = tokens[0]
    // A single-token group is its own last token.
    const last = tokens.at(-1) ?? first
    const prose = tokens.flatMap((t) => lang.proseLines(t))
    const text = prose.join(' ')
    // Only blank lines above means the block heads the file, which also makes it the first block.
    const isFileHeader =
      pureStart(first, srcLines) &&
      srcLines.slice(0, first.startLine - 1).every((l) => l.trim() === '')
    const isJSDoc =
      !isFileHeader &&
      tokens.length === 1 &&
      first.type === 'block' &&
      first.raw.startsWith('/**')

    this.startLine = first.startLine
    this.endLine = last.endLine
    this.chars = prose.reduce((sum, line) => sum + line.length, 0)
    /** @type {'header'|'jsdoc'|'ordinary'} */
    this.category = isFileHeader ? 'header' : isJSDoc ? 'jsdoc' : 'ordinary'
    // Each `@param`/`@returns`/... line grants extra budget.
    this.jsdocTags = isJSDoc
      ? (first.raw.match(/^\s*\*?\s*@[a-zA-Z]+/gm) ?? []).length
      : 0
    this.keywords = COMMENT_KEYWORDS.filter((k) => k.re.test(text)).map(
      (k) => k.label,
    )
  }

  /**
   * Character budget: file headers 300, JSDoc 220 plus 90 per tag, everything else 200.
   * @returns {number}
   */
  get budget() {
    if (this.category === 'header') return HEADER_BUDGET
    if (this.category === 'jsdoc')
      return JSDOC_BASE_BUDGET + JSDOC_PER_TAG * this.jsdocTags
    return ORDINARY_BUDGET
  }

  /**
   * @param {number} start
   * @param {number} end
   * @returns {boolean}
   */
  overlaps(start, end) {
    return this.startLine <= end && this.endLine >= start
  }
}

/**
 * Splits file source into comment blocks: consecutive tokens that own their whole lines merge into one block, anything else starts a new one.
 * @param {string} src
 * @param {Language} lang
 * @returns {CommentBlock[]}
 */
function buildCommentBlocks(src, lang) {
  const srcLines = src.split('\n')
  /** @type {[CommentToken, ...CommentToken[]][]} */
  const groups = []
  for (const token of lang.tokenize(src)) {
    const group = groups.at(-1)
    const prev = group?.at(-1)
    if (
      group &&
      prev &&
      token.startLine === prev.endLine + 1 &&
      pureEnd(prev, srcLines) &&
      pureStart(token, srcLines)
    )
      group.push(token)
    else groups.push([token])
  }
  return groups.map((tokens) => new CommentBlock(tokens, lang, srcLines))
}

/**
 * Nothing but whitespace stands left of the token's opener.
 * @param {CommentToken} token
 * @param {string[]} srcLines
 * @returns {boolean}
 */
function pureStart(token, srcLines) {
  return (
    (srcLines[token.startLine - 1] ?? '').slice(0, token.startCol).trim() === ''
  )
}

/**
 * Nothing but whitespace stands right of the token's close.
 * @param {CommentToken} token
 * @param {string[]} srcLines
 * @returns {boolean}
 */
function pureEnd(token, srcLines) {
  return (srcLines[token.endLine - 1] ?? '').slice(token.endCol).trim() === ''
}

// MARK: helpers

/**
 * Renders findings as `file:line — message`. A headline, if the check sets one, prints once above its indented hits; without one each finding stands alone.
 * @param {EnrichedFinding[]} findings
 * @param {string} path
 * @returns {string}
 */
function render(findings, path) {
  /** @type {Map<string, string[]>} */
  const groups = new Map()
  for (const f of findings) {
    const key = f.headline ?? ''
    const lines = groups.get(key) ?? []
    lines.push(`${basename(path)}:${f.line} — ${f.message}`)
    groups.set(key, lines)
  }
  return Array.from(groups, ([headline, lines]) =>
    headline
      ? [headline, ...lines.map((l) => `  ${l}`)].join('\n')
      : lines.join('\n'),
  ).join('\n\n')
}

/**
 * @param {string} raw
 * @returns {HookPayload | null}
 */
function parsePayload(raw) {
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function readStdin() {
  try {
    return readFileSync(0, 'utf8')
  } catch {
    return ''
  }
}

/**
 * @param {HookPayload | null} payload
 * @param {EvalResult} result
 */
function appendDebug(payload, result) {
  try {
    appendFileSync(
      DEBUG_LOG,
      `${JSON.stringify({ time: new Date().toISOString(), payload, result })}\n`,
    )
  } catch {
    // best effort; a failed debug write must not crash the hook
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  main()
