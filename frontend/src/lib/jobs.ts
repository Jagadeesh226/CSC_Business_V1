import type { Job } from './types'

export const isClosed = (job: Job) => job.status === 'completed' || job.status === 'cancelled'
export const hasReceivedPayment = (job: Job) => Number(job.payment_summary?.paid ?? 0) > 0

export function documentProgressText(job: Job): string {
  const p = job.document_progress
  if (!p) return '-'
  if (p.required_total === 0) return 'No documents needed'
  const parts = [`${p.verified} of ${p.required_total} verified`]
  if (p.processing > 0) parts.push(`${p.processing} being read`)
  if (p.to_review > 0) parts.push(`${p.to_review} to review`)
  if (p.rejected > 0) parts.push(`${p.rejected} rejected`)
  if (p.missing > 0) parts.push(`${p.missing} missing`)
  return parts.join(', ')
}
