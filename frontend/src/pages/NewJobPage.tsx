import { useCallback, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMe } from '../auth/useAuth'
import { ApiError, api } from '../lib/api'
import { formatDate, statusLabel } from '../lib/format'
import type { Job, Service } from '../lib/types'
import { AssigneesPanel } from '../components/AssigneesPanel'
import { ChecklistPanel } from '../components/ChecklistPanel'
import { CustomerPicker, type PickedCustomer } from '../components/CustomerPicker'
import { Field } from '../components/Field'
import { FeesPanel } from '../components/FeesPanel'
import { hasReceivedPayment, isClosed } from '../lib/jobs'
import { ServicePicker } from '../components/ServicePicker'
import { ErrorNotice, Loading } from '../components/Status'
import { UnsavedWarning } from '../components/UnsavedWarning'

export function NewJobPage() {
  // "Create another job" starts a fresh wizard.
  const [run, setRun] = useState(0)
  return <Wizard key={run} onAnother={() => setRun((n) => n + 1)} />
}

function Wizard({ onAnother }: { onAnother: () => void }) {
  const me = useMe()
  const isAdmin = me.data?.role === 'admin'
  const qc = useQueryClient()
  const [params] = useSearchParams()
  const urlCustomerId = Number(params.get('customer_id')) || 0

  const [service, setService] = useState<Service | null>(null)
  const [mobile, setMobile] = useState('')
  const [picked, setPicked] = useState<PickedCustomer | null | undefined>(undefined)
  const [titleText, setTitleText] = useState<string | null>(null) // null: follow the service name
  const [dueDate, setDueDate] = useState('')
  const [description, setDescription] = useState('')
  const [vehicleId, setVehicleId] = useState('')
  const [createdId, setCreatedId] = useState<number | null>(null)
  const [uploading, setUploading] = useState(false)
  const [feeDraft, setFeeDraft] = useState(false)
  const [touched, setTouched] = useState(false)
  const createdHeading = useRef<HTMLHeadingElement>(null)

  // A customer chosen in the URL is filled in without asking again.
  const fromUrl = useQuery({
    queryKey: ['customer', urlCustomerId],
    queryFn: () => api.get('/api/v1/customers/{customer_id}', { params: { customer_id: urlCustomerId } }),
    enabled: urlCustomerId > 0,
  })
  const urlCustomer: PickedCustomer | null = fromUrl.data
    ? { id: fromUrl.data.id, name: fromUrl.data.name, phone: fromUrl.data.phone }
    : null
  const customer = picked !== undefined ? picked : urlCustomer

  const title = titleText ?? service?.name ?? ''

  const vehicles = useQuery({
    queryKey: ['vehicles', { customer_id: customer?.id }],
    queryFn: () => api.get('/api/v1/vehicles', { query: { customer_id: customer?.id, page_size: 100 } }),
    enabled: service?.requires_vehicle === true && customer !== null,
  })

  const job = useQuery({
    queryKey: ['job', createdId],
    queryFn: () => api.get('/api/v1/jobs/{job_id}', { params: { job_id: createdId ?? 0 } }),
    enabled: createdId !== null,
    staleTime: 10_000,
  })

  const create = useMutation({
    mutationFn: () =>
      api.post('/api/v1/jobs', {
        body: {
          customer_id: customer?.id ?? 0,
          service_id: service?.id ?? 0,
          title: title.trim(),
          due_date: dueDate || null,
          description: description.trim() || null,
          vehicle_id: vehicleId ? Number(vehicleId) : null,
        },
      }),
    onSuccess: (data: Job) => {
      qc.setQueryData(['job', data.id], data)
      void qc.invalidateQueries({ queryKey: ['jobs'] })
      setCreatedId(data.id)
      setTimeout(() => createdHeading.current?.focus(), 0)
    },
  })

  const errors = {
    service: service ? undefined : 'Choose a service.',
    customer: customer ? undefined : 'Choose or create a customer.',
    title: title.trim() ? (title.trim().length > 200 ? 'Use 200 characters or fewer.' : undefined) : 'Enter a job title.',
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (errors.service || errors.customer || errors.title) return
    create.mutate()
  }

  const onBusy = useCallback((busy: boolean) => setUploading(busy), [])
  const onFeeDirty = useCallback((dirty: boolean) => setFeeDraft(dirty), [])

  const created = createdId !== null
  const beforeDirty =
    service !== null ||
    mobile.trim() !== '' ||
    titleText !== null ||
    dueDate !== '' ||
    description.trim() !== '' ||
    (customer !== null && customer.id !== urlCustomer?.id)
  const dirty = created ? uploading || feeDraft : beforeDirty

  const j = job.data
  return (
    <div>
      <UnsavedWarning
        dirty={dirty}
        message={
          created
            ? 'A file is still being sent or a fee has not been added. If you leave, it will be lost.'
            : 'The job has not been created yet. If you leave, what you entered will be lost.'
        }
      />
      <h1>New job</h1>

      {!created && (
        <form onSubmit={submit} noValidate aria-label="New job">
          <section className="card section" aria-labelledby="step-service">
            <h2 id="step-service">1. Service</h2>
            <ServicePicker value={service} onChange={setService} />
            {touched && errors.service && (
              <p role="alert" className="field-error">
                {errors.service}
              </p>
            )}
          </section>

          <section className="card section" aria-labelledby="step-customer">
            <h2 id="step-customer">2. Customer</h2>
            <CustomerPicker value={customer} onChange={setPicked} mobile={mobile} onMobileChange={setMobile} />
            {touched && errors.customer && (
              <p role="alert" className="field-error">
                {errors.customer}
              </p>
            )}
            {urlCustomerId > 0 && fromUrl.isError && <ErrorNotice error={fromUrl.error} onRetry={() => void fromUrl.refetch()} />}
          </section>

          <section className="card section" aria-labelledby="step-details">
            <h2 id="step-details">3. Job details</h2>
            <Field label="Job title" error={touched ? errors.title : undefined}>
              {(p) => <input {...p} value={title} onChange={(e) => setTitleText(e.target.value)} autoComplete="off" />}
            </Field>
            <Field label="Due date (optional)">
              {(p) => <input {...p} type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} />}
            </Field>
            <Field label="Notes (optional)">
              {(p) => <textarea {...p} rows={3} maxLength={5000} value={description} onChange={(e) => setDescription(e.target.value)} />}
            </Field>
            {service?.requires_vehicle && (
              <>
                {!customer && <p className="hint">Choose a customer to pick one of their vehicles.</p>}
                {customer && vehicles.isPending && <Loading label="Loading vehicles..." />}
                {customer && vehicles.isError && <ErrorNotice error={vehicles.error} onRetry={() => void vehicles.refetch()} />}
                {customer && vehicles.data && vehicles.data.items.length === 0 && (
                  <p className="notice notice-warning">
                    This service needs a vehicle, but {customer.name} has none recorded yet. Adding vehicles comes in a later step.
                  </p>
                )}
                {customer && vehicles.data && vehicles.data.items.length > 0 && (
                  <Field label="Vehicle">
                    {(p) => (
                      <select {...p} value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
                        <option value="">Choose a vehicle</option>
                        {vehicles.data.items.map((v) => (
                          <option key={v.id} value={v.id}>
                            {v.registration_number}
                            {v.owner_name ? ` (${v.owner_name})` : ''}
                          </option>
                        ))}
                      </select>
                    )}
                  </Field>
                )}
              </>
            )}
          </section>

          {create.isError && (
            <div role="alert" className="notice notice-error">
              {create.error instanceof ApiError ? create.error.message : 'Something went wrong. Please try again.'}
            </div>
          )}
          <div className="actions">
            <button type="submit" className="btn btn-primary" disabled={create.isPending}>
              {create.isPending ? 'Creating job...' : 'Create job'}
            </button>
          </div>
        </form>
      )}

      {created && job.isPending && <Loading label="Loading job..." />}
      {created && job.isError && <ErrorNotice error={job.error} onRetry={() => void job.refetch()} />}
      {created && j && (
        <>
          <section className="notice" aria-labelledby="created-heading" role="status">
            <h2 id="created-heading" tabIndex={-1} ref={createdHeading}>
              Job {j.job_number} created
            </h2>
            <p style={{ margin: 0 }}>
              {j.title} for {j.customer.name}. Status: {statusLabel(j.status)}.
              {j.due_date ? ` Due ${formatDate(j.due_date)}.` : ''}
            </p>
          </section>

          <section className="card section" aria-labelledby="step-docs">
            <h2 id="step-docs" tabIndex={-1}>4. Documents</h2>
            <ChecklistPanel jobId={j.id} customerId={j.customer.id} canChange={!isClosed(j) || isAdmin} onBusyChange={onBusy} />
          </section>

          <section className="card section" aria-labelledby="step-fees">
            <h2 id="step-fees">5. Fees</h2>
            <FeesPanel
              jobId={j.id}
              isAdmin={isAdmin}
              canAdd={isAdmin || !hasReceivedPayment(j)}
              addBlockedReason="A payment has been received, so only an admin can add fees now."
              onDirtyChange={onFeeDirty}
            />
          </section>

          <section className="card section" aria-labelledby="step-assign">
            <h2 id="step-assign">6. Assigned to</h2>
            <AssigneesPanel job={j} isAdmin={isAdmin} />
          </section>

          <section className="card section" aria-labelledby="step-next">
            <h2 id="step-next">What next?</h2>
            <div className="actions">
              <Link className="btn" to={`/customers/${j.customer.id}`}>
                Open customer
              </Link>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  const el = document.getElementById('step-docs')
                  el?.scrollIntoView?.()
                  el?.focus()
                }}
              >
                Add document
              </button>
              <Link className="btn" to={`/jobs/${j.id}`}>
                Update status
              </Link>
              <button type="button" className="btn" disabled aria-describedby="pay-note">
                Record payment
              </button>
              <button type="button" className="btn btn-primary" onClick={onAnother}>
                Create another job
              </button>
            </div>
            <p id="pay-note" className="hint">
              Recording payments comes in a later step.
            </p>
          </section>
        </>
      )}
    </div>
  )
}
