import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import type { Customer } from '../lib/types'
import { customerSchema, type CustomerFormValues } from '../lib/customerSchema'
import { Field } from './Field'

const LABELS: Record<keyof CustomerFormValues, string> = {
  name: 'Name',
  phone: 'Mobile number',
  email: 'Email',
  address: 'Address',
  notes: 'Notes',
}
const KEYS = Object.keys(LABELS) as (keyof CustomerFormValues)[]

const norm = (v: string | null | undefined) => (v ?? '').trim()

function valuesOf(c: Customer): CustomerFormValues {
  return { name: c.name, phone: c.phone ?? '', email: c.email ?? '', address: c.address ?? '', notes: c.notes ?? '' }
}

interface FieldChange {
  key: keyof CustomerFormValues
  label: string
  from: string
  to: string
}

function changesBetween(customer: Customer, next: CustomerFormValues): FieldChange[] {
  const before = valuesOf(customer)
  return KEYS.filter((k) => norm(before[k]) !== norm(next[k])).map((k) => ({
    key: k,
    label: LABELS[k],
    from: norm(before[k]),
    to: norm(next[k]),
  }))
}

/** Edit details. Nothing is saved until the person has seen what will change and confirmed. */
export function CustomerEdit({ customer, onClose }: { customer: Customer; onClose: () => void }) {
  const qc = useQueryClient()
  const [pending, setPending] = useState<FieldChange[] | null>(null)
  const [noChange, setNoChange] = useState(false)
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<CustomerFormValues>({ resolver: zodResolver(customerSchema), defaultValues: valuesOf(customer) })

  const save = useMutation({
    mutationFn: (changes: FieldChange[]) => {
      const body: Record<string, string | null> = {}
      for (const c of changes) body[c.key] = c.to === '' ? null : c.to
      return api.patch('/api/v1/customers/{customer_id}', { params: { customer_id: customer.id }, body })
    },
    onSuccess: (data) => {
      qc.setQueryData(['customer', customer.id], data)
      void qc.invalidateQueries({ queryKey: ['customers'] })
      void qc.invalidateQueries({ queryKey: ['customer', customer.id, 'activity'] })
      onClose()
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        for (const [field, message] of Object.entries(err.fieldErrors)) {
          if (field in LABELS) setError(field as keyof CustomerFormValues, { message })
        }
        if (Object.keys(err.fieldErrors).length > 0) setPending(null)
      }
    },
  })

  const review = (values: CustomerFormValues) => {
    const changes = changesBetween(customer, values)
    setNoChange(changes.length === 0)
    setPending(changes.length ? changes : null)
  }

  if (pending) {
    return (
      <section aria-labelledby="confirm-heading" className="notice">
        <h3 id="confirm-heading">Confirm these changes</h3>
        <ul className="plain-list">
          {pending.map((c) => (
            <li key={c.key}>
              <strong>{c.label}:</strong> {c.from || '(empty)'} to {c.to || '(empty)'}
            </li>
          ))}
        </ul>
        {save.isError && (
          <div role="alert" className="notice notice-error">
            {save.error instanceof ApiError ? save.error.message : 'Something went wrong. Please try again.'}
          </div>
        )}
        <div className="actions">
          <button type="button" className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate(pending)}>
            {save.isPending ? 'Saving...' : 'Confirm and save'}
          </button>
          <button type="button" className="btn" onClick={() => setPending(null)}>
            Back to editing
          </button>
        </div>
      </section>
    )
  }

  return (
    <form onSubmit={(e) => void handleSubmit(review)(e)} noValidate aria-label="Edit customer">
      {noChange && (
        <div role="status" className="notice">
          Nothing has changed.
        </div>
      )}
      <Field label="Name" error={errors.name?.message}>
        {(p) => <input {...p} autoComplete="off" {...register('name', { onChange: () => setNoChange(false) })} />}
      </Field>
      <Field label="Mobile number" error={errors.phone?.message}>
        {(p) => <input {...p} type="tel" autoComplete="off" {...register('phone')} />}
      </Field>
      <Field label="Email" error={errors.email?.message}>
        {(p) => <input {...p} type="email" autoComplete="off" {...register('email')} />}
      </Field>
      <Field label="Address" error={errors.address?.message}>
        {(p) => <textarea {...p} rows={2} {...register('address')} />}
      </Field>
      <Field label="Notes" error={errors.notes?.message}>
        {(p) => <textarea {...p} rows={2} {...register('notes')} />}
      </Field>
      <div className="actions">
        <button type="submit" className="btn btn-primary">
          Review changes
        </button>
        <button type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  )
}
