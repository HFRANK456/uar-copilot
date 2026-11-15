import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApi, type Member, type WorkspaceRole } from '../api'
import { canManage, useWorkspace } from '../workspace'
import { ConnectionsPanel } from './Connections'

const ROLES: { value: WorkspaceRole; label: string; help: string }[] = [
  { value: 'reviewer', label: 'Reviewer', help: 'Sees campaigns and decides the items assigned to them' },
  { value: 'admin', label: 'Admin', help: 'Runs campaigns: uploads, analysis, sign-off, remediation' },
  { value: 'auditor', label: 'Auditor', help: 'Read-only; can download evidence packs' },
  { value: 'owner', label: 'Owner', help: 'Everything, including managing members' },
]

function errorText(err: unknown) {
  return err instanceof Error ? err.message : 'Something went wrong'
}

export default function WorkspaceSettingsPage() {
  const api = useApi()
  const navigate = useNavigate()
  const { current, refresh, select } = useWorkspace()
  const [members, setMembers] = useState<Member[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<WorkspaceRole>('reviewer')
  const [name, setName] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const isOwner = current?.role === 'owner'

  const load = useCallback(async () => {
    if (!current) return
    try {
      setMembers(await api<Member[]>(`/workspaces/${current.id}/members`))
    } catch (err) {
      setError(errorText(err))
    }
  }, [api, current])

  useEffect(() => {
    let cancelled = false
    Promise.resolve().then(() => {
      if (!cancelled) void load()
    })
    return () => {
      cancelled = true
    }
  }, [load])

  async function act(fn: () => Promise<unknown>, ok?: string) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await fn()
      if (ok) setNotice(ok)
      await load()
      await refresh()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  if (!current) return <p className="panel-text">Loading…</p>

  return (
    <div className="page">
      <header>
        <p className="eyebrow">Workspace</p>
        <h1 className="page-title">{current.name}</h1>
        <p className="subhead">
          Everyone in a workspace sees its campaigns. What they can do depends on their role. You are{' '}
          <strong>{current.role}</strong> here.
        </p>
      </header>
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      <section className="grid">
        <section className="panel">
          <div className="panel-header">
            <h2>Members</h2>
            {members && <span className="badge">{members.filter((m) => m.status === 'active').length} active</span>}
          </div>
          {members === null ? (
            <p className="panel-text">Loading…</p>
          ) : (
            <ul className="list">
              {members.map((m) => (
                <li key={m.id} className="member-row">
                  <div className="review-what">
                    <strong>{m.email ?? 'You (email not yet known)'}{m.is_you && ' · you'}</strong>
                    <span className="identity-sub">
                      {m.status === 'invited' ? `Invited by ${m.invited_by}; hasn't joined yet` : 'Active'}
                    </span>
                  </div>
                  {isOwner && !m.is_you ? (
                    <div className="member-actions">
                      <select value={m.role} aria-label={`Role of ${m.email}`} disabled={busy}
                        onChange={(e) => act(() => api(`/workspaces/${current.id}/members/${m.id}`, {
                          method: 'PATCH', body: JSON.stringify({ role: e.target.value }),
                        }), 'Role updated.')}>
                        {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                      </select>
                      <button type="button" className="ghost small" disabled={busy}
                        onClick={() => act(() => api(`/workspaces/${current.id}/members/${m.id}`, { method: 'DELETE' }),
                          `${m.email} removed.`)}>
                        Remove
                      </button>
                    </div>
                  ) : (
                    <span className="state-badge">{m.role}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <div className="stack">
          {isOwner && (
            <form className="panel" onSubmit={(e) => {
              e.preventDefault()
              const invited = email.trim()
              void act(async () => {
                await api(`/workspaces/${current.id}/members`, { method: 'POST', body: JSON.stringify({ email: invited, role }) })
                setEmail('')
              }, `${invited} is invited. Ask them to sign in at uar-copilot.vercel.app with that email address and click Join.`)
            }}>
              <div className="panel-header"><h2>Invite someone</h2></div>
              <label className="field">
                <span>Work email</span>
                <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
                  placeholder="dana@company.com" />
              </label>
              <label className="field">
                <span>Role</span>
                <select className="text-input" value={role} onChange={(e) => setRole(e.target.value as WorkspaceRole)}>
                  {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}: {r.help}</option>)}
                </select>
              </label>
              <p className="hint">
                Reviewers decide the items assigned to them, matched by the email in your HR roster, so invite them with
                that same email.
              </p>
              <button type="submit" className="primary" disabled={busy || !email.includes('@')}>Send invite</button>
            </form>
          )}

          {isOwner && (
            <form className="panel" onSubmit={(e) => {
              e.preventDefault()
              void act(() => api(`/workspaces/${current.id}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
                'Workspace renamed.').then(() => setName(''))
            }}>
              <div className="panel-header"><h2>Rename</h2></div>
              <label className="field">
                <span>Workspace name</span>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder={current.name} maxLength={255} />
              </label>
              <button type="submit" className="ghost small" disabled={busy || !name.trim()}>Rename</button>
            </form>
          )}

          <form className="panel" onSubmit={(e) => {
            e.preventDefault()
            void act(async () => {
              const w = await api<{ id: string }>('/workspaces', { method: 'POST', body: JSON.stringify({ name: newName }) })
              setNewName('')
              select(w.id)
              navigate('/campaigns')
            })
          }}>
            <div className="panel-header"><h2>New workspace</h2></div>
            <p className="panel-text">
              One workspace per company. If you run reviews for several clients, create one for each; their data never
              mixes.
            </p>
            <label className="field">
              <span>Name</span>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Client B" maxLength={255} />
            </label>
            <button type="submit" className="ghost small" disabled={busy || !newName.trim()}>Create workspace</button>
          </form>
        </div>
      </section>

      {canManage(current) && <ConnectionsPanel />}
    </div>
  )
}
