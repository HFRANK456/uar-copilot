import { useCallback } from 'react'
import { useAuth0 } from '@auth0/auth0-react'
import { API_BASE_URL, authEnabled } from './config'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function readError(res: Response): Promise<string> {
  const text = await res.text()
  try {
    const body = JSON.parse(text) as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((d: { msg?: string; loc?: unknown[] }) => `${(d.loc ?? []).slice(1).join('.')}: ${d.msg}`)
        .join('; ')
    }
  } catch {
    // not JSON
  }
  return text || `Request failed (${res.status})`
}

/** Fetch against the API with the signed-in user's token. */
export function useApi() {
  const { getAccessTokenSilently } = useAuth0()

  return useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      const headers = new Headers(init.headers)
      if (authEnabled) headers.set('Authorization', `Bearer ${await getAccessTokenSilently()}`)
      if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json')
      }
      const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
      if (!res.ok) throw new ApiError(res.status, await readError(res))
      return (await res.json()) as T
    },
    [getAccessTokenSilently],
  )
}

// ---- Types mirroring the backend schemas ----

export type ColumnMapping = {
  source: string
  target: string
  matched_column: string | null
  confidence: number
  method: string
  needs_confirmation: boolean
}

export type Snapshot = {
  id: string
  kind: 'hr_roster' | 'access_list'
  application: string | null
  filename: string | null
  sha256: string
  size_bytes: number
  row_count: number
  column_mapping: ColumnMapping[]
  needs_confirmation: boolean
  mapping_confirmed_by: string | null
  created_at: string | null
}

export type Campaign = {
  id: string
  name: string
  state: string
  period_start: string | null
  period_end: string | null
  due_date: string | null
  reviewer_rule: string
  rule_set_version: string | null
  created_by: string | null
  created_at: string | null
  snapshots: Snapshot[]
}

export type IngestResult = {
  snapshot: Snapshot
  counts: Record<string, number>
  warnings: string[]
  campaign_state: string
}

export type PersonRef = {
  id: string
  employee_id: string | null
  name: string | null
  email: string | null
  department: string | null
  employment_status: string | null
}

export type IdentityItem = {
  account_id: string
  application: string | null
  native_id: string
  username: string | null
  email: string | null
  display_name: string | null
  account_type: string
  status: string
  method: string | null
  confidence: number | null
  person: PersonRef | null
  confirmed_by: string | null
  candidates: { person: PersonRef; method: string; confidence: number }[]
}

export type IdentityList = {
  summary: {
    accounts: number
    open: number
    ready_for_analysis: boolean
    auto_match_rate: number | null
    [status: string]: number | boolean | null
  }
  items: IdentityItem[]
}

export type EvidenceEvent = {
  seq: number
  type: string
  actor: string
  actor_kind: string
  occurred_at: string
  payload: Record<string, unknown>
  prev_hash: string
  hash: string
}

export type Evidence = {
  campaign_id: string
  verification: { valid: boolean; events: number; head_hash?: string; broken_at_seq?: number }
  events: EvidenceEvent[]
}

export type Finding = {
  id: string
  rule_id: string
  rule_version: number
  severity: string
  account_id: string | null
  inputs: Record<string, unknown>
  explanation: string
}

export type ReviewItem = {
  id: string
  severity: string
  status: string
  application: string
  entitlement: string
  privilege_tier: string | null
  account_id: string
  native_id: string
  account_type: string
  account_status: string | null
  last_login_at: string | null
  person: PersonRef | null
  reviewer: PersonRef | null
  recommendation: string | null
  findings: Finding[]
  decision: Decision | null
  is_mine: boolean
  is_own_access: boolean
}

export type Decision = {
  id: string
  action: 'keep' | 'revoke' | 'modify'
  justification: string | null
  actor: string
  decided_at: string
  channel: string
}

export type ReviewProgress = {
  total: number
  decided: number
  pending: number
  high_risk_pending: number
  by_action: Record<string, number>
  unassigned: number
  complete: boolean
}

export type Application = {
  id: string
  name: string
  owner: PersonRef | null
}

export type Analysis = {
  summary: {
    review_items: number
    items_by_severity: Record<string, number>
    findings: number
    findings_by_severity: Record<string, number>
    unassigned: number
    rule_set_version: string | null
  } | null
  progress: ReviewProgress | null
  items: ReviewItem[]
}
