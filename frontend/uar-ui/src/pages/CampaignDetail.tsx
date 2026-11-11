import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ApiError,
  useApi,
  type Analysis,
  type Application,
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
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)

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

      <Steps hasHr={hasHr} hasAccess={hasAccess} identities={identities} analyzed={Boolean(analysis?.summary)} reviewDone={Boolean(analysis?.progress?.complete && campaign.state === 'in_review')} />

      <SourcesSection campaign={campaign} onChange={refresh} />

      <IdentitySection
        campaign={campaign}
        identities={identities}
        people={people}
        canResolve={hasHr && hasAccess && RESOLVE_STATES.has(campaign.state)}
        onChange={refresh}
      />

      <AnalysisSection campaign={campaign} identities={identities} analysis={analysis} people={people} onChange={refresh} />

      <EvidenceSection evidence={evidence} />
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
}: {
  hasHr: boolean
  hasAccess: boolean
  identities: IdentityList | null
  analyzed: boolean
  reviewDone: boolean
}) {
  const resolved = (identities?.summary.accounts ?? 0) > 0 && (identities?.summary.not_resolved ?? 0) === 0
  const ready = Boolean(identities?.summary.ready_for_analysis)
  const steps = [
    { label: 'Upload HR roster', done: hasHr },
    { label: 'Upload access lists', done: hasAccess },
    { label: 'Match accounts to people', done: resolved },
    { label: 'Resolve the queue', done: ready },
    { label: 'Run analysis', done: analyzed },
    { label: 'Review access', done: reviewDone },
    { label: 'Remediate & export evidence', done: false, soon: true },
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

  const inReview = campaign.state === 'in_review'
  const ready = Boolean(identities?.summary.ready_for_analysis)
  const canRun = ready && (campaign.state === 'resolving_identities' || campaign.state === 'analyzing')
  const summary = analysis?.summary ?? null
  const progress = analysis?.progress ?? null
  const items = useMemo(() => {
    let all = analysis?.items ?? []
    if (mineOnly) all = all.filter((i) => i.is_mine)
    if (pendingOnly) all = all.filter((i) => i.status === 'pending')
    if (severity === 'flagged') return all.filter((i) => i.severity !== 'none')
    if (severity === 'all') return all
    return all.filter((i) => i.severity === severity)
  }, [analysis, severity, mineOnly, pendingOnly])
  const bulkEligible = items.filter(
    (i) => i.status === 'pending' && (i.severity === 'none' || i.severity === 'low') && !i.is_own_access,
  )

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
        <h2>{inReview ? 'Access review' : 'Risk analysis'}</h2>
        {summary?.rule_set_version && <span className="badge">Rules {summary.rule_set_version}</span>}
      </div>
      <p className="panel-text">
        {inReview
          ? 'Decide on each item: keep, revoke, or modify. Keeping high or critical access needs a written justification. Low-risk items can be kept in bulk; high-risk items are always decided one at a time.'
          : 'The rules engine checks every account and entitlement against HR status and the privileged access catalog. Each entitlement becomes one review item, assigned to the person\'s manager.'}
      </p>
      {!inReview && (
        <div className="actions">
          <button type="button" className="primary" disabled={!canRun || busy !== null}
            onClick={() => call('analyze', `/campaigns/${campaign.id}/analyze`, { method: 'POST' })}>
            {busy === 'analyze' ? 'Analyzing…' : summary ? 'Re-run analysis' : 'Run analysis'}
          </button>
          {summary && campaign.state === 'analyzing' && (
            <button type="button" className="primary" disabled={busy !== null}
              onClick={() => call('start', `/campaigns/${campaign.id}/review/start`, { method: 'POST' })}>
              {busy === 'start' ? 'Starting…' : 'Start review'}
            </button>
          )}
          {!ready && <span className="hint">Resolve every identity first.</span>}
          {summary && campaign.state === 'analyzing' && (
            <span className="hint">Starting the review freezes the analysis.</span>
          )}
        </div>
      )}
      {error && <div className="error">{error}</div>}

      {inReview && progress && (
        <div className="progress-block">
          <div className="progress-head">
            <strong>{progress.decided} of {progress.total} decided</strong>
            <span className="hint">
              {progress.by_action.keep} kept · {progress.by_action.revoke} revoked · {progress.by_action.modify} to modify
              {progress.high_risk_pending > 0 && ` · ${progress.high_risk_pending} high-risk still open`}
            </span>
          </div>
          <div className="progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={progress.total}
            aria-valuenow={progress.decided}>
            <span style={{ width: `${progress.total ? (100 * progress.decided) / progress.total : 0}%` }} />
          </div>
          {progress.complete && <div className="notice">Every item has a decision.</div>}
        </div>
      )}

      {summary && (
        <>
          {!inReview && (
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

          <AppOwners campaign={campaign} people={people} onChange={onChange} />

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
              <button type="button" className="ghost small" disabled={busy !== null}
                onClick={() => call('bulk', `/campaigns/${campaign.id}/review-items/bulk-keep`, {
                  method: 'POST', body: JSON.stringify({ item_ids: bulkEligible.map((i) => i.id) }),
                })}>
                {busy === 'bulk' ? 'Keeping…' : `Keep ${bulkEligible.length} low-risk item${bulkEligible.length === 1 ? '' : 's'} shown`}
              </button>
            )}
          </div>

          {items.length === 0 ? (
            <div className="empty"><p>Nothing here.</p></div>
          ) : (
            <div className="review-list">
              {items.map((item) => (
                <ReviewItemRow key={item.id} campaignId={campaign.id} item={item} inReview={inReview}
                  people={people} onChange={onChange} />
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
}: {
  campaignId: string
  item: ReviewItem
  inReview: boolean
  people: PersonRef[]
  onChange: () => Promise<void>
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
    send(`/campaigns/${campaignId}/review-items/${item.id}/decision`, { action, justification })

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
          <strong>{ACTION_LABELS[item.decision.action]}</strong> by {item.decision.actor} ·{' '}
          {new Date(item.decision.decided_at).toLocaleString()}
          {item.decision.justification && <span className="decision-why">“{item.decision.justification}”</span>}
        </div>
      )}

      {inReview && (
        <div className="decision-actions">
          {item.is_own_access ? (
            <span className="hint">This is your own access, so someone else has to review it.</span>
          ) : (
            <>
              <button type="button" className="act keep" disabled={busy || item.status === 'keep'} onClick={keep}>Keep</button>
              <button type="button" className="act revoke" disabled={busy || item.status === 'revoke'} onClick={() => decide('revoke')}>Revoke</button>
              <button type="button" className="act modify" disabled={busy} onClick={() => setMode(mode === 'modify' ? null : 'modify')}>Modify…</button>
            </>
          )}
          <button type="button" className="ghost small" disabled={busy} onClick={() => setMode(mode === 'reassign' ? null : 'reassign')}>
            Reassign…
          </button>
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
