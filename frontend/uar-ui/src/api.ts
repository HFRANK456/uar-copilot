import { useCallback, useEffect, useState } from 'react'
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

// The workspace every request is for. Set by WorkspaceProvider; sent as X-Workspace-Id.
let activeWorkspace: string | null = null
export function setActiveWorkspace(id: string | null) {
  activeWorkspace = id
}
function workspaceHeader(headers: Headers) {
  if (activeWorkspace) headers.set('X-Workspace-Id', activeWorkspace)
}

// Auth0 errors that mean "this session can't be renewed in the background; sign in again".
const SIGN_IN_AGAIN = new Set(['login_required', 'consent_required', 'interaction_required', 'missing_refresh_token', 'invalid_grant'])

/** The signed-in user's access token, sending them to sign in again if it can't be renewed. */
export function useToken() {
  const { getAccessTokenSilently, loginWithRedirect } = useAuth0()
  return useCallback(async (fresh = false): Promise<string> => {
    try {
      return await getAccessTokenSilently(fresh ? { cacheMode: 'off' } : undefined)
    } catch (err) {
      const code = (err as { error?: string }).error
      if (code && SIGN_IN_AGAIN.has(code)) {
        void loginWithRedirect({ appState: { returnTo: window.location.pathname } })
        throw new ApiError(401, 'Your session has expired. Taking you to sign in again…')
      }
      throw err
    }
  }, [getAccessTokenSilently, loginWithRedirect])
}

/** Fetch against the API with the signed-in user's token. */
export function useApi() {
  const getToken = useToken()

  return useCallback(
    async <T,>(path: string, init: RequestInit = {}, freshToken = false): Promise<T> => {
      const headers = new Headers(init.headers)
      if (authEnabled) headers.set('Authorization', `Bearer ${await getToken(freshToken)}`)
      workspaceHeader(headers)
      if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json')
      }
      const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
      if (!res.ok) throw new ApiError(res.status, await readError(res))
      return (await res.json()) as T
    },
    [getToken],
  )
}

/** POST that returns a file; saves it in the browser under the server's filename. */
export function useDownload() {
  const getToken = useToken()

  return useCallback(
    async (path: string, fallbackName: string, method: 'GET' | 'POST' = 'POST'): Promise<string> => {
      const headers = new Headers()
      if (authEnabled) headers.set('Authorization', `Bearer ${await getToken()}`)
      workspaceHeader(headers)
      const res = await fetch(`${API_BASE_URL}${path}`, { method, headers })
      if (!res.ok) throw new ApiError(res.status, await readError(res))
      const match = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') ?? '')
      const name = match?.[1] ?? fallbackName
      const url = URL.createObjectURL(await res.blob())
      const a = document.createElement('a')
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      return name
    },
    [getToken],
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
  source: string // 'upload' or a connector kind, e.g. 'google_workspace'
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
  is_owner: boolean
  signed_off_by: string | null
  signed_off_at: string | null
  snapshots: Snapshot[]
  auto_reminders?: boolean
  last_reminder_at?: string | null
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
  recommendation: 'keep' | 'revoke' | 'modify' | null
  rationale?: string | null
  recommendation_meta?: {
    model?: string
    confidence?: 'low' | 'medium' | 'high'
    cited_finding_ids?: string[]
    generated_at?: string
    generated_by?: string
  } | null
  findings: Finding[]
  decision: Decision | null
  is_mine: boolean
  is_own_access: boolean
  needs_override: boolean
  can_decide: boolean
}

export type Decision = {
  id: string
  action: 'keep' | 'revoke' | 'modify'
  justification: string | null
  actor: string
  decided_at: string
  channel: string
  as_assigned_reviewer: boolean
  override_reason: string | null
}

export type ReviewProgress = {
  total: number
  decided: number
  pending: number
  high_risk_pending: number
  by_action: Record<string, number>
  unassigned: number
  decided_by_owner_override: number
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
  notifications?: {
    emailed: number
    failed: { email?: string; reason: string }[]
    invited_to_workspace: string[]
    email_configured: boolean
  } | null
}

export type Remediation = {
  id: string
  status: 'open' | 'done' | 'verified' | 'accepted'
  action: 'revoke' | 'modify' | null
  severity: string | null
  application: string | null
  application_id: string | null
  account: string | null
  entitlement: string | null
  person: string | null
  change: string | null
  decided_by: string | null
  ticket_ref: string | null
  note: string | null
  resolved_by: string | null
  resolved_at: string | null
  verified_at: string | null
  verification_method: string | null
}

export type RemediationList = {
  summary: { total: number; open: number; done: number; verified: number; accepted: number; ready_to_close: boolean }
  items: Remediation[]
}

export type VerifyResult = {
  result: {
    application: string
    checked: number
    verified: number
    results: { remediation_id: string; account: string; entitlement: string; verified: boolean; detail: string }[]
  }
  remediations: RemediationList
}

export type WorkspaceRole = 'owner' | 'admin' | 'reviewer' | 'auditor'

export type Workspace = {
  id: string
  name: string
  role: WorkspaceRole
  personal: boolean
  invited_by: string | null
}

export type Member = {
  id: string
  email: string | null
  role: WorkspaceRole
  status: 'invited' | 'active'
  invited_by: string | null
  accepted_at: string | null
  is_you: boolean
}

export type ConnectionKind = 'google_workspace' | 'jira_cloud' | 'entra_id'

export type Connection = {
  id: string
  kind: ConnectionKind
  label: string
  name: string
  config: {
    admin_email?: string
    service_account_email?: string
    domain?: string
    site?: string
    email?: string
    project_key?: string | null
    issue_type?: string
    tenant_id?: string
    client_id?: string
  }
  status: 'untested' | 'ok' | 'error'
  last_checked_at: string | null
  last_error: string | null
  created_by: string | null
  created_at: string | null
  can_create_tickets: boolean
  message?: string
}

export type SendTicketsResult = {
  result: { created: { remediation_id: string; ticket_ref: string }[]; already_had_ticket: number; error: string | null }
  remediations: RemediationList
}

export const SOURCE_LABELS: Record<string, string> = {
  upload: 'Uploaded file',
  google_workspace: 'Google Workspace',
  jira_cloud: 'Jira Cloud',
  entra_id: 'Microsoft Entra ID',
}

/** The workspace's connections, for owners and admins (everyone else gets an empty list). */
export function useConnections(enabled: boolean) {
  const api = useApi()
  const [connections, setConnections] = useState<Connection[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    if (!enabled) {
      setConnections([])
      return
    }
    try {
      setConnections(await api<Connection[]>('/connections'))
      setError(null)
    } catch (err) {
      setConnections([])
      setError(err instanceof Error ? err.message : 'Could not load connections')
    }
  }, [api, enabled])

  useEffect(() => {
    let cancelled = false
    Promise.resolve().then(() => {
      if (!cancelled) void reload()
    })
    return () => {
      cancelled = true
    }
  }, [reload])

  return { connections, error, reload }
}

export type RecommendationResult = {
  result: { suggested: number; skipped: number; items_sent: number; model: string | null }
  analysis: Analysis
}
