import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { statusLabel } from '../lib/format'
import { ErrorNotice } from './Status'

interface Item {
  id: string
  group: 'Customers' | 'Jobs' | 'Vehicles'
  label: string
  detail: string
  to: string
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

const MIN_CHARS = 2
const GROUPS: Item['group'][] = ['Customers', 'Jobs', 'Vehicles']

/** Search dialog. Mount it only while open: focus is taken on mount and restored on unmount. */
export function CommandPalette({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const listId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const [term, setTerm] = useState('')
  const [active, setActive] = useState(0)
  const search = useDebounced(term.trim(), 250)
  const enabled = search.length >= MIN_CHARS

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    inputRef.current?.focus()
    return () => previous?.focus?.()
  }, [])

  const query = { search, page_size: 5 }
  const customers = useQuery({
    queryKey: ['palette', 'customers', search],
    queryFn: () => api.get('/api/v1/customers', { query }),
    enabled,
  })
  const jobs = useQuery({
    queryKey: ['palette', 'jobs', search],
    queryFn: () => api.get('/api/v1/jobs', { query }),
    enabled,
  })
  const vehicles = useQuery({
    queryKey: ['palette', 'vehicles', search],
    queryFn: () => api.get('/api/v1/vehicles', { query }),
    enabled,
  })

  const items: Item[] = enabled
    ? [
        ...(customers.data?.items ?? []).map(
          (c): Item => ({
            id: `customer-${c.id}`,
            group: 'Customers',
            label: c.name,
            detail: c.phone_masked ?? 'No phone number',
            to: `/customers/${c.id}`,
          }),
        ),
        ...(jobs.data?.items ?? []).map(
          (j): Item => ({
            id: `job-${j.id}`,
            group: 'Jobs',
            label: `${j.job_number} - ${j.title}`,
            detail: `${j.customer.name} · ${statusLabel(j.status)}`,
            to: `/jobs/${j.id}`,
          }),
        ),
        ...(vehicles.data?.items ?? []).map(
          (v): Item => ({
            id: `vehicle-${v.id}`,
            group: 'Vehicles',
            label: v.registration_number,
            detail: [v.owner_name, v.customer.name].filter(Boolean).join(' · '),
            to: `/vehicles/${v.id}`,
          }),
        ),
      ]
    : []
  const activeIndex = items.length ? Math.min(active, items.length - 1) : 0
  const fetching = customers.isFetching || jobs.isFetching || vehicles.isFetching
  const error = customers.error ?? jobs.error ?? vehicles.error

  const activeId = items[activeIndex] ? `${listId}-${items[activeIndex].id}` : undefined
  useEffect(() => {
    if (activeId) document.getElementById(activeId)?.scrollIntoView?.({ block: 'nearest' })
  }, [activeId])

  const choose = (item: Item) => {
    onClose()
    navigate(item.to)
  }

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
    } else if (e.key === 'ArrowDown' && items.length) {
      e.preventDefault()
      setActive((activeIndex + 1) % items.length)
    } else if (e.key === 'ArrowUp' && items.length) {
      e.preventDefault()
      setActive((activeIndex - 1 + items.length) % items.length)
    } else if (e.key === 'Enter' && items[activeIndex]) {
      e.preventDefault()
      choose(items[activeIndex])
    } else if (e.key === 'Tab') {
      // The input is the only stop inside the dialog: keep focus trapped.
      e.preventDefault()
    }
  }

  let status = ''
  if (!enabled) status = `Type at least ${MIN_CHARS} characters to search customers, jobs and vehicles.`
  else if (fetching && items.length === 0) status = 'Searching...'
  else if (!error && items.length === 0) status = 'No matches found.'
  else if (items.length > 0) status = `${items.length} result${items.length === 1 ? '' : 's'}.`

  return (
    <div
      className="palette-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="palette" role="dialog" aria-modal="true" aria-label="Search" onKeyDown={onKeyDown}>
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-label="Search customers, jobs and vehicles"
          aria-expanded={items.length > 0}
          aria-controls={listId}
          aria-activedescendant={activeId}
          aria-autocomplete="list"
          autoComplete="off"
          placeholder="Search customers, jobs, vehicles..."
          value={term}
          onChange={(e) => {
            setTerm(e.target.value)
            setActive(0)
          }}
        />
        <div className="palette-results">
          <div role="status" className="palette-status">
            {status}
          </div>
          {error && !fetching && <ErrorNotice error={error} />}
          <div id={listId} role="listbox" aria-label="Search results">
            {GROUPS.map((group) => {
              const groupItems = items.filter((i) => i.group === group)
              if (groupItems.length === 0) return null
              return (
                <div key={group} role="group" aria-label={group}>
                  <div className="palette-group-title" aria-hidden="true">
                    {group}
                  </div>
                  {groupItems.map((item) => (
                    <div
                      key={item.id}
                      id={`${listId}-${item.id}`}
                      role="option"
                      aria-selected={items[activeIndex]?.id === item.id}
                      className="palette-option"
                      onMouseMove={() => setActive(items.indexOf(item))}
                      onClick={() => choose(item)}
                    >
                      <span>{item.label}</span>
                      <small>{item.detail}</small>
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
