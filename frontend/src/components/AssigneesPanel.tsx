import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import type { Job } from '../lib/types'
import { Field } from './Field'

const message = (err: unknown) => (err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')

/** Who is assigned. Only admins can change it; employees just see the names. */
export function AssigneesPanel({ job, isAdmin }: { job: Job; isAdmin: boolean }) {
  const qc = useQueryClient()
  const [userId, setUserId] = useState('')
  const users = useQuery({
    queryKey: ['users', 'active'],
    queryFn: () => api.get('/api/v1/users', { query: { is_active: true, page_size: 100 } }),
    enabled: isAdmin,
  })

  const done = (data: Job) => {
    qc.setQueryData(['job', job.id], data)
    void qc.invalidateQueries({ queryKey: ['jobs'] })
  }
  const assign = useMutation({
    mutationFn: (id: number) =>
      api.post('/api/v1/jobs/{job_id}/assignments', { params: { job_id: job.id }, body: { user_id: id } }),
    onSuccess: (data) => {
      setUserId('')
      done(data)
    },
  })
  const unassign = useMutation({
    mutationFn: (id: number) =>
      api.delete('/api/v1/jobs/{job_id}/assignments/{user_id}', { params: { job_id: job.id, user_id: id } }),
    onSuccess: done,
  })

  const assignedIds = new Set(job.assignees.map((a) => a.id))
  const options = (users.data?.items ?? []).filter((u) => !assignedIds.has(u.id))
  const error = assign.error ?? unassign.error

  return (
    <div>
      {job.assignees.length === 0 ? (
        <p className="hint">Nobody is assigned to this job yet.</p>
      ) : (
        <ul className="plain-list" aria-label="Assigned to">
          {job.assignees.map((a) => (
            <li key={a.id} className="row-between">
              <span>{a.name}</span>
              {isAdmin && (
                <button
                  type="button"
                  className="btn"
                  disabled={unassign.isPending}
                  onClick={() => {
                    assign.reset()
                    unassign.mutate(a.id)
                  }}
                >
                  Unassign<span className="visually-hidden"> {a.name}</span>
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {error && (
        <div role="alert" className="notice notice-error">
          {message(error)}
        </div>
      )}
      {isAdmin && (
        <>
          {users.isError && (
            <div role="alert" className="notice notice-error">
              {message(users.error)}{' '}
              <button type="button" className="btn" onClick={() => void users.refetch()}>
                Try again
              </button>
            </div>
          )}
          <Field label="Assign to">
            {(p) => (
              <div className="inline">
                <select {...p} value={userId} onChange={(e) => setUserId(e.target.value)} disabled={users.isPending}>
                  <option value="">{users.isPending ? 'Loading people...' : 'Choose a person'}</option>
                  {options.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.name} ({u.role === 'admin' ? 'Admin' : 'Employee'})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn"
                  disabled={!userId || assign.isPending}
                  onClick={() => {
                    unassign.reset()
                    assign.mutate(Number(userId))
                  }}
                >
                  {assign.isPending ? 'Assigning...' : 'Assign'}
                </button>
              </div>
            )}
          </Field>
        </>
      )}
    </div>
  )
}
