const STATE_LABELS: Record<string, string> = {
  draft: 'Draft',
  ingesting: 'Ingesting',
  resolving_identities: 'Resolving identities',
  analyzing: 'Analyzing',
  in_review: 'In review',
  remediating: 'Remediating',
  verifying: 'Verifying',
  closed: 'Closed',
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`state-badge state-${state}`}>{STATE_LABELS[state] ?? state}</span>
}

const STATUS_LABELS: Record<string, string> = {
  matched: 'Matched',
  ambiguous: 'Needs review',
  unmatched: 'No match',
  non_human: 'Service / shared',
  external: 'External',
  not_resolved: 'Not resolved',
}

export function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status}`}>{STATUS_LABELS[status] ?? status}</span>
}

export function Confidence({ value }: { value: number | null }) {
  if (value === null || value === undefined) return null
  return <span className="confidence">{Math.round(value * 100)}%</span>
}
