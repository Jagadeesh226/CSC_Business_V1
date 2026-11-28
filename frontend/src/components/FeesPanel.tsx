import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import { formatMoney } from '../lib/format'
import type { FeeRead, FeeType } from '../lib/types'
import { Field } from './Field'
import { Empty, ErrorNotice, Loading } from './Status'

const FEE_TYPE_LABELS: Record<FeeType, string> = {
  service: 'Service fee',
  third_party: 'Third-party fee',
  department: 'Department fee',
}
const FEE_TYPES = Object.keys(FEE_TYPE_LABELS) as FeeType[]
const AMOUNT = /^\d{1,9}(\.\d{1,2})?$/

function validate(description: string, amount: string): { description?: string; amount?: string } {
  const errors: { description?: string; amount?: string } = {}
  if (!description.trim()) errors.description = 'Enter what the fee is for.'
  else if (description.trim().length > 500) errors.description = 'Use 500 characters or fewer.'
  if (!AMOUNT.test(amount.trim()) || Number(amount) <= 0) {
    errors.amount = 'Enter an amount in rupees greater than zero, for example 150 or 150.50.'
  }
  return errors
}

const message = (err: unknown) => (err instanceof ApiError ? err.message : 'Something went wrong. Please try again.')

interface Props {
  jobId: number
  isAdmin: boolean
  /** Who may add: admins always; employees until a payment is received (and not on closed jobs). */
  canAdd: boolean
  addBlockedReason?: string
  onDirtyChange?: (dirty: boolean) => void
}

export function FeesPanel({ jobId, isAdmin, canAdd, addBlockedReason, onDirtyChange }: Props) {
  const qc = useQueryClient()
  const fees = useQuery({
    queryKey: ['job', jobId, 'fees'],
    queryFn: () => api.get('/api/v1/jobs/{job_id}/fees', { params: { job_id: jobId } }),
  })
  const [feeType, setFeeType] = useState<FeeType>('service')
  const [description, setDescription] = useState('')
  const [amount, setAmount] = useState('')
  const [shown, setShown] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)
  const [removing, setRemoving] = useState<number | null>(null)

  const dirty = description !== '' || amount !== ''
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  const changed = () => {
    void qc.invalidateQueries({ queryKey: ['job', jobId] })
    void qc.invalidateQueries({ queryKey: ['jobs'] })
  }

  const add = useMutation({
    mutationFn: () =>
      api.post('/api/v1/jobs/{job_id}/fees', {
        params: { job_id: jobId },
        body: { fee_type: feeType, description: description.trim(), amount: amount.trim() },
      }),
    onSuccess: () => {
      setDescription('')
      setAmount('')
      setShown(false)
      changed()
    },
  })
  const update = useMutation({
    mutationFn: (v: { id: number; fee_type: FeeType; description: string; amount: string }) =>
      api.patch('/api/v1/jobs/{job_id}/fees/{fee_id}', {
        params: { job_id: jobId, fee_id: v.id },
        body: { fee_type: v.fee_type, description: v.description, amount: v.amount },
      }),
    onSuccess: () => {
      setEditing(null)
      changed()
    },
  })
  const remove = useMutation({
    mutationFn: (id: number) => api.delete('/api/v1/jobs/{job_id}/fees/{fee_id}', { params: { job_id: jobId, fee_id: id } }),
    onSuccess: () => {
      setRemoving(null)
      changed()
    },
  })

  const [touched, setTouched] = useState(false)
  const errors = validate(description, amount)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (errors.description || errors.amount) return
    add.mutate()
  }

  if (fees.isPending) return <Loading label="Loading fees..." />
  if (fees.isError) return <ErrorNotice error={fees.error} onRetry={() => void fees.refetch()} />

  return (
    <div>
      {fees.data.items.length === 0 ? (
        <Empty>No fees added yet.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Fees</caption>
            <thead>
              <tr>
                <th scope="col">Type</th>
                <th scope="col">Description</th>
                <th scope="col" className="num">
                  Amount
                </th>
                {isAdmin && <th scope="col">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {fees.data.items.map((fee) =>
                editing === fee.id ? (
                  <FeeEditRow
                    key={fee.id}
                    fee={fee}
                    pending={update.isPending}
                    error={update.isError ? message(update.error) : undefined}
                    onCancel={() => setEditing(null)}
                    onSave={(v) => update.mutate({ id: fee.id, ...v })}
                  />
                ) : (
                  <tr key={fee.id}>
                    <td>{FEE_TYPE_LABELS[fee.fee_type]}</td>
                    <td>{fee.description}</td>
                    <td className="num">{formatMoney(fee.amount)}</td>
                    {isAdmin && (
                      <td>
                        {removing === fee.id ? (
                          <span className="inline">
                            <button type="button" className="btn" disabled={remove.isPending} onClick={() => remove.mutate(fee.id)}>
                              Confirm remove<span className="visually-hidden"> {fee.description}</span>
                            </button>
                            <button type="button" className="btn" onClick={() => setRemoving(null)}>
                              Keep
                            </button>
                          </span>
                        ) : (
                          <span className="inline">
                            <button
                              type="button"
                              className="btn"
                              onClick={() => {
                                update.reset()
                                setEditing(fee.id)
                              }}
                            >
                              Edit<span className="visually-hidden"> {fee.description}</span>
                            </button>
                            <button
                              type="button"
                              className="btn"
                              onClick={() => {
                                remove.reset()
                                setRemoving(fee.id)
                              }}
                            >
                              Remove<span className="visually-hidden"> {fee.description}</span>
                            </button>
                          </span>
                        )}
                      </td>
                    )}
                  </tr>
                ),
              )}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row" colSpan={2}>
                  Total
                </th>
                <td className="num">
                  <strong>{formatMoney(fees.data.totals.total)}</strong>
                </td>
                {isAdmin && <td />}
              </tr>
            </tfoot>
          </table>
        </div>
      )}
      {remove.isError && (
        <p role="alert" className="field-error">
          {message(remove.error)}
        </p>
      )}

      {canAdd ? (
        shown || dirty ? (
          <form onSubmit={submit} noValidate aria-label="Add fee" className="card section">
            {add.isError && (
              <div role="alert" className="notice notice-error">
                {message(add.error)}
              </div>
            )}
            <Field label="Fee type">
              {(p) => (
                <select {...p} value={feeType} onChange={(e) => setFeeType(e.target.value as FeeType)}>
                  {FEE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {FEE_TYPE_LABELS[t]}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field label="Description" error={touched ? errors.description : undefined}>
              {(p) => <input {...p} value={description} onChange={(e) => setDescription(e.target.value)} autoComplete="off" />}
            </Field>
            <Field label="Amount (₹)" error={touched ? errors.amount : undefined}>
              {(p) => <input {...p} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} autoComplete="off" />}
            </Field>
            <div className="actions">
              <button type="submit" className="btn btn-primary" disabled={add.isPending}>
                {add.isPending ? 'Adding...' : 'Add fee'}
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setDescription('')
                  setAmount('')
                  setTouched(false)
                  setShown(false)
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <button type="button" className="btn" onClick={() => setShown(true)}>
            Add fee
          </button>
        )
      ) : (
        addBlockedReason && <p className="hint">{addBlockedReason}</p>
      )}
    </div>
  )
}

function FeeEditRow({
  fee,
  pending,
  error,
  onSave,
  onCancel,
}: {
  fee: FeeRead
  pending: boolean
  error?: string
  onSave: (v: { fee_type: FeeType; description: string; amount: string }) => void
  onCancel: () => void
}) {
  const [feeType, setFeeType] = useState<FeeType>(fee.fee_type)
  const [description, setDescription] = useState(fee.description)
  const [amount, setAmount] = useState(String(Number(fee.amount)))
  const [touched, setTouched] = useState(false)
  const errors = validate(description, amount)
  return (
    <tr>
      <td>
        <select aria-label="Fee type" value={feeType} onChange={(e) => setFeeType(e.target.value as FeeType)}>
          {FEE_TYPES.map((t) => (
            <option key={t} value={t}>
              {FEE_TYPE_LABELS[t]}
            </option>
          ))}
        </select>
      </td>
      <td>
        <input aria-label="Description" value={description} onChange={(e) => setDescription(e.target.value)} aria-invalid={touched && errors.description ? true : undefined} />
        {touched && errors.description && <span className="field-error" role="alert">{errors.description}</span>}
      </td>
      <td className="num">
        <input aria-label="Amount (₹)" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} aria-invalid={touched && errors.amount ? true : undefined} />
        {touched && errors.amount && <span className="field-error" role="alert">{errors.amount}</span>}
      </td>
      <td>
        <span className="inline">
          <button
            type="button"
            className="btn btn-primary"
            disabled={pending}
            onClick={() => {
              setTouched(true)
              if (errors.description || errors.amount) return
              onSave({ fee_type: feeType, description: description.trim(), amount: amount.trim() })
            }}
          >
            Save
          </button>
          <button type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
        </span>
        {error && <span className="field-error" role="alert">{error}</span>}
      </td>
    </tr>
  )
}
