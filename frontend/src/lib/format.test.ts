import { describe, expect, it } from 'vitest'
import { formatDate, formatDateTime, statusLabel } from './format'

describe('formatting', () => {
  it('shows a UTC time just before midnight as the next day in India', () => {
    expect(formatDateTime('2026-11-28T20:00:00Z')).toBe('29-11-2026 01:30')
    expect(formatDate('2026-11-28T20:00:00Z')).toBe('29-11-2026')
  })

  it('treats a timestamp without an offset as UTC', () => {
    expect(formatDateTime('2026-11-28T20:00:00')).toBe('29-11-2026 01:30')
    expect(formatDateTime('2026-11-28T20:00:00.123456')).toBe('29-11-2026 01:30')
  })

  it('respects an explicit offset', () => {
    expect(formatDateTime('2026-11-28T18:29:00+00:00')).toBe('28-11-2026 23:59')
  })

  it('does not shift a plain calendar date', () => {
    expect(formatDate('2026-11-28')).toBe('28-11-2026')
  })

  it('handles missing and invalid values', () => {
    expect(formatDate(null)).toBe('-')
    expect(formatDateTime('not a date')).toBe('-')
  })

  it('labels statuses in plain words', () => {
    expect(statusLabel('waiting_for_documents')).toBe('Waiting for documents')
    expect(statusLabel('something_new')).toBe('something_new')
  })
})

describe('formatMoney', () => {
  it('shows rupees with two decimals and Indian grouping', async () => {
    const { formatMoney } = await import('./format')
    expect(formatMoney('1234567.5')).toBe('₹12,34,567.50')
    expect(formatMoney(0)).toBe('₹0.00')
    expect(formatMoney(null)).toBe('-')
    expect(formatMoney('abc')).toBe('-')
  })
})
