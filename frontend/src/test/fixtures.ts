import type { Job } from '../lib/types'

export const page = <T,>(items: T[], extra: Record<string, unknown> = {}) => ({
  items,
  total: items.length,
  page: 1,
  page_size: 20,
  total_pages: 1,
  ...extra,
})

export const customer = {
  id: 7,
  name: 'Ravi Kumar',
  phone: '9876543210',
  email: 'ravi@example.com',
  address: '12 Test Street',
  notes: null,
  created_at: '2026-11-01T00:00:00Z',
}
export const customerSummary = { id: 7, name: 'Ravi Kumar', phone_masked: 'XXXXXX3210', created_at: '2026-11-01T00:00:00Z' }

export const service = {
  id: 3,
  category: { id: 1, name: 'Transport', code: 'TRN' },
  name: 'RC renewal',
  code: 'RC',
  description: null,
  fee_type: 'fixed' as const,
  default_service_fee: '250.00',
  requires_vehicle: false,
  is_active: true,
  sort_order: 1,
}
export const category = { id: 1, name: 'Transport', code: 'TRN', description: null, is_active: true, sort_order: 1 }
export const requirements = [
  {
    id: 1,
    document_type: { id: 11, code: 'aadhaar', name: 'Aadhaar card', sensitivity: 'sensitive' },
    is_required: true,
    allow_multiple: false,
    is_active: true,
    description: null,
    sort_order: 1,
    created_at: '2026-11-01T00:00:00Z',
  },
]

export function makeJob(over: Partial<Job> = {}): Job {
  return {
    id: 21,
    job_number: 'J-0021',
    title: 'RC renewal',
    description: null,
    status: 'in_progress',
    due_date: '2026-12-05',
    created_by: 2,
    created_at: '2026-11-28T05:00:00Z',
    customer: { id: 7, name: 'Ravi Kumar', phone: 'XXXXXX3210' },
    service: { id: 3, name: 'RC renewal', code: 'RC' },
    assignees: [{ id: 2, name: 'Ravi Employee' }],
    allowed_next_statuses: ['waiting_for_documents', 'submitted'],
    document_progress: { required_total: 1, verified: 0, to_review: 0, processing: 0, rejected: 0, missing: 1, complete: false },
    payment_summary: { fees_total: '0.00', paid: '0.00', balance: '0.00', state: 'no_fees' },
    vehicle: null,
    ...over,
  }
}

export function makeChecklist(status?: string) {
  return {
    items: [
      {
        id: 31,
        document_type_id: 11,
        document_type_code: 'aadhaar',
        document_type_name: 'Aadhaar card',
        sensitivity: 'sensitive',
        is_required: true,
        allow_multiple: false,
        state: status ? 'pending' : 'missing',
        documents: status ? [{ id: 90, filename: 'aadhaar.pdf', status, current_version: 1 }] : [],
      },
    ],
    required_total: 1,
    required_verified: status === 'verified' ? 1 : 0,
    complete: status === 'verified',
  }
}

export const noFees = { items: [], totals: { service: '0.00', third_party: '0.00', department: '0.00', total: '0.00' } }
