import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { formatMoney } from '../lib/format'
import type { Service } from '../lib/types'
import { Field } from './Field'
import { ErrorNotice, Loading } from './Status'

/** Search by name, then choose from the list grouped by category. */
export function ServicePicker({
  value,
  onChange,
  disabled,
}: {
  value: Service | null
  onChange: (service: Service | null) => void
  disabled?: boolean
}) {
  const [filter, setFilter] = useState('')
  const services = useQuery({
    queryKey: ['services', 'active'],
    queryFn: () => api.get('/api/v1/services', { query: { active: true } }),
    staleTime: 5 * 60 * 1000,
  })
  const categories = useQuery({
    queryKey: ['service-categories', 'active'],
    queryFn: () => api.get('/api/v1/service-categories', { query: { active: true } }),
    staleTime: 5 * 60 * 1000,
  })
  const requirements = useQuery({
    queryKey: ['service', value?.id, 'requirements'],
    queryFn: () =>
      api.get('/api/v1/services/{service_id}/document-requirements', {
        params: { service_id: value?.id ?? 0 },
        query: { active: true },
      }),
    enabled: value !== null,
  })

  if (services.isPending) return <Loading label="Loading services..." />
  if (services.isError) return <ErrorNotice error={services.error} onRetry={() => void services.refetch()} />

  const needle = filter.trim().toLowerCase()
  const matching = services.data.filter(
    (s) => !needle || s.name.toLowerCase().includes(needle) || s.category.name.toLowerCase().includes(needle),
  )
  const order = new Map((categories.data ?? []).map((c, i) => [c.id, i]))
  const groups = new Map<number, { name: string; items: Service[] }>()
  for (const s of matching) {
    const g = groups.get(s.category.id) ?? { name: s.category.name, items: [] }
    g.items.push(s)
    groups.set(s.category.id, g)
  }
  const grouped = [...groups.entries()].sort((a, b) => (order.get(a[0]) ?? 999) - (order.get(b[0]) ?? 999))

  return (
    <div>
      <Field label="Search services">
        {(p) => <input {...p} type="search" value={filter} onChange={(e) => setFilter(e.target.value)} disabled={disabled} autoComplete="off" />}
      </Field>
      <Field label="Service" hint={matching.length === 0 ? 'No service matches your search.' : undefined}>
        {(p) => (
          <select
            {...p}
            value={value?.id ?? ''}
            disabled={disabled}
            onChange={(e) => onChange(services.data.find((s) => s.id === Number(e.target.value)) ?? null)}
          >
            <option value="">Choose a service</option>
            {grouped.map(([id, g]) => (
              <optgroup key={id} label={g.name}>
                {g.items
                  .sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
              </optgroup>
            ))}
          </select>
        )}
      </Field>
      {value && (
        <div className="notice" aria-live="polite">
          <p style={{ margin: 0 }}>
            <strong>{value.name}</strong>:{' '}
            {value.fee_type === 'fixed' && value.default_service_fee
              ? `service fee ${formatMoney(value.default_service_fee)}`
              : 'service fee decided per job'}
            {value.requires_vehicle ? '. Needs a vehicle.' : '.'}
          </p>
          {value.description && <p style={{ margin: '4px 0 0' }}>{value.description}</p>}
          {requirements.data && (
            <p style={{ margin: '4px 0 0' }}>
              {requirements.data.length === 0
                ? 'No documents needed.'
                : `Documents: ${requirements.data
                    .map((r) => r.document_type.name + (r.is_required ? '' : ' (optional)'))
                    .join(', ')}.`}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
