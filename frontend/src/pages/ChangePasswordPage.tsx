import { useId } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useChangePassword, useLogout, useMe } from '../auth/useAuth'
import { ApiError } from '../lib/api'

const schema = z
  .object({
    current_password: z.string().min(1, 'Enter your current password.'),
    new_password: z
      .string()
      .min(10, 'The new password must be at least 10 characters.')
      .max(128, 'The new password must be at most 128 characters.'),
    confirm: z.string().min(1, 'Type the new password again.'),
  })
  .refine((v) => v.new_password === v.confirm, {
    path: ['confirm'],
    message: 'The two new passwords do not match.',
  })
type FormValues = z.infer<typeof schema>

const FIELDS = [
  { name: 'current_password', label: 'Current password', autoComplete: 'current-password' },
  { name: 'new_password', label: 'New password', autoComplete: 'new-password', hint: 'At least 10 characters.' },
  { name: 'confirm', label: 'Type the new password again', autoComplete: 'new-password' },
] as const

export function ChangePasswordPage() {
  const me = useMe()
  const navigate = useNavigate()
  const change = useChangePassword()
  const logout = useLogout()
  const id = useId()
  const forced = me.data?.must_change_password ?? false
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema) })

  const onSubmit = async (values: FormValues) => {
    try {
      await change.mutateAsync({
        current_password: values.current_password,
        new_password: values.new_password,
      })
      navigate('/', { replace: true })
    } catch (err) {
      if (err instanceof ApiError) {
        for (const [field, message] of Object.entries(err.fieldErrors)) {
          if (field === 'current_password' || field === 'new_password') setError(field, { message })
        }
      }
    }
  }

  return (
    <div className="centered-page">
      <main className="card">
        <h1>Change password</h1>
        {forced && (
          <div role="status" className="notice notice-warning">
            You must choose a new password before you can use the system.
          </div>
        )}
        {change.error && (
          <div role="alert" className="notice notice-error">
            {change.error instanceof ApiError ? change.error.message : 'Could not change the password.'}
          </div>
        )}
        <form onSubmit={handleSubmit(onSubmit)} noValidate>
          {FIELDS.map((f) => {
            const error = errors[f.name]
            return (
              <div className="field" key={f.name}>
                <label htmlFor={`${id}-${f.name}`}>{f.label}</label>
                <input
                  id={`${id}-${f.name}`}
                  type="password"
                  autoComplete={f.autoComplete}
                  aria-invalid={error ? 'true' : undefined}
                  aria-describedby={error ? `${id}-${f.name}-error` : undefined}
                  {...register(f.name)}
                />
                {'hint' in f && <span className="hint">{f.hint}</span>}
                {error && (
                  <span id={`${id}-${f.name}-error`} className="field-error" role="alert">
                    {error.message}
                  </span>
                )}
              </div>
            )
          })}
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <button type="submit" className="btn btn-primary" disabled={isSubmitting}>
              {isSubmitting ? 'Saving...' : 'Change password'}
            </button>
            {forced ? (
              <button type="button" className="btn" onClick={() => logout.mutate()}>
                Sign out
              </button>
            ) : (
              <Link className="btn" to="/">
                Cancel
              </Link>
            )}
          </div>
        </form>
      </main>
    </div>
  )
}
