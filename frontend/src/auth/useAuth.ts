import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError, api, type PostBody } from '../lib/api'
import type { components } from '../api/schema'

export type User = components['schemas']['UserRead']
export const ME_KEY = ['me'] as const

/** The signed-in user, or null when signed out. */
export function useMe() {
  return useQuery({
    queryKey: ME_KEY,
    queryFn: async (): Promise<User | null> => {
      try {
        return await api.get('/api/v1/auth/me', { public: true })
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null
        throw err
      }
    },
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
}

export function useLogin() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: PostBody<'/api/v1/auth/login'>) =>
      api.post('/api/v1/auth/login', { body, public: true }),
    onSuccess: (data) => {
      qc.clear()
      qc.setQueryData(ME_KEY, data.user)
    },
  })
}

export function useChangePassword() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: PostBody<'/api/v1/auth/change-password'>) =>
      api.post('/api/v1/auth/change-password', { body }),
    onSuccess: (user) => {
      qc.setQueryData(ME_KEY, user)
    },
  })
}

/**
 * Ask the server to end the session. Only a 401 counts as "already signed out";
 * any other failure is thrown, because the server session may still be live.
 */
export async function signOutRequest(): Promise<void> {
  try {
    await api.post('/api/v1/auth/logout', { public: true })
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return
    throw err
  }
}

export interface LoginState {
  message?: string
  from?: string
  /** The user whose session ended, so another user does not inherit their page. */
  endedUserId?: number
  /** Sign-out did not reach the server. */
  signOutFailed?: boolean
}

export function useLogout() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  return useMutation({
    mutationFn: signOutRequest,
    onSettled: (_data, error) => {
      // Local data goes either way: nothing of this user stays in memory.
      qc.clear()
      qc.setQueryData(ME_KEY, null)
      const state: LoginState = error ? { signOutFailed: true } : {}
      navigate('/login', { replace: true, state })
    },
  })
}

/** Only allow same-origin, in-app paths as a return target. */
export function safeReturnPath(value: unknown): string {
  if (typeof value !== 'string') return '/'
  if (!/^\/(?![/\\])/.test(value)) return '/'
  // eslint-disable-next-line no-control-regex
  if (/[\\\u0000-\u001f]/.test(value)) return '/'
  try {
    const url = new URL(value, window.location.origin)
    return url.origin === window.location.origin ? value : '/'
  } catch {
    return '/'
  }
}
