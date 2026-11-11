import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ApiError,
  useApi,
  type Campaign,
  type Evidence,
  type IdentityItem,
  type IdentityList,
  type IngestResult,
  type PersonRef,
  type Snapshot,
} from '../api'
import { Confidence, StateBadge, StatusPill } from './shared'

const INGEST_STATES = new Set(['draft', 'ingesting', 'resolving_identities', 'analyzing'])
const RESOLVE_STATES = new Set(['ingesting', 'resolving_identities'])

const FILTERS: { key: string; label: string; match: (s: string) => boolean }[] = [
  { key: 'open', label: 'Needs attention', match: (s) => s === 'ambiguous' || s === 'unmatched' },
  { key: 'matched', label: 'Matched', match: (s) => s === 'matched' },
  { key: 'other', label: 'Service / external', match: (s) => s === 'non_human' || s === 'external' },
  { key: 'all', label: 'All', match: () => true },
]

function errorText(err: unknown) {
  return err instanceof Error ? err.message : 'Something went wrong'
}

export default function CampaignDetailPage() {
  const { id = '' } = useParams()
  const api = useApi()
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [identities, setIdentities] = useState<IdentityList | null>(null)
  const [people, setPeople] = useState<PersonRef[]>([])
  const [evidence, setEvidence] = useState<Evidence | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [c, ids, ev, ppl] = await Promise.all([
        api<Campaign>(`/campaigns/${id}`),
        api<IdentityList>(`/campaigns/${id}/identities`),
        api<Evidence>(`/campaigns/${id}/evidence`),
        api<PersonRef[]>(`/campaigns/${id}/people`),
      ])
      setCampaign(c)
      setIdentities(ids)
      setEvidence(ev)
      setPeople(ppl)
      setError(null)
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setNotFound(true)
      else setError(errorText(err))
    }
  }, [api, id])

  useEffect(() => {
    let cancelled = false
    Promise.resolve().then(() => {
      if (!cancelled) void refresh()
    })
    return () => {
      cancelled = true
    }
  }, [refresh])

  if (notFound) {
    return (
      <div className="page">
        <Link to="/campaigns" className="back">← Campaigns</Link>
        <div className="empty"><p>Campaign not found.</p></div>
      </div>
    )
  }
  if (!campaign) {
    return (
      <div className="page">
        <Link to="/campaigns" className="back">← Campaigns</Link>
        {error ? <div className="error">{error}</div> : <p className="panel-text">Loading…</p>}
      </div>
    )
  }

  const hasHr = campaign.snapshots.some((s) => s.kind === 'hr_roster')
  const hasAccess = campaign.snapshots.some((s) => s.kind === 'access_list')

  return (
    <div className="page">
      <header>
        <Link to="/campaigns" className="back">← Campaigns</Link>
        <div className="title-row">
          <h1 className="page-title">{campaign.name}</h1>
          <StateBadge state={campaign.state} />
        </div>
        <p className="subhead">
          {campaign.period_start && campaign.period_end
            ? `Review period ${campaign.period_start} → ${campaign.period_end}`
            : 'No review period set'}
          {campaign.due_date ? ` · due ${campaign.due_date}` : ''}
        </p>
      </header>

      {error && <div className="error">{error}</div>}

      <Steps hasHr={hasHr} hasAccess={hasAccess} identities={identities} />

      <SourcesSection campaign={campaign} onChange={refresh} />

      <IdentitySection
        campaign={campaign}
        identities={identities}
        people={people}
        canResolve={hasHr && hasAccess && RESOLVE_STATES.has(campaign.state)}
        onChange={refresh}
      />

      <EvidenceSection evidence={evidence} />
    </div>
  )
}

// ---------------------------------------------------------------------------

function Steps({ hasHr, hasAccess, identities }: { hasHr: boolean; hasAccess: boolean; identities: IdentityList | null }) {
  const resolved = (identities?.summary.accounts ?? 0) > 0 && (identities?.summary.not_resolved ?? 0) === 0
  const ready = Boolean(identities?.summary.ready_for_analysis)
  const steps = [
    { label: 'Upload HR roster', done: hasHr },
    { label: 'Upload access lists', done: hasAccess },
    { label: 'Match accounts to people', done: resolved },
    { label: 'Resolve the queue', done: ready },
    { label: 'Review access', done: false, soon: true },
  ]
  return (
    <ol className="steps">
      {steps.map((s, i) => (
        <li key={s.label} className={s.done ? 'done' : s.soon ? 'soon' : ''}>
          <span className="step-num">{s.done ? '✓' : i + 1}</span>
          <span>{s.label}</span>
          {s.soon && <span className="soon-tag">coming next</span>}
        </li>
      ))}
    </ol>
  )
}

// ---------------------------------------------------------------------------

function SourcesSection({ campaign, onChange }: { campaign: Campaign; onChange: () => Promise<void> }) {
  const api = useApi()
  const canIngest = INGEST_STATES.has(campaign.state)
  const [hrFile, setHrFile] = useState<File | null>(null)
  const [accessFile, setAccessFile] = useState<File | null>(null)
  const [appName, setAppName] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [resetKey, setResetKey] = useState(0)
  const [message, setMessage] = useState<{ kind: 'error' | 'ok'; text: string; warnings?: string[] } | null>(null)

  async function upload(kind: 'hr' | 'access') {
    const file = kind === 'hr' ? hrFile : accessFile
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    if (kind === 'access') form.append('application', appName.trim())
    setBusy(kind)
    setMessage(null)
    try {
      const r = await api<IngestResult>(`/campaigns/${campaign.id}/sources/${kind}`, { method: 'POST', body: form })
      const c = r.counts
      const text =
        kind === 'hr'
          ? `Loaded ${c.rows} rows: ${c.people_created} new people, ${c.people_updated} updated.`
          : `Loaded ${c.accounts} accounts with ${c.entitlement_grants} entitlement grants for ${r.snapshot.application}.`
      setMessage({ kind: 'ok', text, warnings: r.warnings })
      setResetKey((k) => k + 1)
      if (kind === 'hr') setHrFile(null)
      else {
        setAccessFile(null)
        setAppName('')
      }
      await onChange()
    } catch (err) {
      setMessage({ kind: 'error', text: errorText(err) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Sources</h2>
        <span className="badge">CSV / XLSX</span>
      </div>
      {!canIngest && <p className="panel-text">This campaign is past the upload stage.</p>}
      {canIngest && (
        <div className="upload-grid">
          <div className="upload-box">
            <h3>HR roster</h3>
            <p className="panel-text">Employee ID, email, name, manager, department, status, hire and termination dates.</p>
            <input key={`hr-${resetKey}`} type="file" accept=".csv,.xlsx,.xls" onChange={(e) => setHrFile(e.target.files?.[0] ?? null)} />
            <button type="button" className="primary" disabled={!hrFile || busy !== null} onClick={() => upload('hr')}>
              {busy === 'hr' ? 'Uploading…' : 'Upload roster'}
            </button>
          </div>
          <div className="upload-box">
            <h3>Application access list</h3>
            <p className="panel-text">One file per application: account, email, status, role, last login.</p>
            <input
              className="text-input"
              placeholder="Application name, e.g. AWS"
              value={appName}
              maxLength={255}
              onChange={(e) => setAppName(e.target.value)}
            />
            <input key={`access-${resetKey}`} type="file" accept=".csv,.xlsx,.xls" onChange={(e) => setAccessFile(e.target.files?.[0] ?? null)} />
            <button
              type="button"
              className="primary"
              disabled={!accessFile || appName.trim() === '' || busy !== null}
              onClick={() => upload('access')}
            >
              {busy === 'access' ? 'Uploading…' : 'Upload access list'}
            </button>
          </div>
        </div>
      )}
      {message && (
        <div className={message.kind === 'error' ? 'error' : 'notice'}>
          {message.text}
          {message.warnings && message.warnings.length > 0 && (
            <ul className="warnings">
              {message.warnings.slice(0, 5).map((w) => <li key={w}>{w}</li>)}
            </ul>
          )}
        </div>
      )}

      {campaign.snapshots.length > 0 && (
        <div className="snapshots">
          {campaign.snapshots.map((s) => (
            <SnapshotRow key={s.id} campaignId={campaign.id} snapshot={s} onChange={onChange} />
          ))}
        </div>
      )}
    </section>
  )
}

function SnapshotRow({ campaignId, snapshot: s, onChange }: { campaignId: string; snapshot: Snapshot; onChange: () => Promise<void> }) {
  const api = useApi()
  const [open, setOpen] = useState(s.needs_confirmation)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function confirm() {
    setBusy(true)
    try {
      await api(`/campaigns/${campaignId}/sources/${s.id}/confirm-mapping`, { method: 'POST' })
      await onChange()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={'snapshot' + (s.needs_confirmation ? ' attention' : '')}>
      <div className="snapshot-head">
        <span className="snapshot-kind">{s.kind === 'hr_roster' ? 'HR roster' : s.application}</span>
        <span className="snapshot-meta">
          {s.filename} · {s.row_count} rows · sha256 <code title={s.sha256}>{s.sha256.slice(0, 12)}…</code>
        </span>
        <button type="button" className="ghost small" onClick={() => setOpen(!open)}>
          {open ? 'Hide mapping' : 'Column mapping'}
        </button>
      </div>
      {s.needs_confirmation && (
        <div className="attention-row">
          <span>Some columns were matched with low confidence. Check them before relying on this file.</span>
          <button type="button" className="primary small" disabled={busy} onClick={confirm}>
            {busy ? 'Confirming…' : 'Mapping looks right'}
          </button>
        </div>
      )}
      {s.mapping_confirmed_by && <p className="hint">Mapping confirmed by {s.mapping_confirmed_by}</p>}
      {error && <div className="error">{error}</div>}
      {open && (
        <div className="mapping-table">
          {s.column_mapping
            .filter((m) => m.method !== 'none')
            .map((m) => (
              <div key={m.target} className={'mapping-row' + (m.needs_confirmation ? ' weak' : '')}>
                <span>{m.matched_column}</span>
                <span>→ <strong>{m.target}</strong></span>
                <span>{m.method} · {Math.round(m.confidence * 100)}%{m.needs_confirmation ? ' · check' : ''}</span>
              </div>
            ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------

function IdentitySection({
  campaign,
  identities,
  people,
  canResolve,
  onChange,
}: {
  campaign: Campaign
  identities: IdentityList | null
  people: PersonRef[]
  canResolve: boolean
  onChange: () => Promise<void>
}) {
  const api = useApi()
  const [filter, setFilter] = useState('open')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const summary = identities?.summary
  const resolvedOnce = (summary?.accounts ?? 0) > 0 && (summary?.not_resolved ?? 0) === 0
  const editable = campaign.state === 'resolving_identities'
  const active = FILTERS.find((f) => f.key === filter) ?? FILTERS[0]
  const items = useMemo(() => (identities?.items ?? []).filter((i) => active.match(i.status)), [identities, active])

  async function resolve() {
    setBusy(true)
    setError(null)
    try {
      await api(`/campaigns/${campaign.id}/identities/resolve`, { method: 'POST' })
      await onChange()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Identities</h2>
        {summary && summary.accounts > 0 && <span className="badge">{summary.accounts} accounts</span>}
      </div>
      <p className="panel-text">
        Each account is linked to a person from the HR roster. Strong matches (employee ID, email, username) link
        automatically; weaker or tied matches wait here for you.
      </p>
      <div className="actions">
        <button type="button" className="primary" disabled={!canResolve || busy} onClick={resolve}>
          {busy ? 'Matching…' : resolvedOnce ? 'Re-run matching' : 'Match accounts to people'}
        </button>
        {!canResolve && campaign.state !== 'resolving_identities' && (
          <span className="hint">Upload an HR roster and at least one access list first.</span>
        )}
      </div>
      {error && <div className="error">{error}</div>}

      {resolvedOnce && summary && (
        <>
          <div className="stat-row">
            <Stat label="Matched" value={Number(summary.matched ?? 0)} />
            <Stat label="Needs review" value={Number(summary.ambiguous ?? 0)} tone={summary.ambiguous ? 'warn' : undefined} />
            <Stat label="No match" value={Number(summary.unmatched ?? 0)} tone={summary.unmatched ? 'danger' : undefined} />
            <Stat label="Service / shared" value={Number(summary.non_human ?? 0)} />
            <Stat label="External" value={Number(summary.external ?? 0)} />
            <Stat
              label="Auto-matched"
              value={summary.auto_match_rate === null ? '—' : `${Math.round(Number(summary.auto_match_rate) * 100)}%`}
            />
          </div>
          {summary.ready_for_analysis ? (
            <div className="notice">Every account is resolved. This campaign is ready for analysis.</div>
          ) : (
            <div className="attention-row">
              <span>{summary.open} account{summary.open === 1 ? '' : 's'} still need a decision.</span>
            </div>
          )}

          <div className="tabs" role="tablist">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                role="tab"
                aria-selected={f.key === filter}
                className={'tab' + (f.key === filter ? ' active' : '')}
                onClick={() => setFilter(f.key)}
              >
                {f.label} ({(identities?.items ?? []).filter((i) => f.match(i.status)).length})
              </button>
            ))}
          </div>

          {items.length === 0 ? (
            <div className="empty"><p>Nothing here.</p></div>
          ) : (
            <div className="identity-list">
              {items.map((item) => (
                <IdentityRow
                  key={item.account_id}
                  campaignId={campaign.id}
                  item={item}
                  people={people}
                  editable={editable}
                  onChange={onChange}
                />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}

function Stat({ label, value, tone }: { label: string; value: number | string; tone?: 'warn' | 'danger' }) {
  return (
    <div className="mini-stat">
      <span className="label">{label}</span>
      <span className={'mini-value' + (tone ? ` ${tone}` : '')}>{value}</span>
    </div>
  )
}

function IdentityRow({
  campaignId,
  item,
  people,
  editable,
  onChange,
}: {
  campaignId: string
  item: IdentityItem
  people: PersonRef[]
  editable: boolean
  onChange: () => Promise<void>
}) {
  const api = useApi()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [picking, setPicking] = useState(false)
  const [picked, setPicked] = useState('')
  const [editing, setEditing] = useState(false)
  const isOpen = item.status === 'ambiguous' || item.status === 'unmatched'

  async function decide(body: { person_id?: string; account_type?: string }) {
    setBusy(true)
    setError(null)
    try {
      await api(`/campaigns/${campaignId}/identities/${item.account_id}`, { method: 'POST', body: JSON.stringify(body) })
      setPicking(false)
      setEditing(false)
      await onChange()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const sortedPeople = useMemo(
    () => [...people].sort((a, b) => (a.name ?? a.email ?? '').localeCompare(b.name ?? b.email ?? '')),
    [people],
  )

  return (
    <div className={'identity' + (isOpen ? ' open' : '')}>
      <div className="identity-account">
        <span className="app-tag">{item.application}</span>
        <strong>{item.native_id}</strong>
        <span className="identity-sub">
          {[item.display_name, item.email].filter(Boolean).join(' · ') || 'no name or email in source'}
        </span>
      </div>
      <div className="identity-status">
        <StatusPill status={item.status} />
        {item.person && (
          <span className="identity-person">
            → {item.person.name ?? item.person.email}
            {item.person.employment_status === 'terminated' && <span className="term-tag">terminated</span>}
          </span>
        )}
        {item.method && item.method !== 'none' && (
          <span className="identity-sub">
            {item.method === 'manual' ? `confirmed by ${item.confirmed_by}` : <>by {item.method.replace('_', ' ')} <Confidence value={item.confidence} /></>}
          </span>
        )}
      </div>

      {editable && !isOpen && !editing && (
        <div className="identity-actions">
          <button type="button" className="ghost small" onClick={() => setEditing(true)}>
            Change…
          </button>
        </div>
      )}
      {editable && (isOpen || editing) && (
        <div className="identity-actions">
          {isOpen &&
            item.candidates.map((c) => (
              <button
                key={c.person.id}
                type="button"
                className="candidate"
                disabled={busy}
                onClick={() => decide({ person_id: c.person.id })}
                title={`${c.person.email ?? ''} · matched by ${c.method.replace('_', ' ')}`}
              >
                This is {c.person.name ?? c.person.email} <Confidence value={c.confidence} />
              </button>
            ))}
          {picking ? (
            <span className="picker">
              <select value={picked} onChange={(e) => setPicked(e.target.value)} aria-label="Choose a person">
                <option value="">Choose a person…</option>
                {sortedPeople.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name ?? p.email} {p.employee_id ? `(${p.employee_id})` : ''}
                  </option>
                ))}
              </select>
              <button type="button" className="primary small" disabled={!picked || busy} onClick={() => decide({ person_id: picked })}>
                Link
              </button>
              <button type="button" className="ghost small" onClick={() => setPicking(false)}>Cancel</button>
            </span>
          ) : (
            <button type="button" className="ghost small" disabled={busy} onClick={() => setPicking(true)}>
              {isOpen ? 'Someone else…' : 'Change person…'}
            </button>
          )}
          {(['service', 'shared', 'external'] as const).map((t) => (
            <button key={t} type="button" className="ghost small" disabled={busy} onClick={() => decide({ account_type: t })}>
              {t === 'service' ? 'Service account' : t === 'shared' ? 'Shared account' : 'External user'}
            </button>
          ))}
          {editing && (
            <button type="button" className="ghost small" onClick={() => { setEditing(false); setPicking(false) }}>
              Done
            </button>
          )}
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------

function EvidenceSection({ evidence }: { evidence: Evidence | null }) {
  const [open, setOpen] = useState(false)
  if (!evidence) return null
  const v = evidence.verification
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Evidence trail</h2>
        <span className={'chain ' + (v.valid ? 'valid' : 'broken')}>
          {v.valid ? `✓ Hash chain verified · ${v.events} events` : `✗ Chain broken at event ${v.broken_at_seq}`}
        </span>
      </div>
      <p className="panel-text">
        Every action in this campaign is recorded here. Each event includes the hash of the one before it, so any
        later change to the record is detectable.
      </p>
      <button type="button" className="ghost small" onClick={() => setOpen(!open)}>
        {open ? 'Hide events' : 'Show events'}
      </button>
      {open && (
        <ol className="events">
          {[...evidence.events].reverse().map((e) => (
            <li key={e.seq}>
              <span className="event-seq">#{e.seq}</span>
              <span className="event-type">{e.type}</span>
              <span className="event-actor">{e.actor}</span>
              <time dateTime={e.occurred_at}>{new Date(e.occurred_at).toLocaleString()}</time>
              <code title={e.hash}>{e.hash.slice(0, 10)}…</code>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
