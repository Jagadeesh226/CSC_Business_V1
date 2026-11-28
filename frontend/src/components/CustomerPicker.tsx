import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { debounceConfig, useDebounced } from '../lib/hooks'
import { CustomerForm } from './CustomerForm'
import { Field } from './Field'
import { ErrorNotice } from './Status'

export interface PickedCustomer {
  id: number
  name: string
  phone: string | null
}

/**
 * Mobile number first: matches appear as you type. Choose an existing
 * customer, or create one inline. Details of existing customers are never
 * changed from here.
 */
export function CustomerPicker({
  value,
  onChange,
  mobile,
  onMobileChange,
}: {
  value: PickedCustomer | null
  onChange: (customer: PickedCustomer | null) => void
  mobile: string
  onMobileChange: (mobile: string) => void
}) {
  const [creating, setCreating] = useState(false)
  const typed = useDebounced(mobile.trim(), debounceConfig.ms)
  const digits = typed.replace(/\D/g, '').length
  const searchable = digits >= 4 || (digits === 0 && typed.length >= 3)
  const matches = useQuery({
    queryKey: ['customers', { search: typed, page: 1, picker: true }],
    queryFn: () => api.get('/api/v1/customers', { query: { search: typed, page_size: 10 } }),
    enabled: searchable && value === null,
    // Keep the list on screen while the next search loads, so a click is not lost.
    placeholderData: keepPreviousData,
  })

  if (value) {
    return (
      <div className="notice" aria-live="polite">
        <p style={{ margin: 0 }}>
          <strong>{value.name}</strong> · {value.phone ?? 'no mobile number'} · customer ID {value.id}
        </p>
        <button type="button" className="btn" style={{ marginTop: 8 }} onClick={() => onChange(null)}>
          Change customer
        </button>
      </div>
    )
  }

  if (creating) {
    return (
      <div className="card">
        <h3>New customer</h3>
        <CustomerForm
          initialPhone={mobile}
          onDone={(c) => {
            setCreating(false)
            onChange({ id: c.id, name: c.name, phone: c.phone })
          }}
          onUseExisting={(d) => {
            setCreating(false)
            onChange({ id: d.id, name: d.name, phone: d.phone_masked })
          }}
          onCancel={() => setCreating(false)}
        />
      </div>
    )
  }

  return (
    <div>
      <Field label="Customer mobile number" hint="Type the mobile number. Existing customers appear below.">
        {(p) => (
          <input {...p} type="tel" inputMode="tel" autoComplete="off" value={mobile} onChange={(e) => onMobileChange(e.target.value)} />
        )}
      </Field>
      {searchable && matches.isPending && (
        <p role="status" className="hint">
          Looking for customers...
        </p>
      )}
      {matches.isError && <ErrorNotice error={matches.error} onRetry={() => void matches.refetch()} />}
      {matches.data && matches.data.items.length > 0 && (
        <ul className="plain-list" aria-label="Matching customers">
          {matches.data.items.map((c) => (
            <li key={c.id} className="row-between">
              <span>
                {c.name} · {c.phone_masked ?? 'no phone'} · customer ID {c.id}
              </span>
              <button type="button" className="btn" onClick={() => onChange({ id: c.id, name: c.name, phone: c.phone_masked })}>
                Use existing customer<span className="visually-hidden">: {c.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {matches.data && matches.data.items.length === 0 && (
        <p role="status" className="hint">
          No existing customer matches.
        </p>
      )}
      <button type="button" className="btn" onClick={() => setCreating(true)}>
        Create new customer
      </button>
    </div>
  )
}
