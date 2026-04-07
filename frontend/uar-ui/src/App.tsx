import { useMemo, useState } from 'react'
import './App.css'

const API_URL = 'https://uar-copilot.onrender.com/upload'

type AggregatedUser = {
  user_id: string
  issues: string[]
  severity: 'high' | 'medium' | 'low' | string
  explanations: string[]
}

function prettifyIssue(issue: string, explanation: string) {
  const roleMatch = /role='([^']+)'/i.exec(explanation)
  const statusMatch = /status='([^']+)'/i.exec(explanation)
  const termMatch = /termination_date='([^']+)'/i.exec(explanation)
  const role = roleMatch?.[1]
  const status = statusMatch?.[1]
  const terminationDate = termMatch?.[1]

  if (issue === 'admin_role') {
    return {
      title: 'Administrative Access Detected',
      summary: role
        ? `User holds an administrative role (${role}). This access should be explicitly approved and periodically reviewed.`
        : 'User holds an administrative role. This access should be explicitly approved and periodically reviewed.',
    }
  }

  if (issue === 'terminated_active') {
    const dateText = terminationDate ? `Termination date: ${terminationDate}.` : ''
    const statusText = status ? `Current status: ${status}.` : ''
    return {
      title: 'Terminated User Still Active',
      summary:
        `User appears in termination records but remains active in the access list. ${dateText} ${statusText}`.trim(),
    }
  }

  return {
    title: issue.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
    summary: explanation,
  }
}

function App() {
  const [userFile, setUserFile] = useState<File | null>(null)
  const [terminationFile, setTerminationFile] = useState<File | null>(null)
  const [results, setResults] = useState<AggregatedUser[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastRunAt, setLastRunAt] = useState<string | null>(null)

  const summary = useMemo(() => {
    const counts = { high: 0, medium: 0, low: 0, total: results.length }
    for (const r of results) {
      if (r.severity === 'high') counts.high += 1
      else if (r.severity === 'medium') counts.medium += 1
      else if (r.severity === 'low') counts.low += 1
    }
    return counts
  }, [results])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)

    if (userFile === null || terminationFile === null) {
      setError('Please attach both CSV files before running the review.')
      return
    }

    const formData = new FormData()
    formData.append('user_access', userFile)
    formData.append('termination', terminationFile)

    try {
      setLoading(true)
      const res = await fetch(API_URL, {
        method: 'POST',
        body: formData,
      })
      if (res.ok === false) {
        const text = await res.text()
        throw new Error(text || 'Request failed')
      }
      const data = (await res.json()) as AggregatedUser[]
      setResults(data)
      setLastRunAt(new Date().toLocaleString())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected error')
    } finally {
      setLoading(false)
    }
  }

  function downloadJson() {
    const blob = new Blob([JSON.stringify(results, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'uar_results.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="page">
      <header className="hero">
        <div>
          <p className="eyebrow">UAR Copilot</p>
          <h1>UAR Copilot – Automated Access Risk Detection</h1>
          <p className="subhead">
            Upload access and termination files to generate an audit-grade risk
            summary with prioritized findings.
          </p>
        </div>
        <div className="hero-card">
          <div className="stat">
            <span className="label">High</span>
            <span className="value danger">{summary.high}</span>
          </div>
          <div className="stat">
            <span className="label">Medium</span>
            <span className="value warn">{summary.medium}</span>
          </div>
          <div className="stat">
            <span className="label">Low</span>
            <span className="value ok">{summary.low}</span>
          </div>
          <div className="stat">
            <span className="label">Total</span>
            <span className="value">{summary.total}</span>
          </div>
          {lastRunAt && <p className="timestamp">Last run: {lastRunAt}</p>}
        </div>
      </header>

      <section className="grid">
        <form className="panel" onSubmit={handleSubmit}>
          <div className="panel-header">
            <h2>Upload Inputs</h2>
            <span className="badge">CSV</span>
          </div>
          <p className="panel-text">
            Provide both files. The system flags terminated users who remain
            active and users with administrative roles.
          </p>

          <label className="file-field">
            <span>User access file</span>
            <input
              type="file"
              accept=".csv"
              onChange={(e) => setUserFile(e.target.files?.[0] ?? null)}
            />
            <div className="file-meta">
              {userFile ? userFile.name : 'Expected: user_access.csv'}
            </div>
          </label>

          <label className="file-field">
            <span>Termination file</span>
            <input
              type="file"
              accept=".csv"
              onChange={(e) => setTerminationFile(e.target.files?.[0] ?? null)}
            />
            <div className="file-meta">
              {terminationFile
                ? terminationFile.name
                : 'Expected: termination.csv'}
            </div>
          </label>

          {error && <div className="error">{error}</div>}

          <div className="actions">
            <button type="submit" className="primary" disabled={loading}>
              {loading ? 'Running review…' : 'Run Review'}
            </button>
            <button
              type="button"
              className="ghost"
              onClick={downloadJson}
              disabled={results.length === 0}
            >
              Download JSON
            </button>
          </div>
          <p className="hint">
            Tip: Use the sample files in the backend data folder to validate the
            pipeline.
          </p>
        </form>

        <section className="panel results">
          <div className="panel-header">
            <h2>Findings</h2>
            <span className="badge">Aggregated</span>
          </div>

          {results.length === 0 ? (
            <div className="empty">
              <p>No findings yet.</p>
              <span>
                Upload files and run the review to generate an audit-grade
                summary.
              </span>
            </div>
          ) : (
            <div className="table">
              <div className="row header">
                <span>User</span>
                <span>Severity</span>
                <span>Issues</span>
              </div>
              {results.map((item) => (
                <div className="row" key={item.user_id}>
                  <span className="user">{item.user_id}</span>
                  <span className={'severity ' + item.severity}>
                    {item.severity}
                  </span>
                  <div className="issues">
                    {item.issues.map((issue, idx) => (
                      <div key={issue + idx} className="issue">
                        {(() => {
                          const expl = item.explanations[idx] ?? ''
                          const pretty = prettifyIssue(issue, expl)
                          return (
                            <>
                              <div className="issue-title">{pretty.title}</div>
                              <div className="issue-desc">{pretty.summary}</div>
                            </>
                          )
                        })()}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </section>

      <footer className="footer">
        <span>UAR Copilot • Internal Audit Preview</span>
        <span>Backend: Connected</span>
      </footer>
    </div>
  )
}

export default App
