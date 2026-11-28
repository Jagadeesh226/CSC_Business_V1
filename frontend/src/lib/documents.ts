import { api } from './api'

export interface UploadTarget {
  customerId: number
  jobId?: number
  checklistItemId?: number
  documentTypeId?: number
}

export function uploadDocument(file: File, target: UploadTarget) {
  const form = new FormData()
  form.append('file', file)
  if (target.jobId !== undefined) form.append('job_id', String(target.jobId))
  if (target.checklistItemId !== undefined) form.append('checklist_item_id', String(target.checklistItemId))
  if (target.documentTypeId !== undefined) form.append('document_type_id', String(target.documentTypeId))
  return api.postForm('/api/v1/customers/{customer_id}/documents', {
    params: { customer_id: target.customerId },
    form,
  })
}

const DOCUMENT_STATUS: Record<string, string> = {
  uploaded: 'Uploaded',
  processing: 'Reading document',
  needs_review: 'Ready for review',
  verified: 'Verified',
  rejected: 'Rejected',
}
export function documentStatusLabel(status: string): string {
  return DOCUMENT_STATUS[status] ?? status
}

const CHECKLIST_STATE: Record<string, string> = {
  missing: 'Missing',
  pending: 'Waiting for verification',
  rejected: 'Rejected',
  verified: 'Verified',
}
export function checklistStateLabel(state: string): string {
  return CHECKLIST_STATE[state] ?? state
}

/** Documents the server is still reading. */
export function isReading(status: string): boolean {
  return status === 'processing' || status === 'uploaded'
}

/** Where a document that needs review is opened (the review screen comes in step 6.3). */
export function reviewPath(documentId: number): string {
  return `/documents?review=${documentId}`
}

/** Polling while documents are being read. */
export const pollConfig = { ms: 2000, maxMs: 3 * 60 * 1000 }
