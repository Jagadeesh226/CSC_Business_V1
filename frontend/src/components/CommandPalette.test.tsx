import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppRoutes } from '../routes'
import { emptyDashboard, emptyQueue, employeeUser, renderApp, renderWithProviders, stubFetch } from '../test/utils'

afterEach(() => vi.unstubAllGlobals())

const page = (items: unknown[]) => ({ items, total: items.length, page: 1, page_size: 5, total_pages: 1 })

function setup() {
  return stubFetch({
    'GET /api/v1/auth/me': employeeUser,
    'GET /api/v1/dashboard': emptyDashboard,
    'GET /api/v1/work-queue': emptyQueue,
    'GET /api/v1/customers/7': { id: 7, name: 'Ravi Kumar', phone: '9876543210', email: null, address: null, notes: null, created_at: '2026-11-01T00:00:00Z' },
    'GET /api/v1/customers': page([{ id: 7, name: 'Ravi Kumar', phone_masked: 'XXXXXX1234', created_at: '2026-11-01T00:00:00Z' }]),
    'GET /api/v1/jobs': page([
      { id: 9, job_number: 'J-0009', title: 'RC renewal', status: 'pending', customer: { id: 7, name: 'Ravi Kumar', phone: null } },
    ]),
    'GET /api/v1/vehicles': page([
      { id: 3, registration_number: 'KA01AB1234', owner_name: 'Ravi Kumar', customer: { id: 7, name: 'Ravi Kumar' } },
    ]),
  })
}

describe('command palette', () => {
  it('opens with Ctrl+K, searches after 2 characters and groups results', async () => {
    const fetchMock = setup()
    renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await userEvent.keyboard('{Control>}k{/Control}')
    const input = await screen.findByRole('combobox')
    expect(document.activeElement).toBe(input)

    await userEvent.type(input, 'r')
    await new Promise((r) => setTimeout(r, 400))
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('search='))).toBe(false)

    await userEvent.type(input, 'a')
    expect(await screen.findByRole('group', { name: 'Customers' })).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Jobs' })).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Vehicles' })).toBeTruthy()
    expect(screen.getByText('XXXXXX1234')).toBeTruthy()
    expect(screen.getAllByRole('option')).toHaveLength(3)
  })

  it('moves with arrows, chooses with Enter and navigates', async () => {
    setup()
    renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(await screen.findByRole('combobox'), 'ravi')
    await screen.findAllByRole('option')
    const options = () => screen.getAllByRole('option')
    expect(options()[0].getAttribute('aria-selected')).toBe('true')
    await userEvent.keyboard('{ArrowDown}')
    expect(options()[1].getAttribute('aria-selected')).toBe('true')
    await userEvent.keyboard('{ArrowUp}{ArrowUp}')
    expect(options()[2].getAttribute('aria-selected')).toBe('true') // wraps to last
    await userEvent.keyboard('{Enter}')
    expect(await screen.findByRole('heading', { name: 'Vehicle' })).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('closes on Escape, restores focus, and traps Tab', async () => {
    setup()
    renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    const trigger = screen.getByRole('button', { name: /Search/ })
    await userEvent.click(trigger)
    const input = await screen.findByRole('combobox')
    await userEvent.tab()
    expect(document.activeElement).toBe(input)
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })

  it('waits for a pause in typing before searching', async () => {
    const fetchMock = setup()
    renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(await screen.findByRole('combobox'), 'ra')
    await new Promise((r) => setTimeout(r, 100))
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('search='))).toBe(false)
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes('search=ra'))).toBe(true))
  })

  it('closes when the backdrop is clicked, but not when the panel is', async () => {
    setup()
    renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await userEvent.keyboard('{Control>}k{/Control}')
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(dialog)
    expect(screen.getByRole('dialog')).toBeTruthy()
    await userEvent.click(dialog.parentElement as HTMLElement)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('scrolls the active option into view and makes the page behind inert', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    setup()
    const { container } = renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    expect(container.querySelector('.shell')?.hasAttribute('inert')).toBe(false)
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(await screen.findByRole('combobox'), 'ravi')
    await screen.findAllByRole('option')
    expect(container.querySelector('.shell')?.hasAttribute('inert')).toBe(true)
    scroll.mockClear()
    await userEvent.keyboard('{ArrowDown}')
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
    // @ts-expect-error remove the stub again so other tests see plain jsdom
    delete Element.prototype.scrollIntoView
  })

  describe('focus in React StrictMode (as in development)', () => {
    const renderStrict = () =>
      renderWithProviders(
        <StrictMode>
          <AppRoutes />
        </StrictMode>,
      )

    it('returns focus to the Search button after Escape', async () => {
      setup()
      renderStrict()
      await screen.findByRole('heading', { name: 'Dashboard' })
      const trigger = screen.getByRole('button', { name: /Search/ })
      await userEvent.click(trigger)
      await screen.findByRole('combobox')
      await userEvent.keyboard('{Escape}')
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(document.activeElement).toBe(trigger)
    })

    it('returns focus to the element that had it before Ctrl+K', async () => {
      setup()
      renderStrict()
      await screen.findByRole('heading', { name: 'Dashboard' })
      const link = screen.getByRole('link', { name: 'Customers' })
      link.focus()
      await userEvent.keyboard('{Control>}k{/Control}')
      await screen.findByRole('combobox')
      await userEvent.keyboard('{Escape}')
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(document.activeElement).toBe(link)
    })

    it('falls back to the Search button when nothing was focused', async () => {
      setup()
      renderStrict()
      await screen.findByRole('heading', { name: 'Dashboard' })
      await userEvent.keyboard('{Control>}k{/Control}')
      await screen.findByRole('combobox')
      await userEvent.keyboard('{Escape}')
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(document.activeElement).toBe(screen.getByRole('button', { name: /Search/ }))
    })

    it('does not pull focus back after a result is chosen', async () => {
      setup()
      renderStrict()
      await screen.findByRole('heading', { name: 'Dashboard' })
      const trigger = screen.getByRole('button', { name: /Search/ })
      await userEvent.click(trigger)
      await userEvent.type(await screen.findByRole('combobox'), 'ravi')
      await screen.findAllByRole('option')
      await userEvent.keyboard('{Enter}')
      await screen.findByRole('heading', { name: 'Ravi Kumar' })
      expect(document.activeElement).not.toBe(trigger)
    })
  })
})
