import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useApi, type Campaign } from '../api'
import { StateBadge } from './shared'

export default function CampaignsPage() {
  const api = useApi()
  const navigate = useNavigate()
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ name: '', period_start: '', period_end: '', due_date: '' })

  const load = useCallback(async () => {
    try {
      setCampaigns(await api<Campaign[]>('/campaigns'))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load campaigns')
    }
  }, [api])

  useEffect(() => {
    void load()
  }, [load])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError(null)
    try {
      const body = Object.fromEntries(Object.entries(form).filter(([, v]) => v !== ''))
      const c = await api<Campaign>('/campaigns', { method: 'POST', body: JSON.stringify(body) })
      navigate(`/campaigns/${c.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create campaign')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="page">
      <header>
        <p className="eyebrow">Access reviews</p>
        <h1 className="page-title">Campaigns</h1>
        <p className="subhead">
          A campaign is one review period: upload your HR roster and each application's access list,
          link accounts to people, then review.
        </p>
      </header>

      <section className="grid">
        <form className="panel" onSubmit={create}>
          <div className="panel-header">
            <h2>New campaign</h2>
          </div>
          <label className="field">
            <span>Name</span>
            <input
              required
              maxLength={255}
              placeholder="Q4 2026 access review"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <div className="field-row">
            <label className="field">
              <span>Period start</span>
              <input type="date" value={form.period_start} onChange={(e) => setForm({ ...form, period_start: e.target.value })} />
            </label>
            <label className="field">
              <span>Period end</span>
              <input type="date" value={form.period_end} onChange={(e) => setForm({ ...form, period_end: e.target.value })} />
            </label>
          </div>
          <label className="field">
            <span>Due date</span>
            <input type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} />
          </label>
          {error && <div className="error">{error}</div>}
          <div className="actions">
            <button type="submit" className="primary" disabled={creating || form.name.trim() === ''}>
              {creating ? 'Creating…' : 'Create campaign'}
            </button>
          </div>
        </form>

        <section className="panel">
          <div className="panel-header">
            <h2>Your campaigns</h2>
            {campaigns && <span className="badge">{campaigns.length}</span>}
          </div>
          {campaigns === null && !error && <p className="panel-text">Loading…</p>}
          {campaigns?.length === 0 && (
            <div className="empty">
              <p>No campaigns yet.</p>
              <span>Create one to start a review.</span>
            </div>
          )}
          {campaigns && campaigns.length > 0 && (
            <ul className="list">
              {campaigns.map((c) => (
                <li key={c.id}>
                  <Link to={`/campaigns/${c.id}`} className="list-item">
                    <span className="list-title">{c.name}</span>
                    <StateBadge state={c.state} />
                    <span className="list-meta">
                      {c.period_start && c.period_end ? `${c.period_start} → ${c.period_end}` : 'No period set'}
                      {c.due_date ? ` · due ${c.due_date}` : ''}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </section>
    </div>
  )
}
