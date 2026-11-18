import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useAuth0 } from '@auth0/auth0-react'
import { setActiveWorkspace, useApi, type Workspace } from './api'
import { authEnabled } from './config'

const STORAGE_KEY = 'uar.workspace'

type WorkspaceState = {
  workspaces: Workspace[]
  invitations: Workspace[]
  current: Workspace | null
  loading: boolean
  emailReady: boolean
  error: string | null
  select: (id: string) => void
  refresh: (freshIdentity?: boolean) => Promise<void>
}

const Ctx = createContext<WorkspaceState | null>(null)

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

function store(id: string) {
  try {
    localStorage.setItem(STORAGE_KEY, id)
  } catch {
    // private mode etc.: the choice just won't persist
  }
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const api = useApi()
  const { isAuthenticated, isLoading: authLoading } = useAuth0()
  const signedIn = !authEnabled || isAuthenticated
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [invitations, setInvitations] = useState<Workspace[]>([])
  const [currentId, setCurrentId] = useState<string | null>(readStored)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [emailReady, setEmailReady] = useState(true)

  const refresh = useCallback(async (freshIdentity = false) => {
    try {
      setActiveWorkspace(null)
      const body = await api<{ workspaces: Workspace[]; invitations: Workspace[]; email_ready: boolean }>('/workspaces', {}, freshIdentity)
      setEmailReady(body.email_ready)
      setWorkspaces(body.workspaces)
      setInvitations(body.invitations)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load workspaces')
    } finally {
      setLoading(false)
    }
  }, [api])

  useEffect(() => {
    if ((authEnabled && authLoading) || !signedIn) return
    let cancelled = false
    Promise.resolve().then(() => {
      if (!cancelled) void refresh()
    })
    return () => {
      cancelled = true
    }
  }, [authLoading, signedIn, refresh])

  useEffect(() => {
    if (!signedIn || emailReady) return
    const onFocus = () => { void refresh(true) }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [signedIn, emailReady, refresh])

  // Fall back to the personal workspace if the stored one is gone (e.g. removed from it).
  const current = useMemo(
    () => workspaces.find((w) => w.id === currentId) ?? workspaces.find((w) => w.personal) ?? workspaces[0] ?? null,
    [workspaces, currentId],
  )
  setActiveWorkspace(current?.id ?? null)

  const select = useCallback((id: string) => {
    store(id)
    setCurrentId(id)
  }, [])

  const value = useMemo(
    () => ({ workspaces, invitations, current, loading: loading && signedIn, emailReady, error, select, refresh }),
    [workspaces, invitations, current, loading, signedIn, emailReady, error, select, refresh],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useWorkspace(): WorkspaceState {
  const v = useContext(Ctx)
  if (!v) throw new Error('useWorkspace must be used inside WorkspaceProvider')
  return v
}

// eslint-disable-next-line react-refresh/only-export-components
export function canManage(w: Workspace | null) {
  return w?.role === 'owner' || w?.role === 'admin'
}
