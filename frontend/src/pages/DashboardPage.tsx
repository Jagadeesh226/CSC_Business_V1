import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import type { components } from '../api/schema'
import { formatDate, formatDateTime, statusLabel } from '../lib/format'
import { Empty, ErrorNotice, Loading } from '../components/Status'

type DashboardData = components['schemas']['DashboardSummary']

interface TileData {
  label: string
  value: number
  attention?: boolean
}

function Tiles({ tiles }: { tiles: TileData[] }) {
  return (
    <ul className="tiles">
      {tiles.map((t) => (
        <li key={t.label} className={t.attention && t.value > 0 ? 'tile tile-attention' : 'tile'}>
          <span className="value">{t.value}</span>
          <span className="label">{t.label}</span>
          {t.attention && t.value > 0 && <span className="label"> (needs attention)</span>}
        </li>
      ))}
    </ul>
  )
}

export function DashboardPage() {
  const dashboard = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get('/api/v1/dashboard'),
  })
  const queue = useQuery({
    queryKey: ['work-queue'],
    queryFn: () => api.get('/api/v1/work-queue'),
  })

  return (
    <div>
      <h1>Dashboard</h1>

      <section className="section" aria-labelledby="jobs-heading">
        <h2 id="jobs-heading">Jobs</h2>
        {dashboard.isPending && <Loading label="Loading jobs..." />}
        {dashboard.isError && <ErrorNotice error={dashboard.error} onRetry={() => void dashboard.refetch()} />}
        {dashboard.data && <JobsSection data={dashboard.data} />}
      </section>

      <section className="section" aria-labelledby="queue-heading">
        <h2 id="queue-heading">Work to do</h2>
        {queue.isPending && <Loading label="Loading work queue..." />}
        {queue.isError && <ErrorNotice error={queue.error} onRetry={() => void queue.refetch()} />}
        {queue.data && (
          <Tiles
            tiles={[
              { label: 'Documents to review', value: queue.data.documents_to_review },
              { label: 'Reminders to send', value: queue.data.reminders_to_send },
              { label: 'Unread notifications', value: queue.data.unread_notifications },
              { label: 'Jobs missing documents', value: queue.data.jobs_missing_documents },
              { label: 'Jobs ready to submit', value: queue.data.jobs_ready_to_submit },
              { label: 'Jobs ready to resume', value: queue.data.jobs_ready_to_resume },
              { label: 'Jobs with balance due', value: queue.data.jobs_with_balance_due },
              { label: 'Vehicle readings to check', value: queue.data.vehicle_readings_to_check },
              { label: 'Lookups waiting for captcha', value: queue.data.lookups_waiting_for_captcha },
              ...(queue.data.unassigned_jobs != null
                ? [{ label: 'Unassigned jobs', value: queue.data.unassigned_jobs }]
                : []),
              ...(queue.data.jobs_overpaid != null
                ? [{ label: 'Overpaid jobs', value: queue.data.jobs_overpaid }]
                : []),
            ]}
          />
        )}
      </section>
    </div>
  )
}

function JobsSection({ data }: { data: DashboardData }) {
  const open = data.jobs.open
  const openTotal = open.pending + open.in_progress + open.waiting_for_documents + open.submitted
  return (
    <>
      <p className="hint">
        {data.scope === 'mine' ? 'Showing your own jobs.' : 'Showing all jobs.'} As of {formatDate(data.as_of)}.
      </p>
      <Tiles
        tiles={[
          { label: 'Open jobs', value: openTotal },
          { label: 'Due today', value: data.jobs.due_today },
          { label: 'Overdue', value: data.jobs.overdue, attention: true },
          { label: 'Created today', value: data.jobs.created_today },
          { label: 'Completed today', value: data.jobs.completed_today },
          { label: 'Completed this month', value: data.jobs.completed_this_month },
          ...(data.unassigned_open_jobs != null
            ? [{ label: 'Unassigned open jobs', value: data.unassigned_open_jobs, attention: true }]
            : []),
        ]}
      />
      <h3>Open jobs by status</h3>
      <Tiles
        tiles={[
          { label: statusLabel('pending'), value: open.pending },
          { label: statusLabel('in_progress'), value: open.in_progress },
          { label: statusLabel('waiting_for_documents'), value: open.waiting_for_documents },
          { label: statusLabel('submitted'), value: open.submitted },
        ]}
      />

      <h3>Recent jobs</h3>
      {data.recent_jobs.length === 0 ? (
        <Empty>No jobs yet.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Recent jobs</caption>
            <thead>
              <tr>
                <th scope="col">Job</th>
                <th scope="col">Title</th>
                <th scope="col">Customer</th>
                <th scope="col">Status</th>
                <th scope="col">Last activity</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_jobs.map((j) => (
                <tr key={j.id}>
                  <td>
                    <Link to={`/jobs/${j.id}`}>{j.job_number}</Link>
                  </td>
                  <td>{j.title}</td>
                  <td>{j.customer_name}</td>
                  <td>
                    <span className="status-text">{statusLabel(j.status)}</span>
                  </td>
                  <td>{formatDateTime(j.last_activity_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.workload && (
        <>
          <h3>Team workload</h3>
          {data.workload.length === 0 ? (
            <Empty>No staff to show.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <caption className="visually-hidden">Team workload</caption>
                <thead>
                  <tr>
                    <th scope="col">Employee</th>
                    <th scope="col" className="num">Open jobs</th>
                    <th scope="col" className="num">Overdue</th>
                    <th scope="col" className="num">Completed this month</th>
                  </tr>
                </thead>
                <tbody>
                  {data.workload.map((w) => (
                    <tr key={w.user.id}>
                      <th scope="row">{w.user.name}</th>
                      <td className="num">{w.open_jobs}</td>
                      <td className="num">{w.overdue}</td>
                      <td className="num">{w.completed_this_month}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  )
}
