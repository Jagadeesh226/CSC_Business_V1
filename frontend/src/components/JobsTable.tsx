import { Link } from 'react-router-dom'
import { formatDate, formatMoney, paymentStateLabel, statusLabel } from '../lib/format'
import { documentProgressText } from '../lib/jobs'
import type { Job } from '../lib/types'

export function JobsTable({ jobs, showCustomer = true }: { jobs: Job[]; showCustomer?: boolean }) {
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">Jobs</caption>
        <thead>
          <tr>
            <th scope="col">Job</th>
            <th scope="col">Title</th>
            {showCustomer && <th scope="col">Customer</th>}
            <th scope="col">Status</th>
            <th scope="col">Due</th>
            <th scope="col">Documents</th>
            <th scope="col">Payment</th>
            <th scope="col">Assigned to</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td>
                <Link to={`/jobs/${j.id}`}>{j.job_number}</Link>
              </td>
              <td>{j.title}</td>
              {showCustomer && (
                <td>
                  <Link to={`/customers/${j.customer.id}`}>{j.customer.name}</Link>
                </td>
              )}
              <td>
                <span className="status-text">{statusLabel(j.status)}</span>
              </td>
              <td>{formatDate(j.due_date)}</td>
              <td>{documentProgressText(j)}</td>
              <td>
                {j.payment_summary ? (
                  <>
                    {paymentStateLabel(j.payment_summary.state)}
                    {j.payment_summary.state !== 'no_fees' && j.payment_summary.state !== 'paid' && (
                      <span className="hint"> (balance {formatMoney(j.payment_summary.balance)})</span>
                    )}
                  </>
                ) : (
                  '-'
                )}
              </td>
              <td>{j.assignees.length ? j.assignees.map((a) => a.name).join(', ') : 'Unassigned'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
