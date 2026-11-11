import { useMemo, useState } from 'react'
import { Analytics } from '@vercel/analytics/react'
import { useAuth0 } from '@auth0/auth0-react'
import { NavLink, Route, Routes } from 'react-router-dom'
import CampaignsPage from './pages/Campaigns'
import CampaignDetailPage from './pages/CampaignDetail'
import './App.css'
import { API_BASE_URL, authEnabled } from './config'

const API_URL = `${API_BASE_URL}/upload`

type Severity = 'critical' | 'high' | 'medium' | 'low' | string

type Issue = {
  type: string
  severity: Severity
  explanation: string
  rule_version?: number
  control_refs?: string[]
}

type Finding = {
  user_id: string
  severity: Severity
  issues: Issue[]
}

type UploadSummary = {
  critical: number
  high: number
  medium: number
  low: number
  total: number
}

type ColumnMapping = {
  source: string
  target: string
  matched_column: string | null
  confidence: number
  method: 'exact' | 'alias' | 'fuzzy' | 'none' | string
  needs_confirmation: boolean
}

type InputDigest = {
  source: string
  filename: string | null
  sha256: string
  row_count: number
}

type UploadResponse = {
  summary: UploadSummary
  findings: Finding[]
  column_mappings: ColumnMapping[]
  inputs: InputDigest[]
  rule_set_version: string | null
  evaluated_at: string | null
}

const PRIVILEGE_TITLES: Record<string, string> = {
  privileged_access_critical: 'Critical-Tier Privileged Access',
  privileged_access_high: 'High-Tier Privileged Access',
  privileged_access_medium: 'Sensitive Access',
}


function prettifyIssue(issueType: string, explanation: string) {
  const roleMatch = /role='([^']+)'/i.exec(explanation)
  const statusMatch = /status='([^']+)'/i.exec(explanation)
  const termMatch = /termination_date='([^']+)'/i.exec(explanation)
  const role = roleMatch?.[1]
  const status = statusMatch?.[1]
  const terminationDate = termMatch?.[1]

  if (issueType in PRIVILEGE_TITLES) {
    return {
      title: PRIVILEGE_TITLES[issueType],
      summary: role
        ? `Role "${role}" matches the privileged access catalog. Confirm it is still required and approved.`
        : 'Role matches the privileged access catalog. Confirm it is still required and approved.',
    }
  }

  if (issueType === 'terminated_privileged_active') {
    return {
      title: 'Terminated User Holds Privileged Access',
      summary: `Terminated user is still active with privileged role${role ? ` "${role}"` : ''}. Revoke immediately.${terminationDate ? ` Termination date: ${terminationDate}.` : ''}`,
    }
  }

  if (issueType === 'terminated_active_within_grace') {
    return {
      title: 'Recently Terminated, Still Active',
      summary: `User was terminated${terminationDate ? ` on ${terminationDate}` : ''} and is still active, but is within the deprovisioning grace period. Confirm removal is in progress.`,
    }
  }

  if (issueType === 'admin_role') {
    return {
      title: 'Uncatalogued Administrative Role',
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

function AppMain() {
  const { isAuthenticated, getAccessTokenSilently } = useAuth0()
  const [userFile, setUserFile] = useState<File | null>(null)
  const [terminationFile, setTerminationFile] = useState<File | null>(null)
  const [results, setResults] = useState<Finding[]>([])
  const [summary, setSummary] = useState<UploadSummary>({
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    total: 0,
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastRunAt, setLastRunAt] = useState<string | null>(null)
  const [mappings, setMappings] = useState<ColumnMapping[]>([])
  const [inputs, setInputs] = useState<InputDigest[]>([])
  const [ruleSetVersion, setRuleSetVersion] = useState<string | null>(null)

  const hasFindings = useMemo(() => results.length > 0, [results])

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

    if (authEnabled && !isAuthenticated) {
      setError('Please sign in before running the review.')
      return
    }

    try {
      setLoading(true)
      const headers: Record<string, string> = {}
      if (authEnabled) {
        headers.Authorization = `Bearer ${await getAccessTokenSilently()}`
      }
      const res = await fetch(API_URL, {
        method: 'POST',
        headers,
        body: formData,
      })
      if (res.ok === false) {
        const text = await res.text()
        throw new Error(text || 'Request failed')
      }
      const data = (await res.json()) as UploadResponse
      setResults(data.findings)
      setSummary(data.summary)
      setMappings(data.column_mappings ?? [])
      setInputs(data.inputs ?? [])
      setRuleSetVersion(data.rule_set_version)
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
    const report = {
      rule_set_version: ruleSetVersion,
      evaluated_at: lastRunAt,
      inputs,
      column_mappings: mappings,
      summary,
      findings: results,
    }
    const blob = new Blob([JSON.stringify(report, null, 2)], {
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
          <p className="eyebrow">Quick scan</p>
          <h1>Automated Access Risk Detection</h1>
          <p className="subhead">
            Upload access and termination files to generate an audit-grade risk
            summary with prioritized findings.
          </p>
        </div>
        <div className="hero-card">
          <div className="stat">
            <span className="label">Critical</span>
            <span className="value danger">{summary.critical}</span>
          </div>
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
            active and users holding privileged access, tiered by the
            privileged access catalog.
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

          {mappings.length > 0 && (
            <div className="mappings">
              <h3>Column mapping</h3>
              <p className="panel-text">
                How your file headers were interpreted. Review any row marked
                for confirmation before relying on the results.
              </p>
              <div className="mapping-table">
                {mappings
                  .filter((m) => m.method !== 'none' || m.target === 'user_id')
                  .map((m) => (
                    <div
                      key={m.source + m.target}
                      className={'mapping-row' + (m.needs_confirmation ? ' weak' : '')}
                    >
                      <span>{m.source}</span>
                      <span>
                        {m.matched_column ?? '—'} → <strong>{m.target}</strong>
                      </span>
                      <span>
                        {m.method} · {Math.round(m.confidence * 100)}%
                        {m.needs_confirmation ? ' · confirm' : ''}
                      </span>
                    </div>
                  ))}
              </div>
              {inputs.length > 0 && (
                <ul className="digests">
                  {inputs.map((i) => (
                    <li key={i.source}>
                      {i.filename ?? i.source}: {i.row_count} rows · sha256{' '}
                      <code title={i.sha256}>{i.sha256.slice(0, 12)}…</code>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
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

      {ruleSetVersion && <p className="hint">Rule set {ruleSetVersion}</p>}
    </div>
  )
}

function AuthBar() {
  const { isAuthenticated, isLoading, loginWithRedirect, logout, user } = useAuth0()
  if (!authEnabled) return <span className="auth-label">Local mode (no sign-in)</span>
  if (isLoading) return <span className="auth-label">Checking session…</span>
  if (!isAuthenticated) {
    return (
      <button type="button" className="primary small" onClick={() => loginWithRedirect()}>
        Sign in
      </button>
    )
  }
  return (
    <>
      <span className="auth-label">{user?.email ?? user?.name}</span>
      <button
        type="button"
        className="ghost small"
        onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}
      >
        Sign out
      </button>
    </>
  )
}

/** Campaign pages hold customer data, so they need a signed-in user. */
function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, loginWithRedirect } = useAuth0()
  if (!authEnabled || isAuthenticated) return <>{children}</>
  if (isLoading) return <p className="panel-text">Checking session…</p>
  return (
    <div className="panel signin-panel">
      <h2>Sign in to see your campaigns</h2>
      <p className="panel-text">Campaigns store your access data, so they are only visible to your organization.</p>
      <button
        type="button"
        className="primary"
        onClick={() => loginWithRedirect({ appState: { returnTo: window.location.pathname } })}
      >
        Sign in
      </button>
    </div>
  )
}

export default function App() {
  return (
    <>
      <nav className="topnav">
        <NavLink to="/" end className="brand">UAR Copilot</NavLink>
        <div className="nav-links">
          <NavLink to="/" end>Quick scan</NavLink>
          <NavLink to="/campaigns">Campaigns</NavLink>
        </div>
        <div className="nav-auth">
          <AuthBar />
        </div>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<AppMain />} />
          <Route path="/campaigns" element={<RequireAuth><CampaignsPage /></RequireAuth>} />
          <Route path="/campaigns/:id" element={<RequireAuth><CampaignDetailPage /></RequireAuth>} />
          <Route path="*" element={<div className="empty"><p>Page not found.</p></div>} />
        </Routes>
      </main>
      <footer className="footer">
        <span>UAR Copilot • Internal Audit Preview</span>
        <span>API: {API_BASE_URL}</span>
      </footer>
      <Analytics />
    </>
  )
}
