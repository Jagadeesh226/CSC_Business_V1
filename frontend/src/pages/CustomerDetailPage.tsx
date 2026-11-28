import { useRef, useState, type KeyboardEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMe } from '../auth/useAuth'
import { ApiError, api } from '../lib/api'
import { documentStatusLabel, reviewPath, uploadDocument } from '../lib/documents'
import { ACCEPT, validateUpload } from '../lib/files'
import { formatDate, formatDateTime, formatMoney } from '../lib/format'
import { CustomerEdit } from '../components/CustomerEdit'
import { Field } from '../components/Field'
import { JobsTable } from '../components/JobsTable'
import { Pagination } from '../components/Pagination'
import { Empty, ErrorNotice, Loading } from '../components/Status'

type TabKey = 'overview' | 'jobs' | 'documents' | 'vehicles' | 'receipts' | 'activity'
const TABS: { key: TabKey; label: string; adminOnly?: boolean }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'jobs', label: 'Jobs' },
  { key: 'documents', label: 'Documents' },
  { key: 'vehicles', label: 'Vehicles' },
  { key: 'receipts', label: 'Receipts' },
  { key: 'activity', label: 'Activity', adminOnly: true },
]

export function CustomerDetailPage() {
  const { id } = useParams()
  const customerId = Number(id)
  const valid = Number.isInteger(customerId) && customerId > 0
  const me = useMe()
  const isAdmin = me.data?.role === 'admin'
  const [params, setParams] = useSearchParams()
  const tabs = TABS.filter((t) => !t.adminOnly || isAdmin)
  const requested = params.get('tab')
  const active = tabs.find((t) => t.key === requested)?.key ?? 'overview'
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({})

  const customer = useQuery({
    queryKey: ['customer', customerId],
    queryFn: () => api.get('/api/v1/customers/{customer_id}', { params: { customer_id: customerId } }),
    enabled: valid,
  })

  if (!valid || (customer.isError && customer.error instanceof ApiError && customer.error.status === 404)) {
    return (
      <div>
        <h1>Customer not found</h1>
        <Link to="/customers">Back to customers</Link>
      </div>
    )
  }
  if (customer.isPending) return <Loading label="Loading customer..." />
  if (customer.isError) return <ErrorNotice error={customer.error} onRetry={() => void customer.refetch()} />
  const c = customer.data

  const select = (key: TabKey) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (key === 'overview') next.delete('tab')
        else next.set('tab', key)
        return next
      },
      { replace: true },
    )
  const onTabKey = (e: KeyboardEvent, index: number) => {
    let to = -1
    if (e.key === 'ArrowRight') to = (index + 1) % tabs.length
    else if (e.key === 'ArrowLeft') to = (index - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') to = 0
    else if (e.key === 'End') to = tabs.length - 1
    if (to < 0) return
    e.preventDefault()
    select(tabs[to].key)
    tabRefs.current[tabs[to].key]?.focus()
  }

  return (
    <div>
      <p>
        <Link to="/customers">All customers</Link>
      </p>
      <header className="job-head">
        <h1>{c.name}</h1>
        <dl className="facts">
          <div>
            <dt>Customer ID</dt>
            <dd>{c.id}</dd>
          </div>
          <div>
            <dt>Mobile</dt>
            <dd>{c.phone ?? '-'}</dd>
          </div>
        </dl>
        <div className="actions">
          <Link className="btn btn-primary" to={`/jobs/new?customer_id=${c.id}`}>
            New job for this customer
          </Link>
          <Link className="btn" to={`/customers/${c.id}?tab=documents`}>
            Upload document
          </Link>
        </div>
      </header>

      <div role="tablist" aria-label="Customer sections" className="tabs">
        {tabs.map((t, i) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            id={`tab-${t.key}`}
            aria-selected={active === t.key}
            aria-controls={`panel-${t.key}`}
            tabIndex={active === t.key ? 0 : -1}
            ref={(el) => {
              tabRefs.current[t.key] = el
            }}
            onClick={() => select(t.key)}
            onKeyDown={(e) => onTabKey(e, i)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${active}`} aria-labelledby={`tab-${active}`} className="tabpanel">
        {active === 'overview' && <Overview customerId={c.id} />}
        {active === 'jobs' && <CustomerJobs customerId={c.id} />}
        {active === 'documents' && <CustomerDocuments customerId={c.id} />}
        {active === 'vehicles' && <CustomerVehicles customerId={c.id} />}
        {active === 'receipts' && <CustomerReceipts customerId={c.id} />}
        {active === 'activity' && isAdmin && <CustomerActivity customerId={c.id} />}
      </div>
    </div>
  )
}

function Overview({ customerId }: { customerId: number }) {
  const [editing, setEditing] = useState(false)
  const customer = useQuery({
    queryKey: ['customer', customerId],
    queryFn: () => api.get('/api/v1/customers/{customer_id}', { params: { customer_id: customerId } }),
  })
  if (!customer.data) return <Loading />
  const c = customer.data
  if (editing) return <CustomerEdit customer={c} onClose={() => setEditing(false)} />
  return (
    <section aria-labelledby="details-heading">
      <h2 id="details-heading">Details</h2>
      <dl className="facts">
        <div>
          <dt>Name</dt>
          <dd>{c.name}</dd>
        </div>
        <div>
          <dt>Mobile number</dt>
          <dd>{c.phone ?? '-'}</dd>
        </div>
        <div>
          <dt>Email</dt>
          <dd>{c.email ?? '-'}</dd>
        </div>
        <div>
          <dt>Address</dt>
          <dd>{c.address ?? '-'}</dd>
        </div>
        <div>
          <dt>Notes</dt>
          <dd>{c.notes ?? '-'}</dd>
        </div>
        <div>
          <dt>Added</dt>
          <dd>{formatDate(c.created_at)}</dd>
        </div>
      </dl>
      <button type="button" className="btn" onClick={() => setEditing(true)}>
        Edit details
      </button>
    </section>
  )
}

function CustomerJobs({ customerId }: { customerId: number }) {
  const [page, setPage] = useState(1)
  const jobs = useQuery({
    queryKey: ['jobs', { customer_id: customerId, page }],
    queryFn: () => api.get('/api/v1/jobs', { query: { customer_id: customerId, page, page_size: 20 } }),
  })
  if (jobs.isPending) return <Loading label="Loading jobs..." />
  if (jobs.isError) return <ErrorNotice error={jobs.error} onRetry={() => void jobs.refetch()} />
  if (jobs.data.items.length === 0) return <Empty>No jobs for this customer yet.</Empty>
  return (
    <>
      <JobsTable jobs={jobs.data.items} showCustomer={false} />
      <Pagination page={jobs.data.page} totalPages={jobs.data.total_pages} total={jobs.data.total} onPage={setPage} label="Job pages" />
    </>
  )
}

function CustomerDocuments({ customerId }: { customerId: number }) {
  const docs = useQuery({
    queryKey: ['customer', customerId, 'documents'],
    queryFn: () => api.get('/api/v1/customers/{customer_id}/documents', { params: { customer_id: customerId } }),
  })
  return (
    <div>
      <UploadDocument customerId={customerId} />
      <h2>Documents</h2>
      {docs.isPending && <Loading label="Loading documents..." />}
      {docs.isError && <ErrorNotice error={docs.error} onRetry={() => void docs.refetch()} />}
      {docs.data && docs.data.length === 0 && <Empty>No documents uploaded yet.</Empty>}
      {docs.data && docs.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Documents</caption>
            <thead>
              <tr>
                <th scope="col">Type</th>
                <th scope="col">File</th>
                <th scope="col">Uploaded</th>
                <th scope="col">Jobs</th>
                <th scope="col">Verification</th>
              </tr>
            </thead>
            <tbody>
              {docs.data.map((d) => (
                <tr key={d.id}>
                  <td>{d.document_type?.name ?? 'Not set'}</td>
                  <td>{d.filename}</td>
                  <td>{formatDate(d.created_at)}</td>
                  <td>
                    {d.job_ids.length === 0
                      ? '-'
                      : d.job_ids.map((jid, i) => (
                          <span key={jid}>
                            {i > 0 && ', '}
                            <Link to={`/jobs/${jid}`}>Job {jid}</Link>
                          </span>
                        ))}
                  </td>
                  <td>
                    <span className="status-text">{documentStatusLabel(d.status)}</span>
                    {d.archived_at && <span className="hint"> (archived)</span>}
                    {d.status === 'needs_review' && (
                      <>
                        {' '}
                        <Link to={reviewPath(d.id)}>
                          Review<span className="visually-hidden"> {d.filename}</span>
                        </Link>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function UploadDocument({ customerId }: { customerId: number }) {
  const qc = useQueryClient()
  const types = useQuery({
    queryKey: ['document-types'],
    queryFn: () => api.get('/api/v1/document-types', { query: { active: true } }),
  })
  const [typeId, setTypeId] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [done, setDone] = useState('')
  const input = useRef<HTMLInputElement>(null)

  const onFile = async (file: File | undefined) => {
    if (!file) return
    setDone('')
    const problem = validateUpload(file)
    if (problem) {
      setError(problem)
      return
    }
    setError('')
    setBusy(file.name)
    try {
      await uploadDocument(file, { customerId, documentTypeId: typeId ? Number(typeId) : undefined })
      setDone(`${file.name} was uploaded. It will be read automatically.`)
      void qc.invalidateQueries({ queryKey: ['customer', customerId, 'documents'] })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')
    } finally {
      setBusy('')
      if (input.current) input.current.value = ''
    }
  }

  return (
    <section className="card section" aria-labelledby="upload-heading">
      <h2 id="upload-heading">Upload a document</h2>
      <Field label="Document type (optional)" hint="Leave blank if you are not sure.">
        {(p) => (
          <select {...p} value={typeId} onChange={(e) => setTypeId(e.target.value)}>
            <option value="">Not sure</option>
            {(types.data ?? []).map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="File" hint="JPEG, PNG or PDF, up to 10 MB.">
        {(p) => (
          <input {...p} ref={input} type="file" accept={ACCEPT} disabled={busy !== ''} onChange={(e) => void onFile(e.currentTarget.files?.[0])} />
        )}
      </Field>
      {busy && (
        <p role="status" className="hint">
          Uploading {busy}...
        </p>
      )}
      {done && (
        <p role="status" className="notice">
          {done}
        </p>
      )}
      {error && (
        <p role="alert" className="field-error">
          {error}
        </p>
      )}
    </section>
  )
}

function CustomerVehicles({ customerId }: { customerId: number }) {
  const vehicles = useQuery({
    queryKey: ['vehicles', { customer_id: customerId }],
    queryFn: () => api.get('/api/v1/vehicles', { query: { customer_id: customerId, page_size: 100 } }),
  })
  if (vehicles.isPending) return <Loading label="Loading vehicles..." />
  if (vehicles.isError) return <ErrorNotice error={vehicles.error} onRetry={() => void vehicles.refetch()} />
  if (vehicles.data.items.length === 0) return <Empty>No vehicles recorded for this customer.</Empty>
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">Vehicles</caption>
        <thead>
          <tr>
            <th scope="col">Registration</th>
            <th scope="col">Owner</th>
            <th scope="col">Expiries</th>
          </tr>
        </thead>
        <tbody>
          {vehicles.data.items.map((v) => (
            <tr key={v.id}>
              <td>{v.registration_number}</td>
              <td>{v.owner_name ?? '-'}</td>
              <td>
                {v.expiries.filter((e) => e.valid_until).length === 0
                  ? '-'
                  : v.expiries
                      .filter((e) => e.valid_until)
                      .map((e) => `${e.expiry_type}: ${formatDate(e.valid_until)}`)
                      .join(', ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CustomerReceipts({ customerId }: { customerId: number }) {
  const [page, setPage] = useState(1)
  const receipts = useQuery({
    queryKey: ['receipts', { customer_id: customerId, page }],
    queryFn: () => api.get('/api/v1/receipts', { query: { customer_id: customerId, page, page_size: 20 } }),
  })
  if (receipts.isPending) return <Loading label="Loading receipts..." />
  if (receipts.isError) return <ErrorNotice error={receipts.error} onRetry={() => void receipts.refetch()} />
  if (receipts.data.items.length === 0) return <Empty>No receipts for this customer yet.</Empty>
  return (
    <>
      <div className="table-wrap">
        <table>
          <caption className="visually-hidden">Receipts</caption>
          <thead>
            <tr>
              <th scope="col">Receipt</th>
              <th scope="col">Date</th>
              <th scope="col">Job</th>
              <th scope="col" className="num">
                Amount
              </th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {receipts.data.items.map((r) => (
              <tr key={r.id}>
                <td>{r.receipt_number}</td>
                <td>{formatDate(r.issued_at)}</td>
                <td>
                  <Link to={`/jobs/${r.job_id}`}>{r.job_number}</Link>
                </td>
                <td className="num">{formatMoney(r.amount)}</td>
                <td>{r.status === 'cancelled' ? 'Cancelled' : 'Issued'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={receipts.data.page} totalPages={receipts.data.total_pages} total={receipts.data.total} onPage={setPage} label="Receipt pages" />
    </>
  )
}

function describe(details: Record<string, unknown> | null): string {
  if (!details) return ''
  return Object.entries(details)
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join('; ')
    .slice(0, 300)
}

function CustomerActivity({ customerId }: { customerId: number }) {
  const [page, setPage] = useState(1)
  const log = useQuery({
    queryKey: ['customer', customerId, 'activity', page],
    queryFn: () =>
      api.get('/api/v1/audit-logs', { query: { entity_type: 'customer', entity_id: customerId, page, page_size: 20 } }),
  })
  if (log.isPending) return <Loading label="Loading activity..." />
  if (log.isError) return <ErrorNotice error={log.error} onRetry={() => void log.refetch()} />
  if (log.data.items.length === 0) return <Empty>No recorded activity.</Empty>
  return (
    <>
      <div className="table-wrap">
        <table>
          <caption className="visually-hidden">Customer activity</caption>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Action</th>
              <th scope="col">By</th>
              <th scope="col">Details</th>
            </tr>
          </thead>
          <tbody>
            {log.data.items.map((e) => (
              <tr key={e.id}>
                <td>{formatDateTime(e.created_at)}</td>
                <td>{e.action.replace(/_/g, ' ')}</td>
                <td>{e.user?.name ?? 'System'}</td>
                <td>{describe(e.details)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={log.data.page} totalPages={log.data.total_pages} total={log.data.total} onPage={setPage} label="Activity pages" />
    </>
  )
}
