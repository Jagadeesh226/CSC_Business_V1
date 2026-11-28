import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { adminUser, employeeUser, jsonResponse, renderApp, stubFetch } from '../test/utils'
import { makeChecklist, makeJob, page } from '../test/fixtures'
import { debounceConfig } from '../lib/hooks'

const user = userEvent.setup({ applyAccept: false })

beforeEach(() => {
  debounceConfig.ms = 5
})
afterEach(() => vi.unstubAllGlobals())

describe('jobs page', () => {
  function listRoutes(me: object) {
    const queries: URLSearchParams[] = []
    const routes = {
      'GET /api/v1/auth/me': me,
      'GET /api/v1/users': page([employeeUser, adminUser]),
      'GET /api/v1/jobs': (url: URL) => {
        queries.push(url.searchParams)
        return jsonResponse(200, page([makeJob({ payment_summary: { fees_total: '1500.00', paid: '500.00', balance: '1000.00', state: 'partial' } })]))
      },
    }
    return { routes, queries }
  }

  it('shows the job table with status as text, due date, documents, payment and assignees', async () => {
    const { routes } = listRoutes(adminUser)
    stubFetch(routes)
    renderApp('/jobs')
    const row = await screen.findByRole('row', { name: /J-0021/ })
    expect(within(row).getByText('In progress')).toBeTruthy()
    expect(within(row).getByText('05-12-2026')).toBeTruthy()
    expect(within(row).getByText(/0 of 1 verified, 1 missing/)).toBeTruthy()
    expect(within(row).getByText(/Part paid/)).toBeTruthy()
    expect(within(row).getByText(/₹1,000\.00/)).toBeTruthy()
    expect(within(row).getByText('Ravi Employee')).toBeTruthy()
    expect(within(row).getByRole('link', { name: 'Ravi Kumar' }).getAttribute('href')).toBe('/customers/7')
  })

  it('keeps filters in the URL and sends them to the server', async () => {
    const { routes, queries } = listRoutes(adminUser)
    stubFetch(routes)
    const { router } = renderApp('/jobs')
    await screen.findByRole('row', { name: /J-0021/ })

    await user.selectOptions(screen.getByLabelText('Status'), 'submitted')
    await user.selectOptions(screen.getByLabelText('Payment'), 'due')
    await user.selectOptions(screen.getByLabelText('Due date'), 'overdue')
    await user.selectOptions(screen.getByLabelText('Documents'), 'ready_to_submit')
    await user.selectOptions(screen.getByLabelText('Sort by'), 'urgency')
    await user.click(screen.getByLabelText('Only jobs nobody is assigned to'))
    await user.type(screen.getByLabelText('Search jobs'), 'RC')

    await waitFor(() => {
      const search = new URLSearchParams(router.state.location.search)
      expect(Object.fromEntries(search)).toEqual({
        status: 'submitted',
        payment: 'due',
        due: 'overdue',
        readiness: 'ready_to_submit',
        sort: 'urgency',
        unassigned: '1',
        search: 'RC',
      })
    })
    await waitFor(() => {
      const last = queries[queries.length - 1]
      expect(last.get('status')).toBe('submitted')
      expect(last.get('unassigned')).toBe('true')
      expect(last.get('search')).toBe('RC')
    })
  })

  it('restores filters from the URL', async () => {
    const { routes, queries } = listRoutes(adminUser)
    stubFetch(routes)
    renderApp('/jobs?status=waiting_for_documents&assigned_to=me&page=2')
    await screen.findByRole('row', { name: /J-0021/ })
    expect((screen.getByLabelText('Status') as HTMLSelectElement).value).toBe('waiting_for_documents')
    expect((screen.getByLabelText('Assigned to') as HTMLSelectElement).value).toBe('me')
    expect(queries[0].get('status')).toBe('waiting_for_documents')
    expect(queries[0].get('assigned_to')).toBe('me')
    expect(queries[0].get('page')).toBe('2')
  })

  it('does not offer admin-only filters to employees', async () => {
    const { routes } = listRoutes(employeeUser)
    stubFetch(routes)
    renderApp('/jobs')
    await screen.findByRole('row', { name: /J-0021/ })
    expect(screen.queryByLabelText('Assigned to')).toBeNull()
    expect(screen.queryByLabelText('Only jobs nobody is assigned to')).toBeNull()
  })

  it('shows an empty state for filters that match nothing and can clear them', async () => {
    stubFetch({ 'GET /api/v1/auth/me': adminUser, 'GET /api/v1/users': page([]), 'GET /api/v1/jobs': page([]) })
    const { router } = renderApp('/jobs?status=cancelled')
    expect(await screen.findByText('No jobs match these filters.')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(router.state.location.search).toBe('')
    expect(await screen.findByText(/No jobs yet/)).toBeTruthy()
  })
})

describe('job detail', () => {
  function detailRoutes(me: object, job = makeJob(), extra: Record<string, unknown> = {}) {
    return {
      'GET /api/v1/auth/me': me,
      'GET /api/v1/jobs/21': job,
      'GET /api/v1/jobs/21/checklist': makeChecklist(),
      'GET /api/v1/customers/7/documents': [],
      'GET /api/v1/jobs/21/fees': {
        items: [{ id: 5, fee_type: 'service', description: 'Service charge', amount: '250.00', created_by: null, created_at: '2026-11-28T00:00:00Z', deleted_by: null, deleted_at: null }],
        totals: { service: '250.00', third_party: '0.00', department: '0.00', total: '250.00' },
      },
      'GET /api/v1/jobs/21/status-history': [
        { id: 1, old_status: null, new_status: 'pending', changed_by: { id: 2, name: 'Ravi Employee' }, reason: null, changed_at: '2026-11-28T05:00:00Z' },
      ],
      'GET /api/v1/users': page([employeeUser, adminUser]),
      ...extra,
    }
  }

  it('shows the header, fees with Indian formatting, history and payment summary', async () => {
    stubFetch(detailRoutes(adminUser))
    renderApp('/jobs/21')
    expect(await screen.findByRole('heading', { name: /J-0021: RC renewal/ })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Ravi Kumar' }).getAttribute('href')).toBe('/customers/7')
    expect(screen.getByText('05-12-2026')).toBeTruthy()
    expect((await screen.findAllByText('₹250.00')).length).toBeGreaterThan(0)
    expect(await screen.findByText('28-11-2026 10:30')).toBeTruthy() // 05:00Z is 10:30 in India
    expect(screen.getByText(/Recording payments and receipts comes in a later step/)).toBeTruthy()
  })

  it('shows a not-found message for a job the employee cannot see', async () => {
    stubFetch({ 'GET /api/v1/auth/me': employeeUser, 'GET /api/v1/jobs/21': jsonResponse(404, { detail: 'Job not found' }) })
    renderApp('/jobs/21')
    expect(await screen.findByRole('heading', { name: 'Job not found' })).toBeTruthy()
    expect(screen.getByText(/not assigned to it/)).toBeTruthy()
  })

  describe('roles', () => {
    it('gives admins assignment, fee edit/remove and cancel', async () => {
      stubFetch(detailRoutes(adminUser, makeJob({ allowed_next_statuses: ['waiting_for_documents', 'submitted', 'cancelled'] })))
      renderApp('/jobs/21')
      expect(await screen.findByLabelText('Assign to')).toBeTruthy()
      expect(await screen.findByRole('button', { name: /Edit/ })).toBeTruthy()
      expect(screen.getByRole('button', { name: /Remove/ })).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Cancel job' })).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Add fee' })).toBeTruthy()
    })

    it('hides those from employees, who can still add fees before a payment', async () => {
      stubFetch(detailRoutes(employeeUser, makeJob({ allowed_next_statuses: ['waiting_for_documents', 'submitted', 'cancelled'] })))
      renderApp('/jobs/21')
      await screen.findByText('Service charge')
      expect(screen.queryByLabelText('Assign to')).toBeNull()
      expect(screen.queryByRole('button', { name: /Unassign/ })).toBeNull()
      expect(screen.queryByRole('button', { name: /Edit/ })).toBeNull()
      expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull()
      expect(screen.queryByRole('button', { name: 'Cancel job' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Add fee' })).toBeTruthy()
      expect(screen.getByText('Ravi Employee', { selector: 'li span' })).toBeTruthy()
    })

    it('stops employees adding fees once a payment is received, but not admins', async () => {
      const paid = makeJob({ payment_summary: { fees_total: '250.00', paid: '100.00', balance: '150.00', state: 'partial' } })
      stubFetch(detailRoutes(employeeUser, paid))
      const { unmount } = renderApp('/jobs/21')
      await screen.findByText('Service charge')
      expect(screen.queryByRole('button', { name: 'Add fee' })).toBeNull()
      expect(screen.getByText(/only an admin can add fees now/)).toBeTruthy()
      unmount()
      vi.unstubAllGlobals()
      stubFetch(detailRoutes(adminUser, paid))
      renderApp('/jobs/21')
      expect(await screen.findByRole('button', { name: 'Add fee' })).toBeTruthy()
    })

    it('makes a closed job read-only for employees', async () => {
      stubFetch(detailRoutes(employeeUser, makeJob({ status: 'completed', allowed_next_statuses: [] })))
      renderApp('/jobs/21')
      expect(await screen.findByText(/read-only: this job is closed/)).toBeTruthy()
      await screen.findByText('Service charge')
      expect(screen.queryByLabelText(/^Upload/)).toBeNull()
      expect(screen.queryByRole('button', { name: 'Add fee' })).toBeNull()
      expect(screen.queryByRole('group', { name: 'Change status' })).toBeNull()
      expect(screen.getByText(/No further status changes are possible/)).toBeTruthy()
    })
  })

  describe('status actions', () => {
    it('offers exactly the allowed next statuses and posts the chosen one', async () => {
      let body: unknown
      stubFetch(
        detailRoutes(employeeUser, makeJob(), {
          'POST /api/v1/jobs/21/status': (_u: URL, init: RequestInit) => {
            body = JSON.parse(String(init.body))
            return jsonResponse(200, makeJob({ status: 'waiting_for_documents', allowed_next_statuses: ['in_progress'] }))
          },
        }),
      )
      renderApp('/jobs/21')
      const group = await screen.findByRole('group', { name: 'Change status' })
      expect(within(group).getAllByRole('button').map((b) => b.textContent)).toEqual(['Wait for documents', 'Mark as submitted'])
      await user.click(within(group).getByRole('button', { name: 'Wait for documents' }))
      await waitFor(() => expect(body).toEqual({ status: 'waiting_for_documents', override_document_check: false }))
      expect(await screen.findByRole('button', { name: 'Start work' })).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Mark as submitted' })).toBeNull()
    })

    const refusal = jsonResponse(422, { detail: 'Required documents are not verified yet: Aadhaar card.' })

    it('shows the server message when submitting without verified documents; employees cannot override', async () => {
      stubFetch(detailRoutes(employeeUser, makeJob(), { 'POST /api/v1/jobs/21/status': refusal }))
      renderApp('/jobs/21')
      await user.click(await screen.findByRole('button', { name: 'Mark as submitted' }))
      expect((await screen.findByRole('alert')).textContent).toContain('Required documents are not verified yet: Aadhaar card.')
      expect(screen.queryByRole('button', { name: /Override/ })).toBeNull()
    })

    it('lets an admin override with a required reason', async () => {
      const bodies: unknown[] = []
      stubFetch(
        detailRoutes(adminUser, makeJob(), {
          'POST /api/v1/jobs/21/status': (_u: URL, init: RequestInit) => {
            const body = JSON.parse(String(init.body)) as { override_document_check: boolean }
            bodies.push(body)
            return body.override_document_check ? jsonResponse(200, makeJob({ status: 'submitted', allowed_next_statuses: ['completed'] })) : refusal.clone()
          },
        }),
      )
      renderApp('/jobs/21')
      await user.click(await screen.findByRole('button', { name: 'Mark as submitted' }))
      await user.click(await screen.findByRole('button', { name: 'Override the document check' }))

      // a reason is required
      await user.click(screen.getByRole('button', { name: 'Submit anyway' }))
      expect(await screen.findByText(/Enter a reason/)).toBeTruthy()
      expect(bodies).toHaveLength(1)

      await user.type(screen.getByLabelText('Reason for the override'), 'Customer will bring the original tomorrow')
      await user.click(screen.getByRole('button', { name: 'Submit anyway' }))
      await waitFor(() => expect(bodies).toHaveLength(2))
      expect(bodies[1]).toEqual({
        status: 'submitted',
        override_document_check: true,
        reason: 'Customer will bring the original tomorrow',
      })
      expect(await screen.findByRole('button', { name: 'Mark as completed' })).toBeTruthy()
    })

    it('cancels only with a reason, as an admin', async () => {
      let body: unknown
      stubFetch(
        detailRoutes(adminUser, makeJob({ allowed_next_statuses: ['waiting_for_documents', 'cancelled'] }), {
          'POST /api/v1/jobs/21/cancel': (_u: URL, init: RequestInit) => {
            body = JSON.parse(String(init.body))
            return jsonResponse(200, makeJob({ status: 'cancelled', allowed_next_statuses: [] }))
          },
        }),
      )
      renderApp('/jobs/21')
      expect(screen.queryByRole('button', { name: 'Cancelled' })).toBeNull() // not a plain status button
      await user.click(await screen.findByRole('button', { name: 'Cancel job' }))
      await user.click(screen.getByRole('button', { name: 'Cancel this job' }))
      expect(await screen.findByText(/Enter a reason/)).toBeTruthy()
      expect(body).toBeUndefined()
      await user.type(screen.getByLabelText('Reason for cancelling'), 'Customer withdrew')
      await user.click(screen.getByRole('button', { name: 'Cancel this job' }))
      await waitFor(() => expect(body).toEqual({ reason: 'Customer withdrew' }))
      expect(await screen.findByText(/No further status changes are possible/)).toBeTruthy()
    })
  })

  describe('fees', () => {
    it('validates and adds a fee', async () => {
      let body: unknown
      stubFetch(
        detailRoutes(employeeUser, makeJob(), {
          'POST /api/v1/jobs/21/fees': (_u: URL, init: RequestInit) => {
            body = JSON.parse(String(init.body))
            return jsonResponse(201, { id: 6 })
          },
        }),
      )
      renderApp('/jobs/21')
      await user.click(await screen.findByRole('button', { name: 'Add fee' }))
      await user.click(screen.getByRole('button', { name: 'Add fee', description: '' }))
      expect(await screen.findByText('Enter what the fee is for.')).toBeTruthy()
      expect(screen.getByText(/Enter an amount in rupees/)).toBeTruthy()
      await user.type(screen.getByLabelText('Description'), 'Stamp')
      await user.type(screen.getByLabelText('Amount (₹)'), '40.5')
      await user.selectOptions(screen.getByLabelText('Fee type'), 'Department fee')
      await user.click(screen.getByRole('button', { name: 'Add fee', description: '' }))
      await waitFor(() => expect(body).toEqual({ fee_type: 'department', description: 'Stamp', amount: '40.5' }))
    })

    it('lets an admin edit and remove after confirming', async () => {
      let patched: unknown
      let removed = false
      stubFetch(
        detailRoutes(adminUser, makeJob(), {
          'PATCH /api/v1/jobs/21/fees/5': (_u: URL, init: RequestInit) => {
            patched = JSON.parse(String(init.body))
            return jsonResponse(200, { id: 5 })
          },
          'DELETE /api/v1/jobs/21/fees/5': () => {
            removed = true
            return new Response(null, { status: 204 })
          },
        }),
      )
      renderApp('/jobs/21')
      await user.click(await screen.findByRole('button', { name: /Edit/ }))
      const amount = screen.getByLabelText('Amount (₹)')
      await user.clear(amount)
      await user.type(amount, '300')
      await user.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => expect(patched).toEqual({ fee_type: 'service', description: 'Service charge', amount: '300' }))

      await user.click(await screen.findByRole('button', { name: /^Remove/ }))
      expect(removed).toBe(false)
      await user.click(screen.getByRole('button', { name: /Confirm remove/ }))
      await waitFor(() => expect(removed).toBe(true))
    })
  })
})
