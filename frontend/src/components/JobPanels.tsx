import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { formatDateTime, formatMoney, paymentStateLabel, statusLabel } from '../lib/format'
import type { Job } from '../lib/types'
import { Empty, ErrorNotice, Loading } from './Status'

export function StatusHistory({ jobId }: { jobId: number }) {
  const history = useQuery({
    queryKey: ['job', jobId, 'status-history'],
    queryFn: () => api.get('/api/v1/jobs/{job_id}/status-history', { params: { job_id: jobId } }),
  })
  if (history.isPending) return <Loading label="Loading history..." />
  if (history.isError) return <ErrorNotice error={history.error} onRetry={() => void history.refetch()} />
  if (history.data.length === 0) return <Empty>No status changes yet.</Empty>
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">Status history</caption>
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Change</th>
            <th scope="col">By</th>
            <th scope="col">Reason</th>
          </tr>
        </thead>
        <tbody>
          {history.data.map((h) => (
            <tr key={h.id}>
              <td>{formatDateTime(h.changed_at)}</td>
              <td>
                {h.old_status ? `${statusLabel(h.old_status)} to ${statusLabel(h.new_status)}` : statusLabel(h.new_status)}
              </td>
              <td>{h.changed_by?.name ?? '-'}</td>
              <td>{h.reason ?? '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function PaymentSummaryView({ job }: { job: Job }) {
  const s = job.payment_summary
  if (!s) return <Empty>No payment information.</Empty>
  return (
    <div>
      <dl className="facts">
        <div>
          <dt>Fees total</dt>
          <dd>{formatMoney(s.fees_total)}</dd>
        </div>
        <div>
          <dt>Paid</dt>
          <dd>{formatMoney(s.paid)}</dd>
        </div>
        <div>
          <dt>Balance</dt>
          <dd>{formatMoney(s.balance)}</dd>
        </div>
        <div>
          <dt>Payment status</dt>
          <dd>{paymentStateLabel(s.state)}</dd>
        </div>
      </dl>
      <p className="hint">Recording payments and receipts comes in a later step.</p>
    </div>
  )
}
