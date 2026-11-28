import { useEffect, useRef } from 'react'
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ME_KEY, useMe, type LoginState, type User } from './useAuth'
import { setApiHandlers } from '../lib/api'
import { ErrorNotice, Loading } from '../components/Status'

/** Wires API-level events (401, "change your password") to navigation. */
export function ApiEvents() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const here = useRef(location)
  useEffect(() => {
    here.current = location
  })
  useEffect(() => {
    setApiHandlers({
      onSessionEnded: (message) => {
        const { pathname, search } = here.current
        const ended = qc.getQueryData<User | null>(ME_KEY)
        // Several requests can fail together: only the first one ends the session.
        if (!ended) return
        const endedUserId = ended.id
        qc.clear()
        qc.setQueryData(ME_KEY, null)
        if (pathname === '/login') return
        const state: LoginState = { message, from: pathname + search, endedUserId }
        navigate('/login', { replace: true, state })
      },
      onPasswordChangeRequired: () => {
        void qc.invalidateQueries({ queryKey: ME_KEY })
        navigate('/change-password', { replace: true })
      },
    })
    return () => setApiHandlers({})
  }, [qc, navigate])
  return null
}

/** Signed-out users go to /login; users who must change their password go to /change-password. */
export function RequireAuth({ allowForcedChange = false }: { allowForcedChange?: boolean }) {
  const me = useMe()
  const location = useLocation()

  if (me.isPending) return <Loading label="Checking your sign-in..." />
  if (me.isError) {
    return (
      <div className="centered-page">
        <ErrorNotice error={me.error} onRetry={() => void me.refetch()} />
      </div>
    )
  }
  if (!me.data) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: location.pathname + location.search }}
      />
    )
  }
  if (me.data.must_change_password && !allowForcedChange) {
    return <Navigate to="/change-password" replace />
  }
  return <Outlet />
}

/** Employees see a clear message; the backend still enforces access. */
export function RequireAdmin() {
  const me = useMe()
  if (me.data?.role !== 'admin') {
    return (
      <div>
        <h1>Admins only</h1>
        <p>This page is only for administrators. If you need access, please ask your admin.</p>
      </div>
    )
  }
  return <Outlet />
}
