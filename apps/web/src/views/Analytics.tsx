import { useEffect, useMemo, useState } from 'react'
import { authClient } from '../lib/auth'
import Sparkline from '../components/Sparkline'

interface Finding {
  id: string
  kind: string
  metric: string
  direction: string | null
  anchor_date: string | null
  before: number | null
  after: number | null
  delta: number | null
  unit: string
  confidence: string
  related_findings: string[]
  text: string
  stats?: Record<string, unknown>
}
interface Unknown {
  code: string
  metric: string | null
  text: string
}
interface RunSummary {
  id: string
  kind: string
  status: string
  period_start: string
  period_end: string
  created_at: string
  findings_count?: number
  error?: string | null
}
interface GenFile {
  id: string
  kind: string
  status: string
  filename: string | null
  error: string | null
  created_at?: string
  expires_at?: string | null
  size_bytes?: number | null
}

type Tab = 'analyse' | 'visit' | 'history' | 'data'

const PRESETS = [
  { key: '30d', label: '30d' },
  { key: '90d', label: '90d' },
  { key: '6m', label: '6m' },
  { key: '12m', label: '12m' },
  { key: 'all', label: 'All' },
  { key: 'since_last_encounter', label: 'Since last visit' },
]

const PROVIDERS = ['gp', 'cardiology', 'endocrinology', 'gastroenterology', 'nephrology',
  'sleep', 'nutrition', 'sports_medicine', 'psychiatry', 'other']

/** Wave 3 (spec §3.11): Analyse · Doctor visit · History · Data & backup. */
export default function Analytics() {
  const [tab, setTab] = useState<Tab>('analyse')

  return (
    <div>
      <h2 style={{ margin: '8px 0' }}>Analytics</h2>
      <div className="form-actions" style={{ marginBottom: 12 }}>
        {(['analyse', 'visit', 'history', 'data'] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? '' : 'secondary'} onClick={() => setTab(t)}>
            {t === 'analyse' ? 'Analyse' : t === 'visit' ? 'Doctor visit' : t === 'history' ? 'History' : 'Data & backup'}
          </button>
        ))}
      </div>
      {tab === 'analyse' && <AnalyseTab />}
      {tab === 'visit' && <VisitTab />}
      {tab === 'history' && <HistoryTab />}
      {tab === 'data' && <DataTab />}
    </div>
  )
}

// ── Analyse ─────────────────────────────────────────────────────────────────

function AnalyseTab() {
  const [preset, setPreset] = useState('90d')
  const [metrics, setMetrics] = useState<string[]>([])
  const [catalogue, setCatalogue] = useState<{ key: string; label: string; unit: string; group: string; tracked: boolean }[]>([])
  const [run, setRun] = useState<RunSummary | null>(null)
  const [result, setResult] = useState<any>(null)
  const [files, setFiles] = useState<GenFile[]>([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  useEffect(() => {
    authClient.api('/analytics/catalogue')
      .then((d) => setCatalogue(d.metrics || []))
      .catch((e) => setMsg(String(e)))
  }, [])

  async function runAnalysis() {
    setBusy(true)
    setMsg(null)
    setResult(null)
    setFiles([])
    try {
      const body: any = { kind: 'general', period: { preset } }
      if (metrics.length) body.metrics = metrics
      const d = await authClient.api('/analytics/runs', { method: 'POST', body: JSON.stringify(body) })
      let current = d.run
      setRun(current)
      // poll every 2s, stop after 120s (spec §3.11)
      const started = Date.now()
      while (current.status === 'queued' || current.status === 'running') {
        if (Date.now() - started > 120000) {
          setMsg('Still running — check History in a minute.')
          setBusy(false)
          return
        }
        await new Promise((r) => setTimeout(r, 2000))
        const got = await authClient.api(`/analytics/runs/${current.id}`)
        current = got.run
        setRun(current)
      }
      const detail = await authClient.api(`/analytics/runs/${current.id}`)
      setResult(detail.run.result)
      setFiles(detail.run.files || [])
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(false)
    }
  }

  const groups = useMemo(() => {
    const g: Record<string, typeof catalogue> = {}
    catalogue.forEach((m) => {
      ;(g[m.group] = g[m.group] || []).push(m)
    })
    return g
  }, [catalogue])

  return (
    <div>
      <section className="section">
        <div className="form-card">
          <h3 style={{ marginTop: 0 }}>Period</h3>
          <div className="form-actions">
            {PRESETS.map((p) => (
              <button key={p.key} className={preset === p.key ? '' : 'secondary'} onClick={() => setPreset(p.key)}>
                {p.label}
              </button>
            ))}
          </div>
          <h3>Metrics ({metrics.length ? `${metrics.length} selected` : 'all tracked'})</h3>
          {Object.entries(groups).map(([group, ms]) => (
            <div key={group} style={{ marginBottom: 6 }}>
              <strong style={{ fontSize: 12 }}>{group}</strong>{' '}
              {ms.map((m) => (
                <button
                  key={m.key}
                  className={metrics.includes(m.key) ? '' : 'secondary'}
                  style={{ margin: 2, fontSize: 11, opacity: m.tracked ? 1 : 0.45 }}
                  disabled={!m.tracked}
                  title={m.tracked ? m.key : `${m.key} — no data`}
                  onClick={() =>
                    setMetrics((cur) => (cur.includes(m.key) ? cur.filter((k) => k !== m.key) : [...cur, m.key]))
                  }
                >
                  {m.label || m.key}
                  {!m.tracked && ' (no data)'}
                </button>
              ))}
            </div>
          ))}
          <div className="form-actions" style={{ marginTop: 12 }}>
            <button onClick={runAnalysis} disabled={busy}>{busy ? 'Analysing…' : 'Run analysis'}</button>
            {metrics.length > 0 && (
              <button className="secondary" onClick={() => setMetrics([])}>Clear selection</button>
            )}
          </div>
          {msg && <div className="status">{msg}</div>}
        </div>
      </section>
      {run && <ResultView run={run} result={result} files={files} />}
    </div>
  )
}

// ── Shared result view (spec §3.11 §2) ──────────────────────────────────────

function ResultView({ run, result, files }: { run: RunSummary; result: any; files: GenFile[] }) {
  const [msg, setMsg] = useState<string | null>(null)
  const [series, setSeries] = useState<Record<string, { date: string; value: number | null }[]>>({})
  const findings: Finding[] = result?.findings || []
  const unknowns: Unknown[] = result?.unknowns || []

  useEffect(() => {
    // sparkline data per finding metric (spec §3.11: marker on anchor_date)
    const keys = Array.from(new Set(findings.map((f) => f.metric)))
    keys.forEach((k) => {
      authClient
        .api(`/analytics/runs/${run.id}/series/${k}`)
        .then((d) => setSeries((s) => ({ ...s, [k]: d.points || [] })))
        .catch(() => {})
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.id])

  async function downloadPdf() {
    setMsg(null)
    try {
      const d = await authClient.api(`/analytics/runs/${run.id}/report`, { method: 'POST', body: JSON.stringify({}) })
      const fid = d.file.id
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 2000))
        const f = await authClient.api(`/files/${fid}`)
        const gf = f.file
        if (gf.status === 'ready') {
          window.open(`${(window as any).__API_BASE__ || ''}/api/v1/files/${fid}/download`, '_blank')
          setMsg('PDF ready — downloading…')
          return
        }
        if (gf.status === 'failed') {
          setMsg(`PDF failed: ${gf.error}`)
          return
        }
      }
      setMsg('PDF still building — check Data & backup later.')
    } catch (e) {
      setMsg(String(e))
    }
  }

  if (run.status === 'failed') return <div className="status">Run failed: {run.error}</div>
  return (
    <section className="section">
      <div className="form-card">
        <h3 style={{ marginTop: 0 }}>
          {run.kind === 'visit' ? 'Visit summary' : 'Analysis'} · {run.period_start} → {run.period_end}{' '}
          <span className="muted" style={{ fontSize: 12 }}>({run.status})</span>
        </h3>
        <h3>Findings ({findings.length})</h3>
        {findings.length === 0 && <p className="muted">No changes detected.</p>}
        {findings.map((f) => (
          <div key={f.id} className="card" style={{ marginBottom: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <strong>{f.metric}</strong>
              <span className="muted" style={{ fontSize: 12 }}>{f.confidence}</span>
            </div>
            <p style={{ margin: '4px 0' }}>{f.text}</p>
            {f.anchor_date && series[f.metric] && (
              <Sparkline
                points={series[f.metric]}
                markers={[{ date: f.anchor_date, kind: f.kind, finding_id: f.id }]}
              />
            )}
          </div>
        ))}
        <h3>What the data cannot tell</h3>
        <ul className="muted">
          {unknowns.length === 0 && <li>Nothing flagged.</li>}
          {unknowns.map((u, i) => (
            <li key={i}>{u.text}</li>
          ))}
        </ul>
        {result?.narrative_status === 'ok' && result?.narrative_text && (
          <div className="card">
            <div className="label" style={{ fontSize: 11, textTransform: 'uppercase' }}>
              AI-written summary of the findings above
            </div>
            <p>{result.narrative_text}</p>
          </div>
        )}
        <div className="form-actions" style={{ marginTop: 10 }}>
          <button className="secondary" onClick={downloadPdf}>Download PDF</button>
        </div>
        {files.filter((f) => f.kind.includes('report')).map((f) => (
          <p key={f.id} className="muted" style={{ fontSize: 12 }}>
            Report: {f.status} {f.filename}
          </p>
        ))}
        {msg && <div className="status">{msg}</div>}
      </div>
    </section>
  )
}

// ── Doctor visit (spec §3.8) ────────────────────────────────────────────────

function VisitTab() {
  const [visitDate, setVisitDate] = useState('')
  const [provider, setProvider] = useState('gp')
  const [facility, setFacility] = useState('')
  const [purpose, setPurpose] = useState('')
  const [questions, setQuestions] = useState<string[]>([''])
  const [period, setPeriod] = useState('since_last_encounter')
  const [suggested, setSuggested] = useState<string[]>([])
  const [included, setIncluded] = useState<string[]>([])
  const [run, setRun] = useState<RunSummary | null>(null)
  const [result, setResult] = useState<any>(null)
  const [files, setFiles] = useState<GenFile[]>([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  function addQ() {
    setQuestions((q) => [...q, ''])
  }
  function setQ(i: number, v: string) {
    setQuestions((q) => q.map((x, j) => (j === i ? v : x)))
  }

  async function suggest() {
    try {
      const d = await authClient.api('/analytics/visit-suggest', {
        method: 'POST',
        body: JSON.stringify({ purpose, provider_type: provider }),
      })
      setSuggested(d.suggested_metrics || [])
      setIncluded(d.suggested_metrics || [])
    } catch (e) {
      setMsg(String(e))
    }
  }

  async function prepare() {
    setBusy(true)
    setMsg(null)
    try {
      const body = {
        kind: 'visit',
        period: { preset: period },
        intent: {
          provider_type: provider,
          facility: facility || null,
          visit_date: visitDate || null,
          purpose,
          questions: questions.filter((q) => q.trim()),
          include_metrics: included,
          exclude_metrics: suggested.filter((s) => !included.includes(s)),
          include_meds: true,
          include_health_card: true,
        },
      }
      const d = await authClient.api('/analytics/runs', { method: 'POST', body: JSON.stringify(body) })
      let current = d.run
      setRun(current)
      const started = Date.now()
      while (current.status === 'queued' || current.status === 'running') {
        if (Date.now() - started > 120000) {
          setMsg('Still running — check History in a minute.')
          setBusy(false)
          return
        }
        await new Promise((r) => setTimeout(r, 2000))
        const got = await authClient.api(`/analytics/runs/${current.id}`)
        current = got.run
        setRun(current)
      }
      const detail = await authClient.api(`/analytics/runs/${current.id}`)
      setResult(detail.run.result)
      setFiles(detail.run.files || [])
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <section className="section">
        <div className="form-card">
          <div style={{ display: 'grid', gap: 8 }}>
            <label>
              Visit date
              <input type="date" value={visitDate} onChange={(e) => setVisitDate(e.target.value)} style={{ width: '100%' }} />
            </label>
            <label>
              Provider
              <select value={provider} onChange={(e) => setProvider(e.target.value)} style={{ width: '100%' }}>
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>{p.replace('_', ' ')}</option>
                ))}
              </select>
            </label>
            <label>
              Facility
              <input value={facility} onChange={(e) => setFacility(e.target.value)} placeholder="KBC Zagreb" style={{ width: '100%' }} />
            </label>
            <label>
              Purpose (your own words)
              <textarea value={purpose} onChange={(e) => setPurpose(e.target.value)} rows={3} style={{ width: '100%' }} />
            </label>
            <div>
              My questions
              {questions.map((q, i) => (
                <input key={i} value={q} onChange={(e) => setQ(i, e.target.value)} style={{ width: '100%', marginBottom: 4 }} />
              ))}
              <button className="secondary" onClick={addQ}>+ Add question</button>
            </div>
            <label>
              Period
              <select value={period} onChange={(e) => setPeriod(e.target.value)} style={{ width: '100%' }}>
                <option value="since_last_encounter">Since last visit</option>
                <option value="90d">90 days</option>
                <option value="6m">6 months</option>
                <option value="12m">12 months</option>
              </select>
            </label>
          </div>
          <div className="form-actions" style={{ marginTop: 10 }}>
            <button className="secondary" onClick={suggest} disabled={!purpose.trim()}>Suggest metrics</button>
            <button onClick={prepare} disabled={busy || !purpose.trim()}>
              {busy ? 'Preparing…' : 'Prepare summary'}
            </button>
          </div>
          {suggested.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <strong style={{ fontSize: 12 }}>Metric suggestions (uncheck to exclude):</strong>
              <div>
                {suggested.map((s) => (
                  <button
                    key={s}
                    className={included.includes(s) ? '' : 'secondary'}
                    style={{ margin: 2, fontSize: 11 }}
                    onClick={() =>
                      setIncluded((cur) => (cur.includes(s) ? cur.filter((k) => k !== s) : [...cur, s]))
                    }
                  >
                    {s}
                  </button>
                ))}
              </div>
              <p className="muted" style={{ fontSize: 12 }}>Nothing is included without you seeing and confirming it here.</p>
            </div>
          )}
          {msg && <div className="status">{msg}</div>}
        </div>
      </section>
      {run && <ResultView run={run} result={result} files={files} />}
    </div>
  )
}

// ── History ─────────────────────────────────────────────────────────────────

function HistoryTab() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [msg, setMsg] = useState<string | null>(null)

  async function load() {
    try {
      const d = await authClient.api('/analytics/runs?limit=20')
      setRuns(d.runs || [])
    } catch (e) {
      setMsg(String(e))
    }
  }
  useEffect(() => {
    load()
  }, [])

  async function del(id: string) {
    try {
      await authClient.api(`/analytics/runs/${id}`, { method: 'DELETE' })
      await load()
    } catch (e) {
      setMsg(String(e))
    }
  }

  return (
    <section className="section">
      <div className="form-card">
        <h3 style={{ marginTop: 0 }}>Past runs</h3>
        {msg && <div className="status">{msg}</div>}
        <table>
          <thead>
            <tr><th>Kind</th><th>Period</th><th>Created</th><th>Status</th><th>Findings</th><th></th></tr>
          </thead>
          <tbody>
            {runs.length === 0 && (
              <tr><td colSpan={6} className="muted">No runs yet.</td></tr>
            )}
            {runs.map((r) => (
              <tr key={r.id}>
                <td>{r.kind}</td>
                <td>{r.period_start} → {r.period_end}</td>
                <td>{String(r.created_at).slice(0, 16).replace('T', ' ')}</td>
                <td>{r.status}</td>
                <td>{r.findings_count ?? '—'}</td>
                <td>
                  <button className="danger" style={{ fontSize: 11 }} onClick={() => del(r.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// ── Data & backup (spec §4.5) — dashboard-only per amendment A2 ─────────────

function DataTab() {
  const [includeFiles, setIncludeFiles] = useState(true)
  const [wantCsv, setWantCsv] = useState(true)
  const [exports, setExports] = useState<GenFile[]>([])
  const [reports, setReports] = useState<GenFile[]>([])
  const [backups, setBackups] = useState<GenFile[] | null>(null)
  const [backupStatus, setBackupStatus] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  async function load() {
    try {
      const [ex, rp, bs] = await Promise.all([
        authClient.api('/files?kind=data_export'),
        authClient.api('/files?kind=analysis_report,visit_report,health_card'),
        authClient.api('/backups').catch(() => null),
      ])
      setExports(ex.files || [])
      setReports(rp.files || [])
      if (bs) setBackups(bs.files || [])
      const st = await authClient.api('/backups/status').catch(() => null)
      setBackupStatus(st)
    } catch (e) {
      setMsg(String(e))
    }
  }
  useEffect(() => {
    load()
  }, [])

  async function exportData() {
    setBusy(true)
    setMsg(null)
    try {
      const format = ['json', ...(wantCsv ? ['csv'] : [])]
      await authClient.api('/exports/bundle', {
        method: 'POST',
        body: JSON.stringify({ include_files: includeFiles, format }),
      })
      await load()
      setMsg('Export queued — download it below when ready (expires after 7 days).')
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function download(f: GenFile) {
    try {
      const d = await authClient.api(`/files/${f.id}`)
      if (d.file && d.file.status === 'ready') {
        window.open(`${(window as any).__API_BASE__ || ''}/api/v1/files/${f.id}/download`, '_blank')
      } else {
        setMsg(`File status: ${d.file?.status} ${d.file?.error || ''}`)
      }
    } catch (e) {
      setMsg(String(e))
    }
  }

  async function backupAction(kind: 'run' | 'restore-test') {
    setBusy(true)
    try {
      await authClient.api(`/backups/${kind}`, { method: 'POST' })
      await load()
      setMsg(kind === 'run' ? 'Backup queued.' : 'Restore test queued.')
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      {msg && <div className="status">{msg}</div>}
      <section className="section">
        <div className="form-card">
          <h3 style={{ marginTop: 0 }}>My data</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            A full export of everything drHiro stores about you. Downloads here in the dashboard only — it is never
            sent over Telegram.
          </p>
          <label style={{ display: 'block' }}>
            <input type="checkbox" checked={includeFiles} onChange={(e) => setIncludeFiles(e.target.checked)} /> include
            document scans
          </label>
          <label style={{ display: 'block' }}>
            <input type="checkbox" checked={wantCsv} onChange={(e) => setWantCsv(e.target.checked)} /> include CSV (in
            addition to JSON)
          </label>
          <div className="form-actions" style={{ marginTop: 10 }}>
            <button onClick={exportData} disabled={busy}>{busy ? 'Queueing…' : 'Export my data'}</button>
          </div>
          <table style={{ marginTop: 10 }}>
            <thead>
              <tr><th>Export</th><th>Created</th><th>Status</th><th>Expires</th><th></th></tr>
            </thead>
            <tbody>
              {exports.length === 0 && <tr><td colSpan={5} className="muted">No exports yet.</td></tr>}
              {exports.map((f) => (
                <tr key={f.id}>
                  <td>{f.filename || f.id.slice(0, 8)}</td>
                  <td>{String(f.created_at).slice(0, 16).replace('T', ' ')}</td>
                  <td>{f.status}</td>
                  <td>{f.expires_at ? String(f.expires_at).slice(0, 10) : '—'}</td>
                  <td>
                    {f.status === 'ready' && (
                      <button className="secondary" style={{ fontSize: 11 }} onClick={() => download(f)}>Download</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="section">
        <div className="form-card">
          <h3 style={{ marginTop: 0 }}>Reports</h3>
          <table>
            <thead>
              <tr><th>Kind</th><th>Created</th><th>Status</th><th></th></tr>
            </thead>
            <tbody>
              {reports.length === 0 && <tr><td colSpan={4} className="muted">No reports yet.</td></tr>}
              {reports.map((f) => (
                <tr key={f.id}>
                  <td>{f.kind}</td>
                  <td>{String(f.created_at).slice(0, 16).replace('T', ' ')}</td>
                  <td>{f.status}</td>
                  <td>
                    {f.status === 'ready' && (
                      <button className="secondary" style={{ fontSize: 11 }} onClick={() => download(f)}>Download</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {backupStatus && (
        <section className="section">
          <div className="form-card">
            <h3 style={{ marginTop: 0 }}>System backup</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              Last backup: {backupStatus.last_backup ? String(backupStatus.last_backup.at).slice(0, 16).replace('T', ' ') : 'never'} ·
              Last restore test: {backupStatus.last_restore_test
                ? `${backupStatus.last_restore_test.passed ? '✓ passed' : '✗ failed'} (${String(backupStatus.last_restore_test.at).slice(0, 16).replace('T', ' ')})`
                : 'never'}
              {' '}· offsite: {backupStatus.offsite}
            </p>
            <div className="form-actions">
              <button onClick={() => backupAction('run')} disabled={busy}>Back up now</button>
              <button className="secondary" onClick={() => backupAction('restore-test')} disabled={busy}>
                Run restore test
              </button>
            </div>
            {backups && backups.length > 0 && (
              <table style={{ marginTop: 10 }}>
                <thead>
                  <tr><th>File</th><th>Created</th><th>Status</th><th>Size</th></tr>
                </thead>
                <tbody>
                  {backups.map((f: any) => (
                    <tr key={f.id}>
                      <td>{f.filename || f.id.slice(0, 8)}</td>
                      <td>{String(f.created_at).slice(0, 16).replace('T', ' ')}</td>
                      <td>{f.status}</td>
                      <td>{f.size_bytes ? `${Math.round(f.size_bytes / 1024)} KB` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>
      )}
    </div>
  )
}
