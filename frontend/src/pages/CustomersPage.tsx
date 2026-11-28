import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { formatDate } from '../lib/format'
import { debounceConfig, useDebounced } from '../lib/hooks'
import { CustomerForm } from '../components/CustomerForm'
import { Field } from '../components/Field'
import { Pagination } from '../components/Pagination'
import { Empty, ErrorNotice, Loading } from '../components/Status'

const PAGE_SIZE = 20

export function CustomersPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const urlSearch = params.get('search') ?? ''
  const page = Math.max(1, Number(params.get('page')) || 1)
  const [text, setText] = useState(urlSearch)
  const debounced = useDebounced(text.trim(), debounceConfig.ms)
  const [creating, setCreating] = useState(false)

  // The URL follows what was typed, so a search can be shared or reloaded.
  useEffect(() => {
    if (debounced === urlSearch) return
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (debounced) next.set('search', debounced)
        else next.delete('search')
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [debounced, urlSearch, setParams])

  const list = useQuery({
    queryKey: ['customers', { search: urlSearch, page }],
    queryFn: () =>
      api.get('/api/v1/customers', { query: { search: urlSearch || undefined, page, page_size: PAGE_SIZE } }),
    placeholderData: keepPreviousData,
  })

  const setPage = (p: number) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('page', String(p))
      return next
    })

  return (
    <div>
      <div className="page-head">
        <h1>Customers</h1>
        {!creating && (
          <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
            New customer
          </button>
        )}
      </div>

      {creating && (
        <section className="card section" aria-labelledby="new-customer-heading">
          <h2 id="new-customer-heading">New customer</h2>
          <CustomerForm
            onDone={(c) => navigate(`/customers/${c.id}`)}
            onUseExisting={(d) => navigate(`/customers/${d.id}`)}
            onCancel={() => setCreating(false)}
          />
        </section>
      )}

      <search className="section">
        <Field label="Search customers" hint="Name, mobile number (at least 4 digits), customer ID or vehicle number.">
          {(p) => (
            <input {...p} type="search" value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" />
          )}
        </Field>
      </search>

      {list.isPending && <Loading label="Loading customers..." />}
      {list.isError && <ErrorNotice error={list.error} onRetry={() => void list.refetch()} />}
      {list.data && list.data.items.length === 0 && (
        <Empty>{urlSearch ? `No customers match "${urlSearch}".` : 'No customers yet. Use "New customer" to add one.'}</Empty>
      )}
      {list.data && list.data.items.length > 0 && (
        <>
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Customers</caption>
              <thead>
                <tr>
                  <th scope="col">Customer ID</th>
                  <th scope="col">Name</th>
                  <th scope="col">Mobile</th>
                  <th scope="col">Added</th>
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((c) => (
                  <tr key={c.id}>
                    <td>{c.id}</td>
                    <td>
                      <Link to={`/customers/${c.id}`}>{c.name}</Link>
                    </td>
                    <td>{c.phone_masked ?? '-'}</td>
                    <td>{formatDate(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={list.data.page} totalPages={list.data.total_pages} total={list.data.total} onPage={setPage} label="Customer pages" />
        </>
      )}
    </div>
  )
}
