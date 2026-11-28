import type { ReactNode } from 'react'
import { ApiError } from '../lib/api'

export function Loading({ label = 'Loading...' }: { label?: string }) {
  return (
    <p role="status" className="hint">
      {label}
    </p>
  )
}

export function ErrorNotice({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message =
    error instanceof ApiError ? error.message : 'Something went wrong. Please try again.'
  return (
    <div role="alert" className="notice notice-error">
      <p style={{ margin: 0 }}>{message}</p>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry} style={{ marginTop: 8 }}>
          Try again
        </button>
      )}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="hint">{children}</p>
}
