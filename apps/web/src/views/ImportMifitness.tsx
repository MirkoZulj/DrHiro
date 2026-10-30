import { useState } from 'react'
import { authClient, ApiError } from '../lib/auth'

/** Shape of the JSON report returned by POST /import/mifitness. */
interface MifitnessReport {
  ok?: boolean
  dry_run?: boolean
  source_provider?: string
  import_filename?: string
  batch_id?: string
  elapsed_s?: number
  day_range?: { from: string; to: string }
  raw?: {
    rows_read?: number
    imported?: number
    skipped_telemetry?: number
    skipped_per_sample?: number
    inserted?: number
    duplicates?: number
    errors?: number
  }
  daily?: {
    rows_read?: number
    mapped?: number
    skipped?: number
    distinct_keys?: number
    revisions_collapsed?: number
    inserted?: number
    skipped_gapfill?: number
    errors?: number
  }
}

/** Turn an API/network failure into a plain sentence a user can act on. */
function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = e.message
    if (e.status === 400) {
      // The API returns 400 for a wrong password, a non-ZIP file, or a ZIP that
      // has none of the expected CSVs — all of which it explains in `detail`.
      return `Import rejected: ${detail}`
    }
    if (e.status === 401) {
      return `Not signed in — please sign in again and retry. (${detail})`
    }
    if (e.status === 413) {
      return `The ZIP is too large for the server. ${detail}`
    }
    if (e.status === 504) {
      return 'The server took too long and the request timed out. Try the dry run first, then retry the full import.'
    }
    return `Import failed (HTTP ${e.status}): ${detail}`
  }
  const msg = e instanceof Error ? e.message : String(e)
  return `Import failed: ${msg}`
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <li className="list-item">
      <span className="main">
        <span className="title">{label}</span>
      </span>
      <span className="title">{value}</span>
    </li>
  )
}

function ReportPanel({ report }: { report: MifitnessReport }) {
  const raw = report.raw ?? {}
  const daily = report.daily ?? {}
  const range = report.day_range
  return (
    <div className="status" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 700, marginBottom: 4 }}>
        {report.dry_run ? 'Check complete (nothing was written)' : 'Import complete'}
      </div>
      <p className="muted" style={{ marginTop: 0 }}>
        Existing days are never overwritten — the import is additive and gap-filling only.
      </p>

      <h3 style={{ marginTop: 12 }}>Raw samples</h3>
      <ul>
        <Stat label="Rows read" value={raw.rows_read ?? 0} />
        <Stat label="Imported (mapped samples)" value={raw.imported ?? 0} />
        <Stat label="New rows inserted" value={raw.inserted ?? 0} />
        <Stat label="Duplicates skipped (already have them)" value={raw.duplicates ?? 0} />
        <Stat label="Telemetry skipped" value={raw.skipped_telemetry ?? 0} />
        <Stat label="Per-sample skipped" value={raw.skipped_per_sample ?? 0} />
        <Stat label="Errors" value={raw.errors ?? 0} />
      </ul>

      <h3 style={{ marginTop: 12 }}>Daily aggregates</h3>
      <ul>
        <Stat label="Rows read" value={daily.rows_read ?? 0} />
        <Stat label="New daily rows inserted" value={daily.inserted ?? 0} />
        <Stat label="Gap-fill skipped (day already covered)" value={daily.skipped_gapfill ?? 0} />
        <Stat label="Errors" value={daily.errors ?? 0} />
      </ul>

      <h3 style={{ marginTop: 12 }}>Coverage</h3>
      <ul>
        <Stat
          label="Day range covered"
          value={range ? `${range.from} → ${range.to}` : '—'}
        />
        <Stat label="Distinct day/metric keys" value={daily.distinct_keys ?? 0} />
        <Stat
          label="Elapsed"
          value={report.elapsed_s != null ? `${report.elapsed_s} s` : '—'}
        />
      </ul>
      {report.batch_id && (
        <p className="muted" style={{ marginTop: 8 }}>
          Batch <span className="mono">{report.batch_id}</span>
        </p>
      )}
    </div>
  )
}

export default function ImportMifitness() {
  const [file, setFile] = useState<File | null>(null)
  const [password, setPassword] = useState('')
  const [dryRun, setDryRun] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<MifitnessReport | null>(null)

  async function submit() {
    setError(null)
    setReport(null)
    if (!file) {
      setError('Choose the Mi Fitness export ZIP first.')
      return
    }
    if (!password) {
      setError('Enter the archive password.')
      return
    }
    setBusy(true)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('password', password)
      form.append('dry_run', dryRun ? 'true' : 'false')
      const res = await authClient.api('/import/mifitness', { method: 'POST', body: form })
      setReport(res as MifitnessReport)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="settings">
      <h2>Import Mi Fitness export</h2>
      <p className="page-sub">
        Upload the ZIP exported from the Mi Fitness app to fill gaps in your history. This is additive:
        it never overwrites days you already have.
      </p>

      {error && <div className="error">{error}</div>}

      <section className="section">
        <div className="form-card">
          <div className="field">
            <label htmlFor="mf-file">Export ZIP</label>
            <input
              id="mf-file"
              type="file"
              accept=".zip,application/zip"
              disabled={busy}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>

          <div className="field">
            <label htmlFor="mf-password">Archive password</label>
            <input
              id="mf-password"
              type="password"
              autoComplete="off"
              placeholder="Password from the export email"
              disabled={busy}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          <div className="field">
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
              <input
                type="checkbox"
                disabled={busy}
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
                style={{ width: 'auto' }}
              />
              <span>Check first (dry run) — reconcile and report without writing anything</span>
            </label>
          </div>

          <div className="form-actions" style={{ marginTop: 12 }}>
            <button onClick={submit} disabled={busy}>
              {busy
                ? 'Working… this can take a few minutes for a large export. Please keep this tab open.'
                : dryRun
                  ? 'Check the export'
                  : 'Import the export'}
            </button>
          </div>
          {busy && (
            <p className="muted" style={{ marginTop: 8 }}>
              Reading and reconciling the archive — a full export is ~394 MB of CSV, so this can take
              several minutes. The page is not frozen; the request is still running.
            </p>
          )}
        </div>
      </section>

      {report && <ReportPanel report={report} />}
    </div>
  )
}