import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useMe } from '../auth/useAuth'
import { ApiError, api } from '../lib/api'
import { formatDate, statusLabel } from '../lib/format'
import { AssigneesPanel } from '../components/AssigneesPanel'
import { ChecklistPanel } from '../components/ChecklistPanel'
import { FeesPanel } from '../components/FeesPanel'
import { PaymentSummaryView, StatusHistory } from '../components/JobPanels'
import { hasReceivedPayment, isClosed } from '../lib/jobs'
import { StatusPanel } from '../components/StatusPanel'
import { ErrorNotice, Loading } from '../components/Status'

export function JobDetailPage() {
  const { id } = useParams()
  const jobId = Number(id)
  const me = useMe()
  const isAdmin = me.data?.role === 'admin'
  const valid = Number.isInteger(jobId) && jobId > 0
  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.get('/api/v1/jobs/{job_id}', { params: { job_id: jobId } }),
    enabled: valid,
  })

  if (!valid || (job.isError && job.error instanceof ApiError && job.error.status === 404)) {
    return (
      <div>
        <h1>Job not found</h1>
        <p>This job does not exist, or you are not assigned to it.</p>
        <Link to="/jobs">Back to jobs</Link>
      </div>
    )
  }
  if (job.isPending) return <Loading label="Loading job..." />
  if (job.isError) return <ErrorNotice error={job.error} onRetry={() => void job.refetch()} />

  const j = job.data
  const closed = isClosed(j)
  const readOnly = closed && !isAdmin
  const paid = hasReceivedPayment(j)
  const canAddFee = !closed && (isAdmin || !paid)
  const feeBlocked = closed
    ? 'This job is closed, so fees cannot be added.'
    : paid
      ? 'A payment has been received, so only an admin can add fees now.'
      : undefined

  return (
    <div>
      <p>
        <Link to="/jobs">All jobs</Link>
      </p>
      <header className="job-head">
        <h1>
          {j.job_number}: {j.title}
        </h1>
        <p>
          <span className="status-text">{statusLabel(j.status)}</span>
          {readOnly && <span className="hint"> (read-only: this job is closed)</span>}
        </p>
        <dl className="facts">
          <div>
            <dt>Customer</dt>
            <dd>
              <Link to={`/customers/${j.customer.id}`}>{j.customer.name}</Link>
              {j.customer.phone && <span className="hint"> {j.customer.phone}</span>}
            </dd>
          </div>
          <div>
            <dt>Service</dt>
            <dd>{j.service?.name ?? '-'}</dd>
          </div>
          <div>
            <dt>Due date</dt>
            <dd>{formatDate(j.due_date)}</dd>
          </div>
          <div>
            <dt>Assigned to</dt>
            <dd>{j.assignees.length ? j.assignees.map((a) => a.name).join(', ') : 'Nobody yet'}</dd>
          </div>
          {j.vehicle && (
            <div>
              <dt>Vehicle</dt>
              <dd>{j.vehicle.registration_number}</dd>
            </div>
          )}
        </dl>
        {j.description && <p className="notes">{j.description}</p>}
      </header>

      <section className="section card" aria-labelledby="status-heading">
        <h2 id="status-heading">Status</h2>
        <StatusPanel job={j} isAdmin={isAdmin} />
      </section>

      <section className="section card" aria-labelledby="docs-heading">
        <h2 id="docs-heading">Documents</h2>
        <ChecklistPanel jobId={j.id} customerId={j.customer.id} canChange={!readOnly} />
      </section>

      <section className="section card" aria-labelledby="fees-heading">
        <h2 id="fees-heading">Fees</h2>
        <FeesPanel jobId={j.id} isAdmin={isAdmin} canAdd={canAddFee} addBlockedReason={feeBlocked} />
      </section>

      <section className="section card" aria-labelledby="assign-heading">
        <h2 id="assign-heading">Assigned to</h2>
        <AssigneesPanel job={j} isAdmin={isAdmin} />
      </section>

      <section className="section card" aria-labelledby="pay-heading">
        <h2 id="pay-heading">Payment</h2>
        <PaymentSummaryView job={j} />
      </section>

      <section className="section card" aria-labelledby="history-heading">
        <h2 id="history-heading">Status history</h2>
        <StatusHistory jobId={j.id} />
      </section>
    </div>
  )
}
