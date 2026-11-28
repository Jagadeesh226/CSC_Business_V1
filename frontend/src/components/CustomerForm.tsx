import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import type { Customer, CustomerSummary } from '../lib/types'
import { customerSchema, type CustomerFormValues } from '../lib/customerSchema'
import { Field } from './Field'

const orNull = (v: string): string | null => (v.trim() === '' ? null : v.trim())

/**
 * New customer. The server answers with the saved customer and any similar
 * existing ones; similar customers are shown for the person to decide, never
 * merged automatically.
 */
export function CustomerForm({
  initialPhone = '',
  onDone,
  onUseExisting,
  onCancel,
}: {
  initialPhone?: string
  /** The new customer is the one to use. */
  onDone: (customer: Customer) => void
  /** An existing customer is the one to use instead. */
  onUseExisting: (customer: CustomerSummary) => void
  onCancel?: () => void
}) {
  const qc = useQueryClient()
  const [saved, setSaved] = useState<{ customer: Customer; duplicates: CustomerSummary[] } | null>(null)
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<CustomerFormValues>({
    resolver: zodResolver(customerSchema),
    defaultValues: { name: '', phone: initialPhone, email: '', address: '', notes: '' },
  })

  const create = useMutation({
    mutationFn: (v: CustomerFormValues) =>
      api.post('/api/v1/customers', {
        body: { name: v.name, phone: orNull(v.phone), email: orNull(v.email), address: orNull(v.address), notes: orNull(v.notes) },
      }),
    onSuccess: (data) => {
      void qc.invalidateQueries({ queryKey: ['customers'] })
      if (data.possible_duplicates.length === 0) onDone(data.customer)
      else setSaved({ customer: data.customer, duplicates: data.possible_duplicates })
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        for (const [field, message] of Object.entries(err.fieldErrors)) {
          if (field in customerSchema.shape) setError(field as keyof CustomerFormValues, { message })
        }
      }
    },
  })

  if (saved) {
    return (
      <section aria-labelledby="dup-heading" className="notice notice-warning">
        <h3 id="dup-heading">This customer may already exist</h3>
        <p>
          {saved.customer.name} was saved (customer ID {saved.customer.id}), but these customers look similar. Nothing has
          been merged.
        </p>
        <ul className="plain-list">
          {saved.duplicates.map((d) => (
            <li key={d.id} className="row-between">
              <span>
                {d.name} · {d.phone_masked ?? 'no phone'} · customer ID {d.id}
              </span>
              <button type="button" className="btn" onClick={() => onUseExisting(d)}>
                Use this customer instead<span className="visually-hidden">: {d.name}</span>
              </button>
            </li>
          ))}
        </ul>
        <button type="button" className="btn btn-primary" onClick={() => onDone(saved.customer)}>
          Keep the new customer
        </button>
      </section>
    )
  }

  return (
    <form onSubmit={(e) => void handleSubmit((v) => create.mutateAsync(v).catch(() => undefined))(e)} noValidate aria-label="New customer">
      {create.isError && !(create.error instanceof ApiError && Object.keys(create.error.fieldErrors).length > 0) && (
        <div role="alert" className="notice notice-error">
          {create.error instanceof ApiError ? create.error.message : 'Something went wrong. Please try again.'}
        </div>
      )}
      <Field label="Mobile number" error={errors.phone?.message}>
        {(p) => <input {...p} type="tel" inputMode="tel" autoComplete="off" {...register('phone')} />}
      </Field>
      <Field label="Name" error={errors.name?.message}>
        {(p) => <input {...p} autoComplete="off" {...register('name')} />}
      </Field>
      <Field label="Email (optional)" error={errors.email?.message}>
        {(p) => <input {...p} type="email" autoComplete="off" {...register('email')} />}
      </Field>
      <Field label="Address (optional)" error={errors.address?.message}>
        {(p) => <textarea {...p} rows={2} {...register('address')} />}
      </Field>
      <Field label="Notes (optional)" error={errors.notes?.message}>
        {(p) => <textarea {...p} rows={2} {...register('notes')} />}
      </Field>
      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={create.isPending}>
          {create.isPending ? 'Saving...' : 'Save customer'}
        </button>
        {onCancel && (
          <button type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  )
}
