import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import { statusLabel } from '../lib/format'
import type { Job, JobStatus } from '../lib/types'
import { Field } from './Field'

const message = (err: unknown) => (err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')

const ACTION_LABELS: Record<string, string> = {
  in_progress: 'Start work',
  waiting_for_documents: 'Wait for documents',
  submitted: 'Mark as submitted',
  completed: 'Mark as completed',
}

/**
 * Status actions come from the job's own allowed_next_statuses. Submitting
 * with unverified documents is refused by the server; admins may then
 * override with a reason.
 */
export function StatusPanel({ job, isAdmin }: { job: Job; isAdmin: boolean }) {
  const qc = useQueryClient()
  const [overrideFor, setOverrideFor] = useState<JobStatus | null>(null)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState('')
  const [cancelling, setCancelling] = useState(false)
  const [cancelReason, setCancelReason] = useState('')
  const [cancelError, setCancelError] = useState('')

  const done = (data: Job) => {
    qc.setQueryData(['job', job.id], data)
    void qc.invalidateQueries({ queryKey: ['job', job.id, 'status-history'] })
    void qc.invalidateQueries({ queryKey: ['jobs'] })
    setOverrideFor(null)
    setReason('')
    setCancelling(false)
    setCancelReason('')
  }

  const change = useMutation({
    mutationFn: (v: { status: JobStatus; override?: boolean; reason?: string }) =>
      api.post('/api/v1/jobs/{job_id}/status', {
        params: { job_id: job.id },
        body: {
          status: v.status,
          override_document_check: v.override === true,
          ...(v.override ? { reason: v.reason } : {}),
        },
      }),
    onSuccess: done,
  })
  const cancel = useMutation({
    mutationFn: (why: string) =>
      api.post('/api/v1/jobs/{job_id}/cancel', { params: { job_id: job.id }, body: { reason: why } }),
    onSuccess: done,
  })

  const next = job.allowed_next_statuses.filter((s) => s !== 'cancelled')
  const canCancel = isAdmin && job.allowed_next_statuses.includes('cancelled')
  const closed = next.length === 0 && !canCancel

  const move = (status: JobStatus) => {
    change.reset()
    setOverrideFor(null)
    change.mutate({ status })
  }
  // The server's refusal for unverified documents: admins can take over.
  const refusedSubmit =
    isAdmin && change.isError && change.variables?.status === 'submitted' && !change.variables.override && change.error instanceof ApiError && change.error.status === 422

  return (
    <div>
      <p>
        Current status: <strong className="status-text">{statusLabel(job.status)}</strong>
      </p>
      {closed && <p className="hint">This job is {statusLabel(job.status).toLowerCase()}. No further status changes are possible.</p>}
      {next.length > 0 && (
        <div className="actions" role="group" aria-label="Change status">
          {next.map((s) => (
            <button key={s} type="button" className="btn" disabled={change.isPending} onClick={() => move(s)}>
              {ACTION_LABELS[s] ?? statusLabel(s)}
            </button>
          ))}
        </div>
      )}

      {change.isError && (
        <div role="alert" className="notice notice-error">
          {message(change.error)}
          {refusedSubmit && overrideFor === null && (
            <>
              {' '}
              <button type="button" className="btn" onClick={() => setOverrideFor('submitted')}>
                Override the document check
              </button>
            </>
          )}
        </div>
      )}

      {overrideFor && (
        <form
          className="card section"
          aria-label="Override document check"
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            if (!reason.trim()) {
              setReasonError('Enter a reason for submitting without verified documents.')
              return
            }
            setReasonError('')
            change.mutate({ status: overrideFor, override: true, reason: reason.trim() })
          }}
        >
          <p>
            Submitting without every required document verified is recorded in the audit log with your reason.
          </p>
          <Field label="Reason for the override" error={reasonError}>
            {(p) => <textarea {...p} rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />}
          </Field>
          <div className="actions">
            <button type="submit" className="btn btn-primary" disabled={change.isPending}>
              Submit anyway
            </button>
            <button type="button" className="btn" onClick={() => setOverrideFor(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {canCancel && !cancelling && (
        <button
          type="button"
          className="btn btn-danger"
          onClick={() => {
            cancel.reset()
            setCancelling(true)
          }}
        >
          Cancel job
        </button>
      )}
      {cancelling && (
        <form
          className="card section"
          aria-label="Cancel job"
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            if (!cancelReason.trim()) {
              setCancelError('Enter a reason for cancelling this job.')
              return
            }
            setCancelError('')
            cancel.mutate(cancelReason.trim())
          }}
        >
          <p>Cancelling is final. The job cannot be reopened.</p>
          {cancel.isError && (
            <div role="alert" className="notice notice-error">
              {message(cancel.error)}
            </div>
          )}
          <Field label="Reason for cancelling" error={cancelError}>
            {(p) => <textarea {...p} rows={2} maxLength={1000} value={cancelReason} onChange={(e) => setCancelReason(e.target.value)} />}
          </Field>
          <div className="actions">
            <button type="submit" className="btn btn-danger" disabled={cancel.isPending}>
              Cancel this job
            </button>
            <button type="button" className="btn" onClick={() => setCancelling(false)}>
              Keep the job
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
