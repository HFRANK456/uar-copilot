import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ApiError,
  SOURCE_LABELS,
  useApi,
  useConnections,
  useDownload,
  type SendTicketsResult,
  type Analysis,
  type Application,
  type Remediation,
  type RemediationList,
  type VerifyResult,
  type ReviewItem,
  type Campaign,
  type Evidence,
  type IdentityItem,
  type IdentityList,
  type IngestResult,
  type PersonRef,
  type Snapshot,
} from '../api'
import { Confidence, StateBadge, StatusPill } from './shared'
import { canManage, useWorkspace } from '../workspace'

const INGEST_STATES = new Set(['draft', 'ingesting', 'resolving_identities', 'analyzing'])
const RESOLVE_STATES = new Set(['ingesting', 'resolving_identities'])
// Service, shared and bot accounts (e.g. Atlassian's built-in app accounts) aren't people.
const isBot = (i: ReviewItem) => i.account_type === 'service' || i.account_type === 'shared'

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
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const manage = canManage(useWorkspace().current)

  const refresh = useCallback(async () => {
    try {
      const [c, ids, ev, ppl, an] = await Promise.all([
        api<Campaign>(`/campaigns/${id}`),
        api<IdentityList>(`/campaigns/${id}/identities`),
        api<Evidence>(`/campaigns/${id}/evidence`),
        api<PersonRef[]>(`/campaigns/${id}/people`),
        api<Analysis>(`/campaigns/${id}/review-items`),
      ])
      setCampaign(c)
      setIdentities(ids)
      setEvidence(ev)
      setPeople(ppl)
      setAnalysis(an)
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

      <Steps hasHr={hasHr} hasAccess={hasAccess} identities={identities} analyzed={Boolean(analysis?.summary)} reviewDone={Boolean(analysis?.progress?.complete)}
        signedOff={Boolean(campaign.signed_off_at)} exported={Boolean(evidence?.events.some((e) => e.type === 'evidence.exported'))}
        closed={campaign.state === 'closed'} />

      <SourcesSection campaign={campaign} onChange={refresh} />

      <IdentitySection
        campaign={campaign}
        identities={identities}
        people={people}
        canResolve={manage && hasHr && hasAccess && RESOLVE_STATES.has(campaign.state)}
        onChange={refresh}
      />

      <AnalysisSection campaign={campaign} identities={identities} analysis={analysis} people={people} onChange={refresh} />

      {['remediating', 'verifying', 'closed'].includes(campaign.state) && (
        <RemediationSection campaign={campaign} onChange={refresh} />
      )}

      <EvidenceSection campaign={campaign} evidence={evidence} onChange={refresh} />
    </div>
  )
}

// ---------------------------------------------------------------------------

function Steps({
  hasHr,
  hasAccess,
  identities,
  analyzed,
  reviewDone,
  signedOff,
  exported,
  closed,
}: {
  hasHr: boolean
  hasAccess: boolean
  identities: IdentityList | null
  analyzed: boolean
  reviewDone: boolean
  signedOff: boolean
  exported: boolean
  closed: boolean
}) {
  const resolved = (identities?.summary.accounts ?? 0) > 0 && (identities?.summary.not_resolved ?? 0) === 0
  const ready = Boolean(identities?.summary.ready_for_analysis)
  const steps: { label: string; done: boolean; soon?: boolean }[] = [
    { label: 'Upload HR roster', done: hasHr },
    { label: 'Upload access lists', done: hasAccess },
    { label: 'Match accounts to people', done: resolved },
    { label: 'Resolve the queue', done: ready },
    { label: 'Run analysis', done: analyzed },
    { label: 'Review access', done: reviewDone },
    { label: 'Sign off', done: signedOff },
    { label: 'Remediate & verify', done: closed },
    { label: 'Close campaign', done: closed },
    { label: 'Export evidence', done: exported },
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
  const manage = canManage(useWorkspace().current)
  const canIngest = manage && INGEST_STATES.has(campaign.state)
  const [hrFile, setHrFile] = useState<File | null>(null)
  const [accessFile, setAccessFile] = useState<File | null>(null)
  const [appName, setAppName] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [resetKey, setResetKey] = useState(0)
  const [message, setMessage] = useState<{ kind: 'error' | 'ok'; text: string; warnings?: string[] } | null>(null)
  const { connections } = useConnections(canIngest)
  const pullable = connections ?? []
  const [connId, setConnId] = useState('')

  async function pull() {
    const conn = pullable.find((c) => c.id === connId)
    if (!conn) return
    setBusy('pull')
    setMessage(null)
    try {
      const r = await api<IngestResult>(`/campaigns/${campaign.id}/sources/connection`, {
        method: 'POST', body: JSON.stringify({ connection_id: conn.id }),
      })
      const c = r.counts
      setMessage({
        kind: 'ok',
        text: `Pulled ${c.accounts} accounts with ${c.entitlement_grants} entitlement grants from ${conn.label}.`,
        warnings: r.warnings,
      })
      setConnId('')
      await onChange()
    } catch (err) {
      setMessage({ kind: 'error', text: `${conn.label}: ${errorText(err)}` })
    } finally {
      setBusy(null)
    }
  }

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
        <span className="badge">Files or connections</span>
      </div>
      {!canIngest && (
        <p className="panel-text">
          {manage ? 'This campaign is past the upload stage.' : 'Only workspace owners and admins can upload files.'}
        </p>
      )}
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
          <div className="upload-box">
            <h3>Pull from a connection</h3>
            {pullable.length === 0 ? (
              <p className="panel-text">
                Connect Google Workspace or Jira in <Link to="/workspace">Workspace settings</Link> to pull users and
                their access directly, without exporting a file.
              </p>
            ) : (
              <>
                <p className="panel-text">Fetches current users and their admin roles or groups, read-only.</p>
                <select className="text-input" value={connId} onChange={(e) => setConnId(e.target.value)}
                  aria-label="Connection to pull from">
                  <option value="">Choose a connection…</option>
                  {pullable.map((c) => (
                    <option key={c.id} value={c.id}>{c.label}{c.status === 'error' ? ' (not working)' : ''}</option>
                  ))}
                </select>
                <button type="button" className="primary" disabled={!connId || busy !== null} onClick={pull}>
                  {busy === 'pull' ? 'Pulling…' : 'Pull access list'}
                </button>
              </>
            )}
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
          {s.source && s.source !== 'upload' ? `via ${SOURCE_LABELS[s.source] ?? s.source}` : s.filename} · {s.row_count} rows · sha256 <code title={s.sha256}>{s.sha256.slice(0, 12)}…</code>
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
  const editable = canManage(useWorkspace().current) && campaign.state === 'resolving_identities'
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
          <span className="hint">
            {RESOLVE_STATES.has(campaign.state) || campaign.state === 'draft'
              ? 'Upload an HR roster and at least one access list first.'
              : 'Matching is locked once analysis has run. Upload a new file to start over.'}
          </span>
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
            <div className="notice">
              Every account is resolved.{campaign.state === 'resolving_identities' && ' This campaign is ready for analysis.'}
            </div>
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

const SEVERITIES = ['critical', 'high', 'medium', 'low', 'none'] as const

const RULE_LABELS: Record<string, string> = {
  terminated_privileged_active: 'Terminated, still has privileged access',
  terminated_active: 'Terminated, account still active',
  terminated_active_within_grace: 'Recently terminated, still active (within grace period)',
  privileged_access_critical: 'Critical-tier privileged access',
  privileged_access_high: 'High-tier privileged access',
  privileged_access_medium: 'Sensitive access',
  external_account_active: 'External user with active access',
  dormant_account: 'Dormant account',
  admin_role: 'Admin-like role not in the privileged catalog',
}

function AnalysisSection({
  campaign,
  identities,
  analysis,
  people,
  onChange,
}: {
  campaign: Campaign
  identities: IdentityList | null
  analysis: Analysis | null
  people: PersonRef[]
  onChange: () => Promise<void>
}) {
  const api = useApi()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [severity, setSeverity] = useState<string>('flagged')
  const [mineOnly, setMineOnly] = useState(false)
  const [pendingOnly, setPendingOnly] = useState(false)
  const [who, setWho] = useState<'all' | 'people' | 'bots'>('all')

  const inReview = campaign.state === 'in_review'
  const reviewed = inReview || ['remediating', 'verifying', 'closed'].includes(campaign.state)
  const [statement, setStatement] = useState('')
  const [overrideReason, setOverrideReason] = useState('')
  const overrideOk = overrideReason.trim().length >= 10
  const ready = Boolean(identities?.summary.ready_for_analysis)
  const manage = canManage(useWorkspace().current)
  const canRun = manage && ready && (campaign.state === 'resolving_identities' || campaign.state === 'analyzing')
  const summary = analysis?.summary ?? null
  const progress = analysis?.progress ?? null
  const botCount = useMemo(() => (analysis?.items ?? []).filter(isBot).length, [analysis])
  const { items, hiddenBots } = useMemo(() => {
    let all = analysis?.items ?? []
    if (mineOnly) all = all.filter((i) => i.is_mine)
    if (pendingOnly) all = all.filter((i) => i.status === 'pending')
    if (severity === 'flagged') all = all.filter((i) => i.severity !== 'none')
    else if (severity !== 'all') all = all.filter((i) => i.severity === severity)
    const bots = all.filter(isBot)
    const humans = all.filter((i) => !isBot(i))
    if (who === 'people') return { items: humans, hiddenBots: bots }
    if (who === 'bots') return { items: bots, hiddenBots: [] }
    return { items: [...humans, ...bots], hiddenBots: [] } // people first
  }, [analysis, severity, mineOnly, pendingOnly, who])
  const bulkEligible = items.filter(
    (i) => i.status === 'pending' && (i.severity === 'none' || i.severity === 'low') && !i.is_own_access && i.can_decide,
  )
  const bulkNeedsOverride = bulkEligible.some((i) => i.needs_override)

  async function call(label: string, path: string, init: RequestInit) {
    setBusy(label)
    setError(null)
    try {
      await api(path, init)
      await onChange()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{reviewed ? 'Access review' : 'Risk analysis'}</h2>
        {summary?.rule_set_version && <span className="badge">Rules {summary.rule_set_version}</span>}
      </div>
      <p className="panel-text">
        {reviewed
          ? 'Decide on each item: keep, revoke, or modify. Keeping high or critical access needs a written justification. Low-risk items can be kept in bulk; high-risk items are always decided one at a time.'
          : 'The rules engine checks every account and entitlement against HR status and the privileged access catalog. Each entitlement becomes one review item, assigned to the person\'s manager.'}
      </p>
      {!reviewed && (
        <div className="actions">
          <button type="button" className="primary" disabled={!canRun || busy !== null}
            onClick={() => call('analyze', `/campaigns/${campaign.id}/analyze`, { method: 'POST' })}>
            {busy === 'analyze' ? 'Analyzing…' : summary ? 'Re-run analysis' : 'Run analysis'}
          </button>
          {manage && summary && campaign.state === 'analyzing' && (
            <button type="button" className="primary" disabled={busy !== null}
              onClick={() => call('start', `/campaigns/${campaign.id}/review/start`, { method: 'POST' })}>
              {busy === 'start' ? 'Starting…' : 'Start review'}
            </button>
          )}
          {!ready && <span className="hint">Resolve every identity first.</span>}
          {manage && summary && campaign.state === 'analyzing' && (
            <span className="hint">Starting the review freezes the analysis.</span>
          )}
        </div>
      )}
      {error && <div className="error">{error}</div>}

      {campaign.signed_off_at && (
        <div className="notice">
          Signed off by {campaign.signed_off_by} on {new Date(campaign.signed_off_at).toLocaleString()}.{' '}
          {campaign.state === 'remediating'
            ? `${(progress?.by_action.revoke ?? 0) + (progress?.by_action.modify ?? 0)} access change(s) still have to be made. Remediation tickets are coming next.`
            : 'Campaign closed.'}
        </div>
      )}

      {reviewed && progress && (
        <div className="progress-block">
          <div className="progress-head">
            <strong>{progress.decided} of {progress.total} decided</strong>
            <span className="hint">
              {progress.by_action.keep} kept · {progress.by_action.revoke} revoked · {progress.by_action.modify} to modify
              {progress.high_risk_pending > 0 && ` · ${progress.high_risk_pending} high-risk still open`}
              {progress.decided_by_owner_override > 0 && ` · ${progress.decided_by_owner_override} decided by the owner instead of the reviewer`}
            </span>
          </div>
          <div className="progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={progress.total}
            aria-valuenow={progress.decided}>
            <span style={{ width: `${progress.total ? (100 * progress.decided) / progress.total : 0}%` }} />
          </div>
          {progress.complete && inReview && campaign.is_owner && (
            <form
              className="signoff"
              onSubmit={(e) => {
                e.preventDefault()
                void call('signoff', `/campaigns/${campaign.id}/sign-off`, {
                  method: 'POST', body: JSON.stringify({ statement: statement.trim() || null }),
                })
              }}
            >
              <strong>Every item has a decision. Ready to sign off?</strong>
              <p className="hint">
                Signing off records that the review is complete and locks all decisions. Revoked or modified access
                then moves to remediation.
              </p>
              <label htmlFor="signoff-statement">Attestation (optional)</label>
              <textarea id="signoff-statement" rows={2} maxLength={2000} value={statement}
                onChange={(e) => setStatement(e.target.value)}
                placeholder="e.g. Reviewed with IT; all decisions are final for Q4 2026." />
              <div>
                <button type="submit" className="primary" disabled={busy !== null}>
                  {busy === 'signoff' ? 'Signing off…' : 'Sign off campaign'}
                </button>
              </div>
            </form>
          )}
        </div>
      )}

      {summary && (
        <>
          {!reviewed && (
            <>
              <div className="stat-row">
                {SEVERITIES.map((s) => (
                  <div key={s} className={`mini-stat sev-${s}`}>
                    <span className="label">{s === 'none' ? 'No findings' : s}</span>
                    <span className="mini-value">{summary.items_by_severity[s] ?? 0}</span>
                  </div>
                ))}
              </div>
              <p className="hint">
                {summary.review_items} review items · {summary.findings} findings
                {summary.unassigned > 0 && ` · ${summary.unassigned} without a reviewer`}
              </p>
            </>
          )}

          {campaign.is_owner && <AppOwners campaign={campaign} people={people} onChange={onChange} />}

          {inReview && campaign.is_owner && (
            <div className="override-box">
              <label htmlFor="override-reason">
                <strong>Deciding for reviewers?</strong> Each item should be decided by its assigned reviewer. As
                campaign owner you can decide instead (or decide items with no reviewer), but the evidence pack records
                that you did and why.
              </label>
              <textarea id="override-reason" rows={2} maxLength={2000} value={overrideReason}
                onChange={(e) => setOverrideReason(e.target.value)}
                placeholder="e.g. Reviewers not yet onboarded; decisions confirmed with each manager by email" />
              <span className="hint">
                {overrideOk
                  ? 'This reason is saved with every decision you make for someone else.'
                  : 'Needed (10+ characters) before you can decide items assigned to someone else.'}
              </span>
            </div>
          )}

          <div className="tabs" role="tablist">
            {[
              ['flagged', 'With findings'],
              ...SEVERITIES.slice(0, 4).map((s) => [s, s[0].toUpperCase() + s.slice(1)]),
              ['all', 'All'],
            ].map(([key, label]) => (
              <button key={key} type="button" role="tab" aria-selected={severity === key}
                className={'tab' + (severity === key ? ' active' : '')} onClick={() => setSeverity(key)}>
                {label}
              </button>
            ))}
          </div>
          <div className="filter-row">
            {botCount > 0 && (
              <select value={who} onChange={(e) => setWho(e.target.value as 'all' | 'people' | 'bots')}
                aria-label="Which accounts to show">
                <option value="all">Everyone (people first)</option>
                <option value="people">People only</option>
                <option value="bots">Service and bot accounts ({botCount})</option>
              </select>
            )}
            <label className="check">
              <input type="checkbox" checked={mineOnly} onChange={(e) => setMineOnly(e.target.checked)} />
              Assigned to me
            </label>
            {inReview && (
              <label className="check">
                <input type="checkbox" checked={pendingOnly} onChange={(e) => setPendingOnly(e.target.checked)} />
                Undecided only
              </label>
            )}
            {inReview && bulkEligible.length > 0 && (
              <button type="button" className="ghost small" disabled={busy !== null || (bulkNeedsOverride && !overrideOk)}
                title={bulkNeedsOverride && !overrideOk ? 'Add an override reason above first' : undefined}
                onClick={() => call('bulk', `/campaigns/${campaign.id}/review-items/bulk-keep`, {
                  method: 'POST',
                  body: JSON.stringify({
                    item_ids: bulkEligible.map((i) => i.id),
                    override_reason: bulkNeedsOverride ? overrideReason.trim() : null,
                  }),
                })}>
                {busy === 'bulk' ? 'Keeping…' : `Keep ${bulkEligible.length} low-risk item${bulkEligible.length === 1 ? '' : 's'} shown`}
              </button>
            )}
          </div>

          {hiddenBots.length > 0 && (
            <p className={hiddenBots.some((i) => i.severity !== 'none') ? 'attention-row' : 'hint'}>
              {hiddenBots.length} service or bot account item(s) hidden
              {hiddenBots.some((i) => i.severity !== 'none') &&
                `, ${hiddenBots.filter((i) => i.severity !== 'none').length} with findings`}
              . They still need a decision before sign-off.
            </p>
          )}

          {items.length === 0 ? (
            <div className="empty"><p>Nothing here.</p></div>
          ) : (
            <div className="review-list">
              {items.map((item) => (
                <ReviewItemRow key={item.id} campaignId={campaign.id} item={item} inReview={inReview}
                  people={people} onChange={onChange} isOwner={campaign.is_owner} overrideReason={overrideReason.trim()} />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}

function AppOwners({ campaign, people, onChange }: { campaign: Campaign; people: PersonRef[]; onChange: () => Promise<void> }) {
  const api = useApi()
  const [apps, setApps] = useState<Application[] | null>(null)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function toggle() {
    if (!open && apps === null) {
      try {
        setApps(await api<Application[]>(`/campaigns/${campaign.id}/applications`))
      } catch (err) {
        setError(errorText(err))
      }
    }
    setOpen(!open)
  }

  async function setOwner(appId: string, personId: string) {
    setError(null)
    try {
      await api(`/campaigns/${campaign.id}/applications/${appId}/owner`, {
        method: 'PUT', body: JSON.stringify({ person_id: personId || null }),
      })
      setApps(await api<Application[]>(`/campaigns/${campaign.id}/applications`))
      await onChange()
    } catch (err) {
      setError(errorText(err))
    }
  }

  return (
    <div className="owners">
      <button type="button" className="ghost small" onClick={toggle}>
        {open ? 'Hide application owners' : 'Application owners'}
      </button>
      {open && apps && (
        <div className="owner-list">
          <p className="hint">An app owner reviews items that have no manager to review them.</p>
          {apps.map((a) => (
            <label key={a.id} className="owner-row">
              <span>{a.name}</span>
              <select value={a.owner?.id ?? ''} onChange={(e) => setOwner(a.id, e.target.value)} aria-label={`Owner of ${a.name}`}>
                <option value="">No owner</option>
                {people.map((p) => <option key={p.id} value={p.id}>{p.name ?? p.email}</option>)}
              </select>
            </label>
          ))}
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

const ACTION_LABELS: Record<string, string> = { keep: 'Kept', revoke: 'Revoke', modify: 'Modify' }

function ReviewItemRow({
  campaignId,
  item,
  inReview,
  people,
  onChange,
  isOwner,
  overrideReason,
}: {
  campaignId: string
  item: ReviewItem
  inReview: boolean
  people: PersonRef[]
  onChange: () => Promise<void>
  isOwner: boolean
  overrideReason: string
}) {
  const api = useApi()
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<null | 'keep' | 'modify' | 'reassign'>(null)
  const [text, setText] = useState('')
  const [reviewer, setReviewer] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const needsJustification = item.severity === 'critical' || item.severity === 'high'

  const who = item.person
    ? item.person.name ?? item.person.email
    : item.account_type === 'service' || item.account_type === 'shared'
      ? `${item.account_type} account`
      : item.account_type === 'external' ? 'External user' : '—'

  async function send(path: string, body: object) {
    setBusy(true)
    setError(null)
    try {
      await api(path, { method: 'POST', body: JSON.stringify(body) })
      setMode(null)
      setText('')
      await onChange()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }
  const decide = (action: string, justification?: string) =>
    send(`/campaigns/${campaignId}/review-items/${item.id}/decision`, {
      action,
      justification,
      override_reason: item.needs_override ? overrideReason : undefined,
    })
  const reviewerName = item.reviewer ? item.reviewer.name ?? item.reviewer.email : null
  const blockedByOverride = item.needs_override && overrideReason.length < 10

  function keep() {
    if (needsJustification) setMode('keep')
    else void decide('keep')
  }

  const decided = item.status !== 'pending'

  return (
    <div className={`review-item sev-border-${item.severity}` + (decided ? ' decided' : '')}>
      <div className="review-main">
        <span className={'severity ' + item.severity}>{item.severity === 'none' ? 'ok' : item.severity}</span>
        <div className="review-what">
          <strong>{item.entitlement}</strong>
          <span className="identity-sub">{item.application} · {item.native_id}{item.account_status === 'inactive' ? ' · disabled' : ''}</span>
        </div>
        <div className="review-who">
          <span>
            {who}
            {item.person?.employment_status === 'terminated' && <span className="term-tag">terminated</span>}
          </span>
          <span className="identity-sub">
            Reviewer: {item.reviewer ? item.reviewer.name ?? item.reviewer.email : <em>unassigned</em>}
            {item.is_mine && <span className="mine-tag">you</span>}
          </span>
        </div>
      </div>

      {item.findings.length > 0 && (
        <div className="review-findings">
          {item.findings.map((f) => (
            <div key={f.id} className="finding">
              <span className={'dot ' + f.severity} aria-hidden="true" />
              <span>{RULE_LABELS[f.rule_id] ?? f.rule_id}</span>
            </div>
          ))}
          <button type="button" className="ghost small" onClick={() => setOpen(!open)}>
            {open ? 'Hide evidence' : 'Why?'}
          </button>
          {open && (
            <div className="finding-detail">
              {item.findings.map((f) => (
                <p key={f.id}><code>{f.rule_id} v{f.rule_version}</code> {f.explanation}</p>
              ))}
            </div>
          )}
        </div>
      )}

      {item.decision && (
        <div className={`decision decision-${item.decision.action}`}>
          <strong>{ACTION_LABELS[item.decision.action]}</strong> by {item.decision.actor}
          {item.decision.as_assigned_reviewer ? ' (assigned reviewer)' : ' (campaign owner)'} ·{' '}
          {new Date(item.decision.decided_at).toLocaleString()}
          {item.decision.override_reason && (
            <span className="decision-why">Override reason: “{item.decision.override_reason}”</span>
          )}
          {item.decision.justification && <span className="decision-why">“{item.decision.justification}”</span>}
        </div>
      )}

      {inReview && (
        <div className="decision-actions">
          {item.is_own_access ? (
            <span className="hint">This is your own access, so someone else has to review it.</span>
          ) : !item.can_decide ? (
            <span className="hint">
              Assigned to {reviewerName ?? 'nobody yet'}. Only they or the campaign owner can decide this item.
            </span>
          ) : blockedByOverride ? (
            <span className="hint">
              {reviewerName ? `Assigned to ${reviewerName}. ` : 'No reviewer assigned. '}
              To decide it yourself, add an override reason in the box above.
            </span>
          ) : (
            <>
              <button type="button" className="act keep" disabled={busy || item.status === 'keep'} onClick={keep}>Keep</button>
              <button type="button" className="act revoke" disabled={busy || item.status === 'revoke'} onClick={() => decide('revoke')}>Revoke</button>
              <button type="button" className="act modify" disabled={busy} onClick={() => setMode(mode === 'modify' ? null : 'modify')}>Modify…</button>
            </>
          )}
          {isOwner && (
            <button type="button" className="ghost small" disabled={busy} onClick={() => setMode(mode === 'reassign' ? null : 'reassign')}>
              Reassign…
            </button>
          )}
        </div>
      )}

      {(mode === 'keep' || mode === 'modify') && (
        <form className="justify" onSubmit={(e) => { e.preventDefault(); void decide(mode, text) }}>
          <label htmlFor={`j-${item.id}`}>
            {mode === 'keep'
              ? `Why should this ${item.severity}-risk access stay? (required)`
              : 'What should change? (required)'}
          </label>
          <textarea id={`j-${item.id}`} rows={2} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)}
            placeholder={mode === 'keep' ? 'e.g. On-call admin, approved by CISO in SEC-12' : 'e.g. Downgrade to ReadOnlyAccess'} />
          <div className="justify-actions">
            <button type="submit" className="primary small" disabled={busy || text.trim().length < 10}>
              {mode === 'keep' ? 'Keep with justification' : 'Request change'}
            </button>
            <button type="button" className="ghost small" onClick={() => setMode(null)}>Cancel</button>
            {text.trim().length > 0 && text.trim().length < 10 && <span className="hint">At least 10 characters.</span>}
          </div>
        </form>
      )}

      {mode === 'reassign' && (
        <div className="picker">
          <select value={reviewer} onChange={(e) => setReviewer(e.target.value)} aria-label="New reviewer">
            <option value="">Choose a reviewer…</option>
            {people.filter((p) => p.id !== item.person?.id).map((p) => (
              <option key={p.id} value={p.id}>{p.name ?? p.email}</option>
            ))}
          </select>
          <button type="button" className="primary small" disabled={!reviewer || busy}
            onClick={() => send(`/campaigns/${campaignId}/review-items/${item.id}/reassign`, { person_id: reviewer })}>
            Reassign
          </button>
          <button type="button" className="ghost small" onClick={() => setMode(null)}>Cancel</button>
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------

const REM_LABELS: Record<string, string> = {
  open: 'To do',
  done: 'Done, not verified',
  verified: 'Verified',
  accepted: 'Accepted',
}

function RemediationSection({ campaign, onChange }: { campaign: Campaign; onChange: () => Promise<void> }) {
  const api = useApi()
  const download = useDownload()
  const [data, setData] = useState<RemediationList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [verifyApp, setVerifyApp] = useState('')
  const [verifyFile, setVerifyFile] = useState<File | null>(null)
  const [verifyKey, setVerifyKey] = useState(0)
  const [verifyResult, setVerifyResult] = useState<VerifyResult['result'] | null>(null)
  const owner = campaign.is_owner
  const editable = owner && campaign.state === 'remediating'
  const { connections } = useConnections(editable)
  const conns = useMemo(() => connections ?? [], [connections])
  const jira = conns.filter((c) => c.can_create_tickets)
  const [jiraId, setJiraId] = useState('')
  const [sent, setSent] = useState<SendTicketsResult['result'] | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await api<RemediationList>(`/campaigns/${campaign.id}/remediations`))
    } catch (err) {
      setError(errorText(err))
    }
  }, [api, campaign.id])

  useEffect(() => {
    let cancelled = false
    Promise.resolve().then(() => {
      if (!cancelled) void load()
    })
    return () => {
      cancelled = true
    }
  }, [load, campaign.state])

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label)
    setError(null)
    try {
      await fn()
      await load()
      await onChange()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  const apps = useMemo(() => {
    const m = new Map<string, string>()
    for (const r of data?.items ?? []) {
      if (r.application_id && r.application && (r.status === 'open' || r.status === 'done')) m.set(r.application_id, r.application)
    }
    return [...m.entries()]
  }, [data])

  // An application whose list came from a connection can be re-checked by pulling it again.
  const verifyConn = useMemo(() => {
    const name = apps.find(([id]) => id === verifyApp)?.[1]
    const snap = campaign.snapshots.find((x) => x.kind === 'access_list' && x.application === name && x.source !== 'upload')
    return snap ? conns.find((c) => c.kind === snap.source) ?? null : null
  }, [apps, verifyApp, campaign.snapshots, conns])

  async function verifyByPull() {
    if (!verifyConn || !verifyApp) return
    await run('repull', async () => {
      const r = await api<VerifyResult>(`/campaigns/${campaign.id}/remediations/verify-pull`, {
        method: 'POST', body: JSON.stringify({ application_id: verifyApp, connection_id: verifyConn.id }),
      })
      setVerifyResult(r.result)
    })
  }

  async function verify() {
    if (!verifyFile || !verifyApp) return
    const form = new FormData()
    form.append('application_id', verifyApp)
    form.append('file', verifyFile)
    await run('verify', async () => {
      const r = await api<VerifyResult>(`/campaigns/${campaign.id}/remediations/verify`, { method: 'POST', body: form })
      setVerifyResult(r.result)
      setVerifyFile(null)
      setVerifyKey((k) => k + 1)
    })
  }

  const s = data?.summary
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Remediation</h2>
        {s && <span className="badge">{s.verified + s.accepted} of {s.total} confirmed</span>}
      </div>
      <p className="panel-text">
        Every revoke or modify decision is an access change someone has to make. Record the ticket and when it's
        done, then upload a fresh access list for that application (or pull it again from its connection): the change counts as <strong>verified</strong>{' '}
        only when the new list no longer shows the access. If a system has no export, accept it with a written reason.
      </p>
      {error && <div className="error">{error}</div>}
      {s && s.total === 0 && <div className="notice">No access changes were needed.</div>}

      {s && s.total > 0 && (
        <>
          <div className="actions">
            <button type="button" className="ghost small" disabled={busy !== null}
              onClick={() => run('tickets', () => download(`/campaigns/${campaign.id}/remediations/tickets.csv`,
                'remediation-tickets.csv', 'GET'))}>
              Download tickets (CSV for Jira import)
            </button>
            {editable && jira.length > 0 && (
              <>
                {jira.length > 1 && (
                  <select value={jiraId} onChange={(e) => setJiraId(e.target.value)} aria-label="Jira connection">
                    <option value="">Jira project…</option>
                    {jira.map((c) => <option key={c.id} value={c.id}>{c.config.project_key} on {c.config.site?.replace('https://', '')}</option>)}
                  </select>
                )}
                <button type="button" className="primary small"
                  disabled={busy !== null || (jira.length > 1 && !jiraId) || data!.items.every((r) => r.ticket_ref || r.status === 'verified' || r.status === 'accepted')}
                  onClick={() => run('jira', async () => {
                    setSent(null)
                    const r = await api<SendTicketsResult>(`/campaigns/${campaign.id}/remediations/send-tickets`, {
                      method: 'POST', body: JSON.stringify({ connection_id: jiraId || jira[0].id }),
                    })
                    setSent(r.result)
                  })}>
                  {busy === 'jira' ? 'Sending…' : `Send to Jira${jira.length === 1 ? ` (${jira[0].config.project_key})` : ''}`}
                </button>
              </>
            )}
          </div>
          {sent && (
            <div className={sent.error ? 'error' : 'notice'}>
              {sent.created.length > 0
                ? `Created ${sent.created.length} Jira ticket(s): ${sent.created.map((t) => t.ticket_ref).join(', ')}.`
                : 'No new tickets: every open change already has one.'}
              {sent.error && ` Stopped: ${sent.error}`}
            </div>
          )}

          <div className="rem-list">
            {data!.items.map((r) => (
              <RemediationRow key={r.id} campaignId={campaign.id} rem={r} editable={editable}
                busy={busy !== null} run={run} />
            ))}
          </div>

          {editable && apps.length > 0 && (
            <div className="verify-box">
              <h3>Verify with a fresh access list</h3>
              <p className="hint">
                After IT has made the changes, upload the application's current users and roles, or, for an
                application that came from a connection, pull it again.
              </p>
              <div className="verify-row">
                <select value={verifyApp} onChange={(e) => setVerifyApp(e.target.value)} aria-label="Application to verify">
                  <option value="">Application…</option>
                  {apps.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
                </select>
                <input key={verifyKey} type="file" accept=".csv,.xlsx,.xls" aria-label="Fresh access list"
                  onChange={(e) => setVerifyFile(e.target.files?.[0] ?? null)} />
                <button type="button" className="primary small" disabled={!verifyApp || !verifyFile || busy !== null}
                  onClick={verify}>
                  {busy === 'verify' ? 'Checking…' : 'Check changes'}
                </button>
              </div>
              {verifyConn && (
                <div className="verify-row">
                  <span className="hint">Or skip the export:</span>
                  <button type="button" className="primary small" disabled={busy !== null} onClick={verifyByPull}>
                    {busy === 'repull' ? `Pulling from ${verifyConn.label}…` : `Re-pull from ${verifyConn.label} and check`}
                  </button>
                </div>
              )}
            </div>
          )}

          {verifyResult && (
            <div className={verifyResult.verified === verifyResult.checked ? 'notice' : 'attention-row'}>
              {verifyResult.application}: {verifyResult.verified} of {verifyResult.checked} change(s) confirmed.
              {verifyResult.results.filter((x) => !x.verified).map((x) => (
                <div key={x.remediation_id}>Still there: {x.account} · {x.entitlement} ({x.detail})</div>
              ))}
            </div>
          )}

          {campaign.state === 'remediating' && owner && (
            <div className="actions">
              <button type="button" className="primary" disabled={!s.ready_to_close || busy !== null}
                onClick={() => run('close', () => api(`/campaigns/${campaign.id}/close`, { method: 'POST' }))}>
                {busy === 'close' ? 'Closing…' : 'Close campaign'}
              </button>
              {!s.ready_to_close && (
                <span className="hint">{s.open + s.done} change(s) still need verifying or accepting.</span>
              )}
            </div>
          )}
          {campaign.state === 'closed' && <div className="notice">Campaign closed. Every access change is confirmed.</div>}
        </>
      )}
    </section>
  )
}

function RemediationRow({
  campaignId,
  rem: r,
  editable,
  busy,
  run,
}: {
  campaignId: string
  rem: Remediation
  editable: boolean
  busy: boolean
  run: (label: string, fn: () => Promise<unknown>) => Promise<void>
}) {
  const api = useApi()
  const [ticket, setTicket] = useState(r.ticket_ref ?? '')
  const [mode, setMode] = useState<null | 'accept'>(null)
  const [reason, setReason] = useState('')
  const openish = r.status === 'open' || r.status === 'done'
  const post = (path: string, body: object) => api(path, { method: 'POST', body: JSON.stringify(body) })

  return (
    <div className={`rem rem-${r.status}`}>
      <div className="rem-main">
        <span className={`status-pill rem-pill-${r.status}`}>{REM_LABELS[r.status]}</span>
        <div className="review-what">
          <strong>{r.action === 'revoke' ? 'Remove' : 'Change'} {r.entitlement}</strong>
          <span className="identity-sub">{r.application} · {r.account}{r.person ? ` · ${r.person}` : ''}</span>
          {r.action === 'modify' && r.change && <span className="identity-sub">Requested: {r.change}</span>}
        </div>
        <div className="review-who">
          {r.ticket_ref && <span className="identity-sub">Ticket {r.ticket_ref}</span>}
          {(r.verification_method === 'reupload' || r.verification_method === 'repull') && r.verified_at && (
            <span className="identity-sub">
              Confirmed by {r.verification_method === 'repull' ? 'a fresh pull' : 'upload'},{' '}
              {new Date(r.verified_at).toLocaleDateString()}
            </span>
          )}
          {r.status === 'accepted' && r.note && <span className="identity-sub">Accepted: “{r.note}”</span>}
          {r.status === 'done' && <span className="identity-sub">Marked done by {r.resolved_by}</span>}
        </div>
      </div>
      {editable && openish && (
        <div className="decision-actions">
          <input className="text-input small-input" placeholder="Ticket no., e.g. IT-101" value={ticket} maxLength={255}
            onChange={(e) => setTicket(e.target.value)} aria-label="Ticket number" />
          <button type="button" className="ghost small" disabled={busy || ticket.trim() === (r.ticket_ref ?? '')}
            onClick={() => run('ticket', () => post(`/campaigns/${campaignId}/remediations/${r.id}`, { ticket_ref: ticket }))}>
            Save ticket
          </button>
          {r.status === 'open' && (
            <button type="button" className="act keep" disabled={busy}
              onClick={() => run('done', () => post(`/campaigns/${campaignId}/remediations/${r.id}`, { status: 'done' }))}>
              Mark done
            </button>
          )}
          <button type="button" className="ghost small" disabled={busy} onClick={() => setMode(mode ? null : 'accept')}>
            Accept without upload…
          </button>
        </div>
      )}
      {mode === 'accept' && openish && editable && (
        <form className="justify" onSubmit={(e) => {
          e.preventDefault()
          void run('accept', () => post(`/campaigns/${campaignId}/remediations/${r.id}/accept`, { reason }))
        }}>
          <label htmlFor={`acc-${r.id}`}>Why can't this be confirmed with an upload? (required)</label>
          <textarea id={`acc-${r.id}`} rows={2} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Vendor portal has no user export; removal confirmed by vendor email, ticket IT-102" />
          <div className="justify-actions">
            <button type="submit" className="primary small" disabled={busy || reason.trim().length < 10}>Accept</button>
            <button type="button" className="ghost small" onClick={() => setMode(null)}>Cancel</button>
          </div>
        </form>
      )}
    </div>
  )
}

const EXPORTABLE = new Set(['analyzing', 'in_review', 'remediating', 'verifying', 'closed'])

function EvidenceSection({
  campaign,
  evidence,
  onChange,
}: {
  campaign: Campaign
  evidence: Evidence | null
  onChange: () => Promise<void>
}) {
  const download = useDownload()
  const role = useWorkspace().current?.role
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)
  if (!evidence) return null
  const v = evidence.verification
  const canExport = (role !== 'reviewer') && EXPORTABLE.has(campaign.state)
  const draft = !campaign.signed_off_at

  async function exportPack() {
    setBusy(true)
    setMessage(null)
    try {
      const name = await download(`/campaigns/${campaign.id}/evidence-pack`, 'evidence-pack.zip')
      setMessage({ kind: 'ok', text: `Downloaded ${name}. The export is recorded in the trail below.` })
      await onChange()
    } catch (err) {
      setMessage({ kind: 'error', text: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

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
      <div className="export-box">
        <div>
          <strong>Evidence pack</strong>
          <p className="hint">
            A ZIP for your auditor: PDF summary, the uploaded files, identities, findings, every decision and this
            trail, each with a SHA-256 fingerprint.
            {draft && canExport && ' Until the campaign is signed off, the pack is marked DRAFT.'}
            {!canExport && ' Available once the analysis has run.'}
          </p>
        </div>
        <button type="button" className="primary" disabled={!canExport || busy} onClick={exportPack}>
          {busy ? 'Preparing…' : draft ? 'Download draft pack' : 'Download evidence pack'}
        </button>
      </div>
      {message && <div className={message.kind === 'error' ? 'error' : 'notice'}>{message.text}</div>}
      <button type="button" className="ghost small events-toggle" onClick={() => setOpen(!open)}>
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
