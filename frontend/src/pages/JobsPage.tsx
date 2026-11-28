import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useMe } from '../auth/useAuth'
import { api, type GetQuery } from '../lib/api'
import { statusLabel } from '../lib/format'
import { debounceConfig, useDebounced } from '../lib/hooks'
import type { JobStatus } from '../lib/types'
import { Field } from '../components/Field'
import { JobsTable } from '../components/JobsTable'
import { Pagination } from '../components/Pagination'
import { Empty, ErrorNotice, Loading } from '../components/Status'

type JobQuery = NonNullable<GetQuery<'/api/v1/jobs'>>

const STATUSES: JobStatus[] = ['pending', 'in_progress', 'waiting_for_documents', 'submitted', 'completed', 'cancelled']
const READINESS: [string, string][] = [
  ['missing_documents', 'Missing documents'],
  ['needs_review', 'Documents to review'],
  ['processing', 'Documents being read'],
  ['ready_to_submit', 'Ready to submit'],
  ['ready_to_resume', 'Ready to resume'],
]
const DUE: [string, string][] = [
  ['overdue', 'Overdue'],
  ['today', 'Due today'],
]
const PAYMENT: [string, string][] = [
  ['due', 'Balance due'],
  ['paid', 'Fully paid'],
  ['overpaid', 'Overpaid'],
]
const SORT: [string, string][] = [
  ['newest', 'Newest first'],
  ['urgency', 'Most urgent first'],
]
const PAGE_SIZE = 20

export function JobsPage() {
  const me = useMe()
  const isAdmin = me.data?.role === 'admin'
  const [params, setParams] = useSearchParams()
  const get = (k: string) => params.get(k) ?? ''
  const urlSearch = get('search')
  const [text, setText] = useState(urlSearch)
  const debounced = useDebounced(text.trim(), debounceConfig.ms)
  const page = Math.max(1, Number(params.get('page')) || 1)

  const update = (changes: Record<string, string>) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        for (const [k, v] of Object.entries(changes)) {
          if (v) next.set(k, v)
          else next.delete(k)
        }
        if (!('page' in changes)) next.delete('page')
        return next
      },
      { replace: true },
    )

  useEffect(() => {
    if (debounced !== urlSearch) update({ search: debounced })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced])

  const users = useQuery({
    queryKey: ['users', 'active'],
    queryFn: () => api.get('/api/v1/users', { query: { is_active: true, page_size: 100 } }),
    enabled: isAdmin,
  })

  const query: JobQuery = {
    page,
    page_size: PAGE_SIZE,
    search: urlSearch || undefined,
    status: (get('status') || undefined) as JobQuery['status'],
    due: (get('due') || undefined) as JobQuery['due'],
    readiness: (get('readiness') || undefined) as JobQuery['readiness'],
    payment: (get('payment') || undefined) as JobQuery['payment'],
    sort: (get('sort') || undefined) as JobQuery['sort'],
    ...(isAdmin
      ? { assigned_to: get('assigned_to') || undefined, unassigned: get('unassigned') === '1' ? true : undefined }
      : {}),
  }
  const list = useQuery({
    queryKey: ['jobs', query],
    queryFn: () => api.get('/api/v1/jobs', { query }),
    placeholderData: keepPreviousData,
    enabled: me.isSuccess,
  })

  const filtered = ['search', 'status', 'due', 'readiness', 'payment', 'assigned_to', 'unassigned'].some((k) => get(k))
  const select = (key: string, label: string, options: [string, string][]) => (
    <Field label={label}>
      {(p) => (
        <select {...p} value={get(key)} onChange={(e) => update({ [key]: e.target.value })}>
          <option value="">Any</option>
          {options.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
      )}
    </Field>
  )

  return (
    <div>
      <div className="page-head">
        <h1>Jobs</h1>
        <Link className="btn btn-primary" to="/jobs/new">
          New job
        </Link>
      </div>

      <form className="filters card section" aria-label="Filter jobs" onSubmit={(e) => e.preventDefault()}>
        <Field label="Search jobs" hint="Job number or title.">
          {(p) => <input {...p} type="search" value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" />}
        </Field>
        {select('status', 'Status', STATUSES.map((s) => [s, statusLabel(s)]))}
        {select('due', 'Due date', DUE)}
        {select('readiness', 'Documents', READINESS)}
        {select('payment', 'Payment', PAYMENT)}
        {isAdmin && (
          <>
            <Field label="Assigned to">
              {(p) => (
                <select {...p} value={get('assigned_to')} onChange={(e) => update({ assigned_to: e.target.value })}>
                  <option value="">Anyone</option>
                  <option value="me">Me</option>
                  {(users.data?.items ?? []).map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.name}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <div className="field">
              <label className="check">
                <input
                  type="checkbox"
                  checked={get('unassigned') === '1'}
                  onChange={(e) => update({ unassigned: e.target.checked ? '1' : '' })}
                />
                Only jobs nobody is assigned to
              </label>
            </div>
          </>
        )}
        {select('sort', 'Sort by', SORT)}
        {filtered && (
          <div className="field">
            <button
              type="button"
              className="btn"
              onClick={() => {
                setText('')
                setParams({}, { replace: true })
              }}
            >
              Clear filters
            </button>
          </div>
        )}
      </form>

      {list.isPending && <Loading label="Loading jobs..." />}
      {list.isError && <ErrorNotice error={list.error} onRetry={() => void list.refetch()} />}
      {list.data && list.data.items.length === 0 && (
        <Empty>{filtered ? 'No jobs match these filters.' : 'No jobs yet. Use "New job" to start one.'}</Empty>
      )}
      {list.data && list.data.items.length > 0 && (
        <>
          <JobsTable jobs={list.data.items} />
          <Pagination
            page={list.data.page}
            totalPages={list.data.total_pages}
            total={list.data.total}
            onPage={(p) => update({ page: String(p) })}
            label="Job pages"
          />
        </>
      )}
    </div>
  )
}
