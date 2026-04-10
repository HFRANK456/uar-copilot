import { useMemo, useState } from 'react'
import { Analytics } from '@vercel/analytics/react'
import { useAuth0 } from '@auth0/auth0-react'
import './App.css'

const API_URL = 'https://uar-copilot.onrender.com/upload'

type Issue = {
  type: string
  severity: 'high' | 'medium' | 'low' | string
  explanation: string
}

type Finding = {
  user_id: string
  severity: 'high' | 'medium' | 'low' | string
  issues: Issue[]
}

type UploadSummary = {
  high: number
  medium: number
  low: number
  total: number
}

type UploadResponse = {
  summary: UploadSummary
  findings: Finding[]
}

const authEnabled = Boolean(
  (import.meta.env.VITE_AUTH0_CLIENT_ID as string | undefined)?.trim()
)

const authAudience =
  (import.meta.env.VITE_AUTH0_AUDIENCE as string | undefined)?.trim() ||
  'https://uar-copilot-api'

function prettifyIssue(issueType: string, explanation: string) {
  const roleMatch = /role='([^']+)'/i.exec(explanation)
  const statusMatch = /status='([^']+)'/i.exec(explanation)
  const termMatch = /termination_date='([^']+)'/i.exec(explanation)
  const role = roleMatch?.[1]
  const status = statusMatch?.[1]
  const terminationDate = termMatch?.[1]

  if (issueType === 'admin_role') {
    return {
      title: 'Administrative Access Detected',
      summary: role
        ? `User holds an administrative role (${role}). This access should be explicitly approved and periodically reviewed.`
        : 'User holds an administrative role. This access should be explicitly approved and periodically reviewed.',
    }
  }

  if (issueType === 'terminated_active') {
    const dateText = terminationDate ? `Termination date: ${terminationDate}.` : ''
    const statusText = status ? `Current status: ${status}.` : ''
    return {
      title: 'Terminated User Still Active',
      summary:
        `User appears in termination records but remains active in the access list. ${dateText} ${statusText}`.trim(),
    }
  }

  return {
    title: issueType.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
    summary: explanation,
  }
}

function AppAuthed() {
  const auth0 = useAuth0()
  const [userFile, setUserFile] = useState<File | null>(null)
  const [terminationFile, setTerminationFile] = useState<File | null>(null)
  const [results, setResults] = useState<Finding[]>([])
  const [summary, setSummary] = useState<UploadSummary>({
    high: 0,
    medium: 0,
    low: 0,
    total: 0,
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastRunAt, setLastRunAt] = useState<string | null>(null)

  const hasFindings = useMemo(() => results.length > 0, [results])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)

    if (auth0.isAuthenticated === false) {
      await auth0.loginWithRedirect()
      return
    }

    if (userFile === null || terminationFile === null) {
      setError('Please attach both CSV files before running the review.')
      return
    }

    const formData = new FormData()
    formData.append('user_access', userFile)
    formData.append('termination', terminationFile)

    try {
      setLoading(true)
      const token = await auth0.getAccessTokenSilently({
        authorizationParams: {
          audience: authAudience,
        },
      })
      const res = await fetch(API_URL, {
        method: 'POST',
        body: formData,
        headers: { Authorization: `Bearer ${token}` },
      })
      if (res.ok === false) {
        const text = await res.text()
        throw new Error(text || 'Request failed')
      }
      const data = (await res.json()) as UploadResponse
      setResults(data.findings)
      setSummary(data.summary)
      setLastRunAt(new Date().toLocaleString())
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Unexpected error'
      if (msg.toLowerCase().includes('missing bearer token')) {
        setError('Please sign in before running the review.')
      } else {
        setError(msg)
      }
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
          <div className="auth">
            {auth0.isAuthenticated ? (
              <>
                <span className="auth-label">
                  Signed in{auth0.user?.email ? ` as ${auth0.user.email}` : ''}
                </span>
                <button
                  type="button"
                  className="ghost"
                  onClick={() =>
                    auth0.logout({
                      logoutParams: { returnTo: window.location.origin },
                    })
                  }
                >
                  Sign out
                </button>
              </>
            ) : (
              <button
                type="button"
                className="primary"
                onClick={() => auth0.loginWithRedirect()}
              >
                Sign in
              </button>
            )}
          </div>
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
            <button
              type="submit"
              className="primary"
              disabled={loading}
            >
              {loading ? 'Running review…' : 'Run Review'}
            </button>
            <button
              type="button"
              className="ghost"
              onClick={downloadJson}
              disabled={hasFindings === false}
            >
              Download JSON
            </button>
          </div>
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
                      <div key={issue.type + idx} className="issue">
                        {(() => {
                          const pretty = prettifyIssue(
                            issue.type,
                            issue.explanation ?? ''
                          )
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
      <Analytics />
    </div>
  )
}

export default function App() {
  if (authEnabled === false) {
    return (
      <div className="page">
        <header className="hero">
          <div>
            <p className="eyebrow">UAR Copilot</p>
            <h1>UAR Copilot – Automated Access Risk Detection</h1>
            <p className="subhead">
              Authentication is not configured. Set
              {' `VITE_AUTH0_CLIENT_ID` '}in Vercel and redeploy.
            </p>
          </div>
        </header>
        <footer className="footer">
          <span>UAR Copilot • Internal Audit Preview</span>
          <span>Backend: Connected</span>
        </footer>
        <Analytics />
      </div>
    )
  }

  return <AppAuthed />
}
