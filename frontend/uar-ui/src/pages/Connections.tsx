import { useState } from 'react'
import { useApi, useConnections, type Connection, type ConnectionKind } from '../api'

const GOOGLE_SCOPES =
  'https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/admin.directory.rolemanagement.readonly'

function errorText(err: unknown) {
  return err instanceof Error ? err.message : 'Something went wrong'
}

function describe(c: Connection) {
  if (c.kind === 'google_workspace') return `${c.config.domain} · acting as ${c.config.admin_email}`
  const tickets = c.config.project_key ? `tickets go to project ${c.config.project_key}` : 'no ticket project set'
  return `${c.config.site?.replace('https://', '')} · ${c.config.email} · ${tickets}`
}

function StatusTag({ c }: { c: Connection }) {
  if (c.status === 'ok') return <span className="status-pill status-matched">Working</span>
  if (c.status === 'error') return <span className="status-pill status-unmatched">Not working</span>
  return <span className="status-pill status-not_resolved">Not tested</span>
}

/** Workspace settings: add, test and remove connections to Google Workspace and Jira Cloud. */
export function ConnectionsPanel() {
  const api = useApi()
  const { connections, error: loadError, reload } = useConnections(true)
  const [kind, setKind] = useState<ConnectionKind | ''>('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [formKey, setFormKey] = useState(0)

  // Google Workspace
  const [adminEmail, setAdminEmail] = useState('')
  const [keyFile, setKeyFile] = useState<{ name: string; json: Record<string, unknown> } | null>(null)
  // Jira Cloud
  const [site, setSite] = useState('')
  const [jiraEmail, setJiraEmail] = useState('')
  const [token, setToken] = useState('')
  const [projectKey, setProjectKey] = useState('')

  async function run(label: string, fn: () => Promise<string | void>) {
    setBusy(label)
    setError(null)
    setNotice(null)
    try {
      const ok = await fn()
      if (ok) setNotice(ok)
    } catch (err) {
      setError(errorText(err))
    } finally {
      await reload()
      setBusy(null)
    }
  }

  async function readKey(file: File | undefined) {
    setKeyFile(null)
    if (!file) return
    try {
      const json = JSON.parse(await file.text()) as Record<string, unknown>
      if (json.type !== 'service_account' || !json.private_key) {
        setError("That file isn't a service account key. In Google Cloud, open the service account → Keys → Add key → JSON.")
        return
      }
      setError(null)
      setKeyFile({ name: file.name, json })
    } catch {
      setError("That file isn't valid JSON. Download the key again as JSON.")
    }
  }

  function resetForm() {
    setKind('')
    setAdminEmail('')
    setKeyFile(null)
    setSite('')
    setJiraEmail('')
    setToken('')
    setProjectKey('')
    setFormKey((k) => k + 1)
  }

  async function add() {
    const body =
      kind === 'google_workspace'
        ? { kind, config: { admin_email: adminEmail }, secret: { service_account: keyFile?.json } }
        : { kind, config: { site, email: jiraEmail, project_key: projectKey }, secret: { api_token: token } }
    await run('add', async () => {
      const created = await api<Connection>('/connections', { method: 'POST', body: JSON.stringify(body) })
      resetForm()
      try {
        const tested = await api<Connection>(`/connections/${created.id}/test`, { method: 'POST' })
        return `${created.label} connected. ${tested.message ?? ''}`
      } catch (err) {
        throw new Error(`${created.label} was saved, but the test failed: ${errorText(err)}`)
      }
    })
  }

  const ready =
    kind === 'google_workspace'
      ? adminEmail.includes('@') && keyFile !== null
      : kind === 'jira_cloud'
        ? site.trim() !== '' && jiraEmail.includes('@') && token.trim() !== ''
        : false

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Connections</h2>
        {connections && <span className="badge">{connections.length}</span>}
      </div>
      <p className="panel-text">
        Pull users and their access straight from your systems instead of exporting files, and send remediation
        tickets to Jira. Access is read-only, except creating tickets in the Jira project you choose. Keys are
        stored encrypted and never shown again.
      </p>
      {(error || loadError) && <div className="error">{error || loadError}</div>}
      {notice && <div className="notice">{notice}</div>}

      {connections && connections.length > 0 && (
        <ul className="list">
          {connections.map((c) => (
            <li key={c.id} className="member-row">
              <div className="review-what">
                <strong>{c.label}</strong>
                <span className="identity-sub">{describe(c)}</span>
                {c.status === 'error' && c.last_error && <span className="identity-sub danger-text">{c.last_error}</span>}
                {c.last_checked_at && (
                  <span className="identity-sub">Last checked {new Date(c.last_checked_at).toLocaleString()}</span>
                )}
              </div>
              <div className="member-actions">
                <StatusTag c={c} />
                <button type="button" className="ghost small" disabled={busy !== null}
                  onClick={() => run(`test-${c.id}`, async () => {
                    const r = await api<Connection>(`/connections/${c.id}/test`, { method: 'POST' })
                    return r.message
                  })}>
                  {busy === `test-${c.id}` ? 'Testing…' : 'Test'}
                </button>
                <button type="button" className="ghost small" disabled={busy !== null}
                  onClick={() => {
                    if (!window.confirm(`Remove the ${c.label} connection? Data already pulled into campaigns stays.`)) return
                    void run(`del-${c.id}`, async () => {
                      await api(`/connections/${c.id}`, { method: 'DELETE' })
                      return `${c.label} connection removed.`
                    })
                  }}>
                  Remove
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="connection-form" key={formKey}>
        <label className="field">
          <span>Add a connection</span>
          <select className="text-input" value={kind} onChange={(e) => setKind(e.target.value as ConnectionKind | '')}>
            <option value="">Choose a system…</option>
            <option value="google_workspace">Google Workspace (users, suspended accounts, admin roles)</option>
            <option value="jira_cloud">Jira Cloud (users and groups; remediation tickets)</option>
          </select>
        </label>

        {kind === 'google_workspace' && (
          <>
            <details className="setup-steps">
              <summary>How to set this up (about 10 minutes, needs a Google super admin)</summary>
              <ol>
                <li>In <strong>console.cloud.google.com</strong>, pick or create a project, then enable the
                  <strong> Admin SDK API</strong> (APIs &amp; Services → Library).</li>
                <li>IAM &amp; Admin → Service accounts → <strong>Create service account</strong> (no roles needed).
                  Open it → Keys → Add key → <strong>JSON</strong>. A file downloads.</li>
                <li>Copy the service account's <strong>Client ID</strong> (a long number, on its Details tab).</li>
                <li>In <strong>admin.google.com</strong>: Security → Access and data control → API controls →
                  <strong> Manage domain-wide delegation</strong> → Add new. Paste the Client ID and these scopes:
                  <code className="scopes">{GOOGLE_SCOPES}</code></li>
                <li>Below, enter the email of a Google admin to act as, and choose the JSON file.</li>
              </ol>
              <p className="hint">Both scopes are read-only: the connector can't change anything in Google.</p>
            </details>
            <label className="field">
              <span>Google admin email</span>
              <input type="email" value={adminEmail} onChange={(e) => setAdminEmail(e.target.value)}
                placeholder="it-admin@company.com" autoComplete="off" />
            </label>
            <label className="field">
              <span>Service account key (JSON file)</span>
              <input type="file" accept=".json,application/json" onChange={(e) => void readKey(e.target.files?.[0])} />
            </label>
            {keyFile && <p className="hint">Key for {String(keyFile.json.client_email)} ready.</p>}
          </>
        )}

        {kind === 'jira_cloud' && (
          <>
            <details className="setup-steps">
              <summary>How to set this up (2 minutes, needs a Jira admin account)</summary>
              <ol>
                <li>Signed in as a Jira admin, go to <strong>id.atlassian.com/manage-profile/security/api-tokens</strong>
                  {' '}→ <strong>Create API token</strong>. Name it "UAR Copilot", copy the token.</li>
                <li>Enter your Jira site, that admin's email and the token below.</li>
                <li>Optional: a project key (like IT) to send remediation tickets to.</li>
              </ol>
              <p className="hint">
                The token acts as that person. UAR Copilot only reads users and groups, and creates tickets when you
                click Send to Jira.
              </p>
            </details>
            <label className="field">
              <span>Jira site</span>
              <input value={site} onChange={(e) => setSite(e.target.value)} placeholder="yourcompany.atlassian.net" />
            </label>
            <label className="field">
              <span>Atlassian account email</span>
              <input type="email" value={jiraEmail} onChange={(e) => setJiraEmail(e.target.value)}
                placeholder="it-admin@company.com" autoComplete="off" />
            </label>
            <label className="field">
              <span>API token</span>
              <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="new-password" />
            </label>
            <label className="field">
              <span>Project key for tickets (optional)</span>
              <input value={projectKey} onChange={(e) => setProjectKey(e.target.value.toUpperCase())} placeholder="IT"
                maxLength={20} />
            </label>
          </>
        )}

        {kind && (
          <button type="button" className="primary" disabled={!ready || busy !== null} onClick={add}>
            {busy === 'add' ? 'Connecting…' : 'Connect and test'}
          </button>
        )}
      </div>
    </section>
  )
}
