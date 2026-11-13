// @ts-nocheck
/**
 * Records read-view line-preservation tests (no web test runner in this repo
 * yet — run with vitest/node:test once one is added; kept out of the build
 * because nothing imports this file and tsc skips it via @ts-nocheck).
 *
 * Regression: OCR text with single \n lines collapsed into one <p> in read
 * view while the edit <textarea> showed lines correctly. splitContent must
 * preserve single \n inside a paragraph (trim is outer-only, never join);
 * the read view renders those paragraphs with white-space: pre-wrap
 * (apps/web/src/index.css: .records-doc-body p / .records-conclusion p /
 * .records-encounter-notes).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { splitContent } from './Records'

describe('splitContent single-line preservation', () => {
  it('keeps single \\n lines inside one paragraph verbatim (no joining)', () => {
    const { paragraphs, conclusion } = splitContent('Line one\nLine two\nLine three')
    assert.deepEqual(paragraphs, ['Line one\nLine two\nLine three'])
    assert.equal(conclusion, null)
  })

  it('splits only on blank lines, preserving single lines in each paragraph', () => {
    const { paragraphs } = splitContent('A1\nA2\n\nB1\nB2')
    assert.deepEqual(paragraphs, ['A1\nA2', 'B1\nB2'])
  })

  it('still extracts a conclusion and preserves its single lines', () => {
    const { paragraphs, conclusion } = splitContent('Body line\n\nConclusion: resolved\nfollow-up')
    assert.deepEqual(paragraphs, ['Body line'])
    assert.equal(conclusion, 'resolved\nfollow-up')
  })
})
