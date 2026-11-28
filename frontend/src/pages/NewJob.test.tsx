import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { adminUser, employeeUser, jsonResponse, renderApp, stubFetch } from '../test/utils'
import { category, customer, customerSummary, makeChecklist, makeJob, noFees, page, requirements, service } from '../test/fixtures'
import { debounceConfig } from '../lib/hooks'
import { pollConfig } from '../lib/documents'

const user = userEvent.setup({ applyAccept: false })

beforeEach(() => {
  debounceConfig.ms = 5
  pollConfig.ms = 20
})
afterEach(() => vi.unstubAllGlobals())

function wizardRoutes(opts: { me?: object } = {}) {
  const state = { phase: 'none' as 'none' | 'processing' | 'needs_review', reads: 0, checklistGets: 0, posted: undefined as unknown, uploaded: undefined as FormData | undefined }
  const routes = {
    'GET /api/v1/auth/me': opts.me ?? employeeUser,
    'GET /api/v1/services': [service],
    'GET /api/v1/service-categories': [category],
    'GET /api/v1/services/3/document-requirements': requirements,
    'GET /api/v1/customers': page([customerSummary]),
    'POST /api/v1/jobs': (_u: URL, init: RequestInit) => {
      state.posted = JSON.parse(String(init.body))
      return jsonResponse(201, makeJob())
    },
    'GET /api/v1/jobs/21': makeJob(),
    'GET /api/v1/jobs': page([]),
    'GET /api/v1/jobs/21/checklist': () => {
      state.checklistGets += 1
      if (state.phase === 'processing') {
        state.reads += 1
        if (state.reads >= 3) state.phase = 'needs_review'
      }
      return jsonResponse(200, makeChecklist(state.phase === 'none' ? undefined : state.phase))
    },
    'GET /api/v1/customers/7/documents': [],
    'POST /api/v1/customers/7/documents': (_u: URL, init: RequestInit) => {
      state.uploaded = init.body as FormData
      state.phase = 'processing'
      return jsonResponse(201, { id: 90 })
    },
    'GET /api/v1/jobs/21/fees': noFees,
    'GET /api/v1/users': page([employeeUser, adminUser]),
  }
  return { routes, state }
}

async function fillAndCreate() {
  await user.selectOptions(await screen.findByLabelText('Service'), 'RC renewal')
  await user.type(screen.getByLabelText('Customer mobile number'), '98765 43210')
  await user.click(await screen.findByRole('button', { name: /Use existing customer/ }))
  await user.click(screen.getByRole('button', { name: 'Create job' }))
  await screen.findByRole('heading', { name: 'Job J-0021 created' })
}

describe('new job wizard', () => {
  it('creates a job from service, mobile match and details, then uploads and follows OCR until it is done', async () => {
    const { routes, state } = wizardRoutes()
    stubFetch(routes)
    renderApp('/jobs/new')

    // 1. service: grouped by category, shows fee and required documents
    const select = await screen.findByLabelText('Service')
    expect(within(select).getByRole('group', { name: 'Transport' })).toBeTruthy()
    await user.selectOptions(select, 'RC renewal')
    expect(await screen.findByText(/service fee ₹250\.00/)).toBeTruthy()
    expect(await screen.findByText(/Documents: Aadhaar card/)).toBeTruthy()

    // 2. customer by mobile number
    await user.type(screen.getByLabelText('Customer mobile number'), '98765 43210')
    await user.click(await screen.findByRole('button', { name: /Use existing customer/ }))
    expect(screen.getByText(/XXXXXX3210/)).toBeTruthy()

    // 3. title prefilled from the service
    expect((screen.getByLabelText('Job title') as HTMLInputElement).value).toBe('RC renewal')
    await user.type(screen.getByLabelText('Due date (optional)'), '2026-12-05')
    await user.click(screen.getByRole('button', { name: 'Create job' }))

    expect(await screen.findByRole('heading', { name: 'Job J-0021 created' })).toBeTruthy()
    expect(state.posted).toEqual({
      customer_id: 7,
      service_id: 3,
      title: 'RC renewal',
      due_date: '2026-12-05',
      description: null,
      vehicle_id: null,
    })

    // 4. documents: upload to the checklist item
    const file = new File(['%PDF-1.4'], 'aadhaar.pdf', { type: 'application/pdf' })
    await user.upload(await screen.findByLabelText('Upload Aadhaar card'), file)
    await waitFor(() => expect(state.uploaded).toBeInstanceOf(FormData))
    expect(state.uploaded?.get('job_id')).toBe('21')
    expect(state.uploaded?.get('checklist_item_id')).toBe('31')
    expect(state.uploaded?.get('document_type_id')).toBe('11')
    expect((state.uploaded?.get('file') as File).name).toBe('aadhaar.pdf')

    // OCR progress is followed without the person doing anything...
    expect((await screen.findAllByText(/Reading document/)).length).toBeGreaterThan(0)
    // ...until the document is ready for review
    const review = await screen.findByRole('link', { name: /Review/ })
    expect(review.getAttribute('href')).toBe('/documents?review=90')
    expect(screen.queryByText(/Reading documents\./)).toBeNull()

    // polling has stopped
    const settled = state.checklistGets
    await new Promise((r) => setTimeout(r, 150))
    expect(state.checklistGets).toBe(settled)

    // next actions
    expect(screen.getByRole('link', { name: 'Open customer' }).getAttribute('href')).toBe('/customers/7')
    expect(screen.getByRole('link', { name: 'Update status' }).getAttribute('href')).toBe('/jobs/21')
    expect((screen.getByRole('button', { name: 'Record payment' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('stops polling when the person leaves the page', async () => {
    const { routes, state } = wizardRoutes()
    state.phase = 'processing'
    state.reads = -1000 // never finishes reading
    stubFetch(routes)
    renderApp('/jobs/new')
    await fillAndCreate()
    await screen.findByText(/Reading documents\./)
    await user.click(screen.getByRole('link', { name: 'Open customer' }))
    await waitFor(() => expect(screen.queryByText(/Reading documents\./)).toBeNull())
    const after = state.checklistGets
    await new Promise((r) => setTimeout(r, 120))
    expect(state.checklistGets).toBe(after)
  })

  it('creates a new customer inline when the number is unknown', async () => {
    const { routes } = wizardRoutes()
    let posted: unknown
    stubFetch({
      ...routes,
      'GET /api/v1/customers': page([]),
      'POST /api/v1/customers': (_u: URL, init: RequestInit) => {
        posted = JSON.parse(String(init.body))
        return jsonResponse(201, { customer: { ...customer, id: 9, name: 'New Person' }, possible_duplicates: [] })
      },
    })
    renderApp('/jobs/new')
    await user.type(await screen.findByLabelText('Customer mobile number'), '9000000001')
    await screen.findByText('No existing customer matches.')
    await user.click(screen.getByRole('button', { name: 'Create new customer' }))
    expect((screen.getByLabelText('Mobile number') as HTMLInputElement).value).toBe('9000000001') // not retyped
    await user.type(screen.getByLabelText('Name'), 'New Person')
    await user.click(screen.getByRole('button', { name: 'Save customer' }))
    expect(await screen.findByText(/New Person/)).toBeTruthy()
    expect(posted).toMatchObject({ name: 'New Person', phone: '9000000001' })
  })

  it('fills in the customer from the URL', async () => {
    const { routes } = wizardRoutes()
    stubFetch({ ...routes, 'GET /api/v1/customers/7': customer })
    renderApp('/jobs/new?customer_id=7')
    expect(await screen.findByText(/9876543210/)).toBeTruthy()
    expect(screen.queryByLabelText('Customer mobile number')).toBeNull()
  })

  it('asks for the missing pieces before creating', async () => {
    const { routes } = wizardRoutes()
    const fetchMock = stubFetch(routes)
    renderApp('/jobs/new')
    await user.click(await screen.findByRole('button', { name: 'Create job' }))
    expect(await screen.findByText('Choose a service.')).toBeTruthy()
    expect(screen.getByText('Choose or create a customer.')).toBeTruthy()
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'POST')).toBe(false)
  })

  it('shows the server message when the job cannot be created', async () => {
    const { routes } = wizardRoutes()
    stubFetch({ ...routes, 'POST /api/v1/jobs': jsonResponse(422, { detail: 'The selected service is inactive.' }) })
    renderApp('/jobs/new')
    await user.selectOptions(await screen.findByLabelText('Service'), 'RC renewal')
    await user.type(screen.getByLabelText('Customer mobile number'), '98765 43210')
    await user.click(await screen.findByRole('button', { name: /Use existing customer/ }))
    await user.click(screen.getByRole('button', { name: 'Create job' }))
    expect((await screen.findByRole('alert')).textContent).toContain('The selected service is inactive.')
  })

  it('lets admins assign people; employees only see who is assigned', async () => {
    const { routes } = wizardRoutes({ me: adminUser })
    let assigned: unknown
    stubFetch({
      ...routes,
      'POST /api/v1/jobs/21/assignments': (_u: URL, init: RequestInit) => {
        assigned = JSON.parse(String(init.body))
        return jsonResponse(201, makeJob({ assignees: [{ id: 2, name: 'Ravi Employee' }] }))
      },
      'GET /api/v1/jobs/21': makeJob({ assignees: [] }),
      'POST /api/v1/jobs': jsonResponse(201, makeJob({ assignees: [] })),
    })
    renderApp('/jobs/new')
    await fillAndCreate()
    await user.selectOptions(await screen.findByLabelText('Assign to'), 'Ravi Employee (Employee)')
    await user.click(screen.getByRole('button', { name: 'Assign' }))
    await waitFor(() => expect(assigned).toEqual({ user_id: 2 }))
    expect(await screen.findByRole('button', { name: /Unassign/ })).toBeTruthy()
  })

  it('employees see the assignee without assignment controls', async () => {
    const { routes } = wizardRoutes()
    stubFetch(routes)
    renderApp('/jobs/new')
    await fillAndCreate()
    const section = screen.getByRole('heading', { name: '6. Assigned to' }).closest('section') as HTMLElement
    expect(within(section).getByText('Ravi Employee')).toBeTruthy()
    expect(within(section).queryByLabelText('Assign to')).toBeNull()
    expect(within(section).queryByRole('button', { name: /Unassign/ })).toBeNull()
  })
})

describe('unsaved changes', () => {
  it('asks before leaving a half-filled job and lets the person stay or leave', async () => {
    const { routes } = wizardRoutes()
    stubFetch(routes)
    renderApp('/jobs/new')
    await user.selectOptions(await screen.findByLabelText('Service'), 'RC renewal')

    await user.click(screen.getByRole('link', { name: 'Jobs' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave this page?' })
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Stay on this page' }))
    await user.click(within(dialog).getByRole('button', { name: 'Stay on this page' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByRole('heading', { name: 'New job' })).toBeTruthy()

    await user.click(screen.getByRole('link', { name: 'Jobs' }))
    await user.click(await screen.findByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByRole('heading', { name: 'Jobs' })).toBeTruthy()
  })

  it('sets the browser warning for closing the tab only while input is pending', async () => {
    const { routes } = wizardRoutes()
    stubFetch(routes)
    renderApp('/jobs/new')
    await screen.findByLabelText('Service')
    const clean = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(clean)
    expect(clean.defaultPrevented).toBe(false)

    await user.selectOptions(screen.getByLabelText('Service'), 'RC renewal')
    const dirty = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirty)
    expect(dirty.defaultPrevented).toBe(true)
  })

  it('does not warn after the job is created and nothing is pending', async () => {
    const { routes } = wizardRoutes()
    stubFetch(routes)
    renderApp('/jobs/new')
    await fillAndCreate()
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    await user.click(screen.getByRole('link', { name: 'Jobs' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(await screen.findByRole('heading', { name: 'Jobs' })).toBeTruthy()
  })

  it('warns after creation while a fee is typed but not added', async () => {
    const { routes } = wizardRoutes()
    stubFetch(routes)
    renderApp('/jobs/new')
    await fillAndCreate()
    await user.click(await screen.findByRole('button', { name: 'Add fee' }))
    await user.type(screen.getByLabelText('Amount (₹)'), '150')
    await user.click(screen.getByRole('link', { name: 'Jobs' }))
    expect(await screen.findByRole('alertdialog')).toBeTruthy()
  })
})

describe('upload validation', () => {
  async function toDocuments() {
    const { routes, state } = wizardRoutes()
    stubFetch(routes)
    renderApp('/jobs/new')
    await fillAndCreate()
    return { input: await screen.findByLabelText('Upload Aadhaar card'), state }
  }

  it('refuses a file type that is not JPEG, PNG or PDF', async () => {
    const { input, state } = await toDocuments()
    await user.upload(input, new File(['x'], 'notes.txt', { type: 'text/plain' }))
    expect((await screen.findByText(/"notes.txt" is not allowed/)).getAttribute('role')).toBe('alert')
    expect(state.uploaded).toBeUndefined()
  })

  it('refuses a file over 10 MB', async () => {
    const { input, state } = await toDocuments()
    const big = new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'big.pdf', { type: 'application/pdf' })
    await user.upload(input, big)
    expect(await screen.findByText(/larger than 10 MB/)).toBeTruthy()
    expect(state.uploaded).toBeUndefined()
  })

  it('shows the server message when an upload is refused', async () => {
    const { routes } = wizardRoutes()
    stubFetch({ ...routes, 'POST /api/v1/customers/7/documents': jsonResponse(413, { detail: 'File too large.' }) })
    renderApp('/jobs/new')
    await fillAndCreate()
    await user.upload(await screen.findByLabelText('Upload Aadhaar card'), new File(['x'], 'a.png', { type: 'image/png' }))
    expect(await screen.findByText('File too large.')).toBeTruthy()
  })

  it('offers a verified customer document for reuse and links it', async () => {
    const { routes } = wizardRoutes()
    let linked: unknown
    stubFetch({
      ...routes,
      'GET /api/v1/customers/7/documents': [
        {
          id: 70,
          customer_id: 7,
          document_type: { id: 11, code: 'aadhaar', name: 'Aadhaar card', sensitivity: 'sensitive' },
          filename: 'old-aadhaar.pdf',
          status: 'verified',
          verified_at: '2026-10-01T00:00:00Z',
          archived_at: null,
          reusable: true,
          job_ids: [],
        },
      ],
      'POST /api/v1/jobs/21/checklist/31/documents': (_u: URL, init: RequestInit) => {
        linked = JSON.parse(String(init.body))
        return jsonResponse(201, makeChecklist('verified'))
      },
      'GET /api/v1/jobs/21/checklist': () => jsonResponse(200, makeChecklist(linked ? 'verified' : undefined)),
    })
    renderApp('/jobs/new')
    await fillAndCreate()
    await user.selectOptions(await screen.findByLabelText('Reuse a verified Aadhaar card'), '70')
    await user.click(screen.getByRole('button', { name: 'Use this document' }))
    await waitFor(() => expect(linked).toEqual({ document_id: 70 }))
    await waitFor(() => expect(screen.getByText(/required documents verified/).textContent).toContain('1 of 1'))
  })
})
