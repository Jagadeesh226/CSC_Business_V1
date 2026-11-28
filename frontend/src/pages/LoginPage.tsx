import { useId, useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { safeReturnPath, signOutRequest, useLogin, useMe, type LoginState, type User } from '../auth/useAuth'
import { ApiError } from '../lib/api'
import { CENTRE_NAME } from '../config'

const schema = z.object({
  email: z.string().trim().min(1, 'Enter your email address.').pipe(z.email('Enter a valid email address.')),
  password: z.string().min(1, 'Enter your password.'),
})
type FormValues = z.infer<typeof schema>

/** Another person signing in does not inherit the previous user's page. */
function returnPathFor(state: LoginState, user: User): string {
  if (state.endedUserId !== undefined && state.endedUserId !== user.id) return '/'
  return safeReturnPath(state.from)
}

export function LoginPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const me = useMe()
  const login = useLogin()
  const state = (location.state ?? {}) as LoginState
  const id = useId()
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema) })

  const [signOutFailed, setSignOutFailed] = useState(state.signOutFailed === true)
  const [retrying, setRetrying] = useState(false)

  if (me.data) return <Navigate to={returnPathFor(state, me.data)} replace />

  const retrySignOut = async () => {
    setRetrying(true)
    try {
      await signOutRequest()
      setSignOutFailed(false)
    } catch {
      // Stay on the warning so the person can try again.
    } finally {
      setRetrying(false)
    }
  }

  const onSubmit = async (values: FormValues) => {
    try {
      const result = await login.mutateAsync(values)
      navigate(returnPathFor(state, result.user), { replace: true })
    } catch {
      // The message is shown from login.error below.
    }
  }

  return (
    <div className="centered-page">
      <main className="card">
        <h1>{CENTRE_NAME}</h1>
        <h2>Sign in</h2>
        {state.message && (
          <div role="status" className="notice notice-warning">
            {state.message}
          </div>
        )}
        {signOutFailed && (
          <div role="alert" className="notice notice-warning">
            <p style={{ margin: 0 }}>
              Sign-out did not reach the server, so your session may still be open. Try again, or
              close the browser before leaving this computer.
            </p>
            <button type="button" className="btn" onClick={() => void retrySignOut()} disabled={retrying} style={{ marginTop: 8 }}>
              {retrying ? 'Signing out...' : 'Try signing out again'}
            </button>
          </div>
        )}
        {login.error && (
          <div role="alert" className="notice notice-error">
            {login.error instanceof ApiError ? login.error.message : 'Could not sign in. Please try again.'}
          </div>
        )}
        <form onSubmit={handleSubmit(onSubmit)} noValidate>
          <div className="field">
            <label htmlFor={`${id}-email`}>Email</label>
            <input
              id={`${id}-email`}
              type="email"
              autoComplete="username"
              aria-invalid={errors.email ? 'true' : undefined}
              aria-describedby={errors.email ? `${id}-email-error` : undefined}
              {...register('email')}
            />
            {errors.email && (
              <span id={`${id}-email-error`} className="field-error" role="alert">
                {errors.email.message}
              </span>
            )}
          </div>
          <div className="field">
            <label htmlFor={`${id}-password`}>Password</label>
            <input
              id={`${id}-password`}
              type="password"
              autoComplete="current-password"
              aria-invalid={errors.password ? 'true' : undefined}
              aria-describedby={errors.password ? `${id}-password-error` : undefined}
              {...register('password')}
            />
            {errors.password && (
              <span id={`${id}-password-error`} className="field-error" role="alert">
                {errors.password.message}
              </span>
            )}
          </div>
          <button type="submit" className="btn btn-primary" disabled={isSubmitting}>
            {isSubmitting ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
      </main>
    </div>
  )
}
