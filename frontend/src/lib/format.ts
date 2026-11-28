const TIME_ZONE = 'Asia/Kolkata'

const dateParts = new Intl.DateTimeFormat('en-GB', {
  timeZone: TIME_ZONE,
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
})
const timeParts = new Intl.DateTimeFormat('en-GB', {
  timeZone: TIME_ZONE,
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

/** 2026-11-21 or a full timestamp -> 21-11-2026 (India time). */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  // A plain calendar date has no time zone: do not shift it.
  const plain = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (plain) return `${plain[3]}-${plain[2]}-${plain[1]}`
  const date = new Date(hasZone(value) ? value : `${value}Z`)
  if (Number.isNaN(date.getTime())) return '-'
  return dateParts.format(date).replace(/\//g, '-')
}

/** Timestamp -> 21-11-2026 14:30 (India time). */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '-'
  const date = new Date(hasZone(value) ? value : `${value}Z`)
  if (Number.isNaN(date.getTime())) return '-'
  return `${dateParts.format(date).replace(/\//g, '-')} ${timeParts.format(date)}`
}

// Timestamps without an offset are treated as UTC.
function hasZone(value: string): boolean {
  return /(Z|[+-]\d{2}:?\d{2})$/.test(value)
}

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  in_progress: 'In progress',
  waiting_for_documents: 'Waiting for documents',
  submitted: 'Submitted',
  completed: 'Completed',
  cancelled: 'Cancelled',
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

const rupees = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** "1234.5" -> "₹1,234.50" (Indian digit grouping). */
export function formatMoney(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '-'
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isNaN(n) ? '-' : rupees.format(n)
}

const PAYMENT_STATE_LABELS: Record<string, string> = {
  no_fees: 'No fees',
  unpaid: 'Unpaid',
  partial: 'Part paid',
  paid: 'Paid',
  overpaid: 'Overpaid',
}
export function paymentStateLabel(state: string): string {
  return PAYMENT_STATE_LABELS[state] ?? state
}
