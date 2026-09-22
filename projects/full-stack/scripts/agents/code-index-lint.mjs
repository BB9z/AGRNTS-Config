// Lints .agents/codebase-index.md and .agents/codebase-index.local.md against the `- path(#symbol)? — note` entry format: format, path existence, symbol existence, cross-file path#symbol duplicates, note token budget, and volatile/status phrasing in notes.

import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

/** @typedef {{ line: number, message: string }} Issue */
/** @typedef {{ file: string, line: number, path: string, symbol: string | undefined }} Entry */

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..', '..')

const INDEX_FILES = [
  '.agents/codebase-index.md',
  '.agents/codebase-index.local.md',
]
const MAX_NOTE_TOKENS = 45

// Temporal/status phrasings that describe current state rather than a durable role
const VOLATILE_NOTE_PHRASES = [
  'commented out',
  'currently',
  'for now',
  'kept for',
  'no caller',
  'no longer',
  'not built',
  'not deleted',
  'only consumer',
  'so far',
  'temporarily',
  'yet',
]
const VOLATILE_NOTE_PATTERNS = VOLATILE_NOTE_PHRASES.map(
  (p) => new RegExp(`\\b${p}\\b`, 'i'),
)

function main() {
  /** @type {Map<string, Issue[]>} */
  const issuesByFile = new Map(INDEX_FILES.map((f) => [f, []]))
  /** @type {Entry[]} */
  const entries = []

  for (const relIndexPath of INDEX_FILES) {
    const issues = issuesByFile.get(relIndexPath) ?? []
    entries.push(...parseIndexFile(relIndexPath, issues))
  }

  for (const issue of findDuplicates(entries)) {
    issuesByFile.get(issue.file)?.push(issue)
  }

  let hasIssues = false
  for (const relIndexPath of INDEX_FILES) {
    const issues = (issuesByFile.get(relIndexPath) ?? []).sort(
      (a, b) => a.line - b.line,
    )
    if (issues.length === 0) continue
    hasIssues = true
    console.log(`\n${relIndexPath}`)
    for (const issue of issues) {
      console.log(`  L${issue.line}: ${issue.message}`)
    }
  }

  if (hasIssues) {
    process.exitCode = 1
  } else {
    console.log('All index entries valid.')
  }
}

/**
 * @param {string} relIndexPath
 * @param {Issue[]} issues
 * @returns {Entry[]}
 */
function parseIndexFile(relIndexPath, issues) {
  const lines = readFileSync(join(root, relIndexPath), 'utf8').split('\n')
  /** @type {Entry[]} */
  const entries = []

  lines.forEach((line, i) => {
    if (!line.startsWith('- ')) return
    const lineNo = i + 1
    const body = line.slice(2)

    const sepIndex = body.indexOf(' — ')
    if (sepIndex === -1) {
      issues.push({
        line: lineNo,
        message: "missing ' — ' separator between location and note",
      })
      return
    }

    const location = body.slice(0, sepIndex)
    const note = body.slice(sepIndex + 3)

    if (/[,{}]/.test(location) || / and /.test(location)) {
      issues.push({
        line: lineNo,
        message: `multiple paths in one entry ("${location}") — split into separate entries`,
      })
    } else {
      const hashParts = location.split('#')
      if (hashParts.length > 2 || hashParts[1]?.includes('/')) {
        issues.push({
          line: lineNo,
          message: `multiple symbols in one entry ("${location}") — split into separate entries`,
        })
      } else {
        const [relPath = '', symbol] = hashParts
        const absPath = join(root, relPath)
        if (!existsSync(absPath)) {
          issues.push({
            line: lineNo,
            message: `path does not exist: ${relPath}`,
          })
        } else {
          if (symbol && !symbolExists(absPath, symbol)) {
            issues.push({
              line: lineNo,
              message: `symbol "${symbol}" not found in ${relPath}`,
            })
          }
          entries.push({
            file: relIndexPath,
            line: lineNo,
            path: relPath,
            symbol,
          })
        }
      }
    }

    if (Math.ceil(note.length / 4) > MAX_NOTE_TOKENS) {
      issues.push({ line: lineNo, message: 'note over token budget' })
    }

    if (VOLATILE_NOTE_PATTERNS.some((re) => re.test(note))) {
      issues.push({
        line: lineNo,
        message: 'note describes current state, not a durable role',
      })
    }
  })

  return entries
}

// A bare path (no #symbol) claims the whole file, so it conflicts with every other entry for that path; a symboled entry conflicts only with an exact symbol match.
/**
 * @param {Entry[]} entries
 * @returns {(Issue & { file: string })[]}
 */
function findDuplicates(entries) {
  /** @type {Map<string, Entry[]>} */
  const byPath = new Map()
  for (const e of entries) {
    if (!byPath.has(e.path)) byPath.set(e.path, [])
    byPath.get(e.path)?.push(e)
  }

  /** @type {(Issue & { file: string })[]} */
  const issues = []
  for (const [path, group] of byPath) {
    if (group.length < 2) continue

    if (group.some((e) => !e.symbol)) {
      for (const e of group) issues.push(duplicateIssue(e, path, group))
      continue
    }

    /** @type {Map<string | undefined, Entry[]>} */
    const bySymbol = new Map()
    for (const e of group) {
      if (!bySymbol.has(e.symbol)) bySymbol.set(e.symbol, [])
      bySymbol.get(e.symbol)?.push(e)
    }
    for (const duplicates of bySymbol.values()) {
      if (duplicates.length < 2) continue
      for (const e of duplicates)
        issues.push(duplicateIssue(e, `${path}#${e.symbol}`, duplicates))
    }
  }
  return issues
}

/**
 * @param {Entry} entry
 * @param {string} label
 * @param {Entry[]} group
 * @returns {Issue & { file: string }}
 */
function duplicateIssue(entry, label, group) {
  const others = group
    .filter((o) => o !== entry)
    .map((o) => `${o.file}:L${o.line}`)
    .join(', ')
  return {
    file: entry.file,
    line: entry.line,
    message: `duplicate entry for "${label}" — also at ${others}`,
  }
}

/**
 * @param {string} absPath
 * @param {string} symbol
 */
function symbolExists(absPath, symbol) {
  const content = readFileSync(absPath, 'utf8')
  const lastSegment = symbol.split('.').at(-1) ?? symbol
  return new RegExp(`\\b${escapeRegExp(lastSegment)}\\b`).test(content)
}

/** @param {string} s */
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

main()
