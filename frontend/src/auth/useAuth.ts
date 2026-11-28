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

export function useLogout() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  return useMutation({
    mutationFn: async () => {
      try {
        await api.post('/api/v1/auth/logout')
      } catch (err) {
        // Already signed out is fine; anything else still ends the local session.
        if (!(err instanceof ApiError)) throw err
      }
    },
    onSettled: () => {
      qc.clear()
      qc.setQueryData(ME_KEY, null)
      navigate('/login', { replace: true })
    },
  })
}

/** Only allow in-app paths as a return target. */
export function safeReturnPath(value: unknown): string {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') ? value : '/'
}
