import { useEffect, useRef, useState } from 'react'
import { authClient } from '../lib/auth'
import QuestionChart, { extractChartBlock, stripChartBlocks, type ChartSpec } from '../components/QuestionChart'

/**
 * Questionnarium (0.12.0) — ask health questions grounded in the patient's record.
 *
 * Contract with the backend router `questionnarium`:
 *   GET  /api/v1/questionnarium/models
 *        -> { models: [{ id: "default"|"ling", label, ready, note? }] }
 *   POST /api/v1/questionnarium/ask  { question, model?, history? }
 *        -> { answer, model, chart?, sources?, latency_ms? }
 *        or { error, ... } with a non-2xx status.
 *
 * The first Ling request can take ~10 s while the model loads: the UI shows a
 * loading state and a clear completion signal (elapsed time + model badge).
 */

type ModelId = 'default' | 'ling'

interface ModelInfo {
  id: string
  label: string
  ready?: boolean
  note?: string | null
}

export interface ChatTurn {
  id: number
  role: 'user' | 'assistant'
  text: string
  model?: string
  chart?: ChartSpec | null
  elapsedMs?: number | null
  sources?: string[]
  error?: boolean
}

const SUGGESTED = [
  'Summarise my recent blood-pressure trend',
  'What do my latest lab results say?',
  'Am I due for any vaccinations?',
]

function fmtElapsed(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return ''
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

/** Minimal markdown-ish rendering: paragraphs + bullets, no HTML injection. */
function AnswerText({ text }: { text: string }) {
  const lines = text.split('\n')
  const blocks: React.ReactNode[] = []
  let list: string[] = []
  const flush = (key: number) => {
    if (!list.length) return
    blocks.push(
      <ul key={`ul-${key}`} style={{ margin: '6px 0', paddingLeft: 18 }}>
        {list.map((li, i) => (
          <li key={i} style={{ margin: '3px 0' }}>{li}</li>
        ))}
      </ul>,
    )
    list = []
  }
  lines.forEach((raw, i) => {
    const line = raw.trim()
    if (!line) { flush(i); return }
    if (/^[-*•]\s+/.test(line)) { list.push(line.replace(/^[-*•]\s+/, '')); return }
    if (/^\d+[.)]\s+/.test(line)) { list.push(line.replace(/^\d+[.)]\s+/, '')); return }
    flush(i)
    // bold **x** inline
    const parts = line.split(/\*\*(.+?)\*\*/g).map((p, j) =>
      j % 2 === 1 ? <strong key={j}>{p}</strong> : p,
    )
    blocks.push(<p key={`p-${i}`} style={{ margin: '6px 0' }}>{parts}</p>)
  })
  flush(lines.length)
  return <div style={{ fontSize: 14, lineHeight: 1.55 }}>{blocks}</div>
}

export default function Questionnarium() {
  const [models, setModels] = useState<ModelInfo[]>([])
  const [model, setModel] = useState<ModelId>('default')
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [apiPending, setApiPending] = useState(false)
  const idRef = useRef(1)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    authClient.api('/questionnarium/models')
      .then((d) => {
        const list: ModelInfo[] = d.models || []
        setModels(list)
        const ling = list.find((m) => m.id === 'ling')
        if (ling && !ling.ready) setStatus('Ling is not loaded yet — the first request will take longer while it starts.')
      })
      .catch(() => { /* models endpoint optional; toggle still works */ })
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [turns, busy])

  async function ask(question: string) {
    const q = question.trim()
    if (!q || busy) return
    setError(null); setStatus(null)
    setBusy(true)
    setTurns((t) => [...t, { id: idRef.current++, role: 'user', text: q }])
    setInput('')
    const started = Date.now()
    try {
      const res = await authClient.api('/questionnarium/ask', {
        method: 'POST',
        body: JSON.stringify({ question: q, model }),
      })
      const elapsed = res.latency_ms ?? Date.now() - started
      const rawAnswer = String(res.answer ?? '')
      const chart = res.chart ?? extractChartBlock(rawAnswer)
      const answerText = res.chart ? rawAnswer : stripChartBlocks(rawAnswer)
      setTurns((t) => [...t, {
        id: idRef.current++,
        role: 'assistant',
        text: answerText || '(no answer returned)',
        model: res.model ?? model,
        chart: chart ?? null,
        elapsedMs: elapsed,
        sources: Array.isArray(res.sources) ? res.sources : [],
      }])
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      // 404/405 while the backend router is still landing — say so plainly.
      if (/404|405|not found|method not allowed/i.test(msg)) setApiPending(true)
      setError(msg)
      setTurns((t) => [...t, { id: idRef.current++, role: 'assistant', text: msg, error: true }])
    } finally {
      setBusy(false)
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      ask(input)
    }
  }

  const activeLabel = models.find((m) => m.id === model)?.label ?? (model === 'ling' ? 'Ling' : 'Default')

  return (
    <section className="section">
      <div className="form-card">
        {/* header row: model toggle + status */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>Questionnarium</h3>
          <div className="form-actions" style={{ marginBottom: 0 }}>
            <span className="muted" style={{ fontSize: 12 }}>Model:</span>
            <button
              className={model === 'default' ? '' : 'secondary'}
              onClick={() => setModel('default')}
              aria-pressed={model === 'default'}
            >
              Default
            </button>
            <button
              className={model === 'ling' ? '' : 'secondary'}
              onClick={() => setModel('ling')}
              aria-pressed={model === 'ling'}
              title="Healthcare-specialised model — first request may take ~10 s to load"
            >
              Ling
            </button>
          </div>
        </div>

        <p className="muted" style={{ fontSize: 12, marginTop: 0, marginBottom: 10 }}>
          Ask about your health using your recorded data. {activeLabel === 'Ling' && 'The healthcare model is slower on first use.'}
        </p>

        {/* chat transcript */}
        <div
          ref={scrollRef}
          aria-label="Conversation"
          style={{
            maxHeight: 420, overflowY: 'auto', border: '1px solid var(--color-border, #e2e7ea)',
            borderRadius: 10, padding: 10, background: 'var(--color-surface-2, #f7f9fa)', marginBottom: 10,
            display: 'flex', flexDirection: 'column', gap: 10,
          }}
        >
          {turns.length === 0 && (
            <div className="muted" style={{ fontSize: 13 }}>
              <p style={{ margin: '0 0 8px' }}>Ask a question about your record. For example:</p>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {SUGGESTED.map((s) => (
                  <button key={s} className="secondary small" onClick={() => ask(s)}>{s}</button>
                ))}
              </div>
            </div>
          )}

          {turns.map((t) => (
            <div key={t.id} style={{ display: 'flex', flexDirection: t.role === 'user' ? 'row-reverse' : 'row', gap: 8 }}>
              <div
                style={{
                  maxWidth: '82%', padding: '8px 10px', borderRadius: 10,
                  background: t.role === 'user' ? 'var(--color-accent, #0d7c6b)' : 'var(--color-surface, #fff)',
                  color: t.role === 'user' ? '#fff' : 'inherit',
                  border: t.role === 'assistant' ? '1px solid var(--color-border, #e2e7ea)' : 'none',
                  boxShadow: 'var(--shadow-card)',
                }}
              >
                {t.role === 'assistant' && (t.model || t.elapsedMs != null) && (
                  <div className="muted" style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>
                    {t.model}{t.elapsedMs != null ? ` · ${fmtElapsed(t.elapsedMs)}` : ''}
                    {t.error ? ' · failed' : ''}
                  </div>
                )}
                {t.error ? (
                  <div style={{ fontSize: 13, color: 'var(--color-danger, #c0392b)' }}>{t.text}</div>
                ) : (
                  <>
                    {t.role === 'user' ? <div style={{ fontSize: 14, whiteSpace: 'pre-wrap' }}>{t.text}</div> : <AnswerText text={t.text} />}
                    {t.chart && <QuestionChart spec={t.chart} />}
                    {t.sources && t.sources.length > 0 && (
                      <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
                        Based on: {t.sources.join(', ')}
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          ))}

          {busy && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--color-muted, #6b7a83)', fontSize: 13 }}>
              <span className="spinner" aria-hidden="true" />
              <span>
                Thinking{model === 'ling' ? ' — Ling may need to load first (up to ~10 s)' : ''}…
              </span>
            </div>
          )}
        </div>

        {/* composer */}
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            placeholder="Ask about your health record…"
            aria-label="Your question"
            style={{ flex: 1, resize: 'vertical', minHeight: 44, padding: '8px 10px', border: '1px solid var(--color-border, #e2e7ea)', borderRadius: 10, font: 'inherit', fontSize: 14 }}
          />
          <button onClick={() => ask(input)} disabled={busy || !input.trim()}>
            {busy ? 'Sending…' : 'Send'}
          </button>
        </div>
        <p className="muted" style={{ fontSize: 11, margin: '6px 0 0' }}>
          Enter to send · Shift+Enter for a new line. Answers come from your own records — not a diagnosis.
        </p>

        {status && <div className="status" style={{ marginTop: 10 }}>{status}</div>}
        {error && (
          <div className="status" style={{ marginTop: 10, background: 'var(--color-danger-soft, #fbe9e7)', color: 'var(--color-danger, #c0392b)' }}>
            {apiPending
              ? 'Questionnarium API not yet available (/questionnarium/ask pending backend). The chat will work as soon as it lands — no data is fabricated in the meantime.'
              : error}
            <div style={{ marginTop: 6 }}><button className="secondary small" onClick={() => ask(input || turns.filter((t) => t.role === 'user').slice(-1)[0]?.text || '')}>Retry</button></div>
          </div>
        )}
      </div>
    </section>
  )
}
