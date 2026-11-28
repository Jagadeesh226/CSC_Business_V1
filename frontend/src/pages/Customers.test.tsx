import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { adminUser, employeeUser, jsonResponse, renderApp, stubFetch } from '../test/utils'
import { customer, customerSummary, page } from '../test/fixtures'
import { debounceConfig } from '../lib/hooks'

beforeEach(() => {
  debounceConfig.ms = 5
})
afterEach(() => vi.unstubAllGlobals())

describe('customers page', () => {
  it('searches and shows masked phone numbers', async () => {
    const fetchMock = stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'GET /api/v1/customers': page([customerSummary]),
    })
    renderApp('/customers')
    await screen.findByText('XXXXXX3210')
    expect(screen.queryByText('9876543210')).toBeNull()

    await userEvent.type(screen.getByLabelText('Search customers'), '98765')
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes('search=98765'))).toBe(true))
  })

  it('shows an empty state and an error with retry', async () => {
    let fail = true
    stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'GET /api/v1/customers': () => (fail ? jsonResponse(500, { detail: 'Server broke' }) : jsonResponse(200, page([]))),
    })
    renderApp('/customers')
    expect((await screen.findByRole('alert')).textContent).toContain('Server broke')
    fail = false
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText(/No customers yet/)).toBeTruthy()
  })

  it('creates a customer and shows possible duplicates without merging', async () => {
    let posted: unknown
    stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'GET /api/v1/customers': page([]),
      'POST /api/v1/customers': (_url, init) => {
        posted = JSON.parse(String(init.body))
        return jsonResponse(201, {
          customer: { ...customer, id: 8, name: 'Ravi K' },
          possible_duplicates: [customerSummary],
        })
      },
      'GET /api/v1/customers/7': customer,
      'GET /api/v1/jobs': page([]),
    })
    renderApp('/customers')
    await userEvent.click(await screen.findByRole('button', { name: 'New customer' }))
    await userEvent.type(screen.getByLabelText('Name'), 'Ravi K')
    await userEvent.type(screen.getByLabelText('Mobile number'), '98765 43210')
    await userEvent.click(screen.getByRole('button', { name: 'Save customer' }))

    expect(await screen.findByText('This customer may already exist')).toBeTruthy()
    expect(posted).toMatchObject({ name: 'Ravi K', phone: '98765 43210', email: null })
    expect(screen.getByText(/Nothing has\s+been merged/)).toBeTruthy()
    expect(screen.getByText(/XXXXXX3210/)).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /Use this customer instead/ }))
    expect(await screen.findByRole('heading', { name: 'Ravi Kumar' })).toBeTruthy()
  })

  it('validates the form before sending', async () => {
    const fetchMock = stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'GET /api/v1/customers': page([]),
    })
    renderApp('/customers')
    await userEvent.click(await screen.findByRole('button', { name: 'New customer' }))
    await userEvent.type(screen.getByLabelText('Mobile number'), '123')
    await userEvent.click(screen.getByRole('button', { name: 'Save customer' }))
    expect(await screen.findByText('Enter the customer name.')).toBeTruthy()
    expect(screen.getByText(/valid mobile number/)).toBeTruthy()
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'POST')).toBe(false)
  })
})

describe('customer profile', () => {
  const base = {
    'GET /api/v1/auth/me': adminUser,
    'GET /api/v1/customers/7': customer,
  }

  it('lists the changed fields and saves only after confirmation', async () => {
    let patched: unknown
    stubFetch({
      ...base,
      'PATCH /api/v1/customers/7': (_u, init) => {
        patched = JSON.parse(String(init.body))
        return jsonResponse(200, { ...customer, name: 'Ravi Kumar S', address: null })
      },
    })
    renderApp('/customers/7')
    await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
    const name = screen.getByLabelText('Name')
    await userEvent.clear(name)
    await userEvent.type(name, 'Ravi Kumar S')
    await userEvent.clear(screen.getByLabelText('Address'))
    await userEvent.click(screen.getByRole('button', { name: 'Review changes' }))

    const confirm = await screen.findByRole('region', { name: 'Confirm these changes' })
    expect(within(confirm).getByText(/Ravi Kumar to Ravi Kumar S/)).toBeTruthy()
    expect(within(confirm).getByText(/12 Test Street to \(empty\)/)).toBeTruthy()
    expect(within(confirm).queryByText(/Mobile number/)).toBeNull() // unchanged fields are not listed
    expect(patched).toBeUndefined() // nothing saved yet

    await userEvent.click(screen.getByRole('button', { name: 'Confirm and save' }))
    await waitFor(() => expect(patched).toEqual({ name: 'Ravi Kumar S', address: null }))
    expect(await screen.findByRole('button', { name: 'Edit details' })).toBeTruthy()
  })

  it('does not call the server when nothing changed', async () => {
    const fetchMock = stubFetch(base)
    renderApp('/customers/7')
    await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
    await userEvent.click(screen.getByRole('button', { name: 'Review changes' }))
    expect(await screen.findByText('Nothing has changed.')).toBeTruthy()
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'PATCH')).toBe(false)
  })

  it('shows the Activity tab to admins only', async () => {
    stubFetch({ ...base, 'GET /api/v1/audit-logs': page([]) })
    renderApp('/customers/7')
    expect(await screen.findByRole('tab', { name: 'Activity' })).toBeTruthy()
    await userEvent.click(screen.getByRole('tab', { name: 'Activity' }))
    expect(await screen.findByText('No recorded activity.')).toBeTruthy()
  })

  it('hides the Activity tab from employees and offers quick actions', async () => {
    stubFetch({ ...base, 'GET /api/v1/auth/me': employeeUser })
    renderApp('/customers/7')
    await screen.findByRole('heading', { name: 'Ravi Kumar' })
    expect(screen.queryByRole('tab', { name: 'Activity' })).toBeNull()
    expect(screen.getByRole('link', { name: 'New job for this customer' }).getAttribute('href')).toBe('/jobs/new?customer_id=7')
  })

  it('shows documents with a review link where needed', async () => {
    stubFetch({
      ...base,
      'GET /api/v1/document-types': [],
      'GET /api/v1/customers/7/documents': [
        {
          id: 90,
          customer_id: 7,
          document_type: { id: 11, code: 'aadhaar', name: 'Aadhaar card', sensitivity: 'sensitive' },
          filename: 'aadhaar.pdf',
          status: 'needs_review',
          created_at: '2026-11-20T00:00:00Z',
          job_ids: [21],
          archived_at: null,
        },
      ],
    })
    renderApp('/customers/7?tab=documents')
    const row = await screen.findByRole('row', { name: /aadhaar\.pdf/ })
    expect(within(row).getByText('Ready for review')).toBeTruthy()
    expect(within(row).getByRole('link', { name: /Review/ }).getAttribute('href')).toBe('/documents?review=90')
    expect(within(row).getByRole('link', { name: 'Job 21' })).toBeTruthy()
    expect(within(row).getByText('20-11-2026')).toBeTruthy()
  })
})
