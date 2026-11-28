import { useEffect, useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import { formatDate } from '../lib/format'
import {
  checklistStateLabel,
  documentStatusLabel,
  isReading,
  pollConfig,
  reviewPath,
  uploadDocument,
} from '../lib/documents'
import { ACCEPT, validateUpload } from '../lib/files'
import type { ChecklistItem, ChecklistResponse, DocumentRead } from '../lib/types'
import { ErrorNotice, Loading } from './Status'

function messageOf(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong. Please try again.'
}

interface Props {
  jobId: number
  customerId: number
  /** Staff may add documents (false for closed jobs seen by employees). */
  canChange: boolean
  /** True while a file is being sent, so the page can warn before leaving. */
  onBusyChange?: (busy: boolean) => void
}

export function ChecklistPanel({ jobId, customerId, canChange, onBusyChange }: Props) {
  const qc = useQueryClient()
  const [uploading, setUploading] = useState<Record<number, string>>({})
  const [errors, setErrors] = useState<Record<number, string>>({})
  const [timedOut, setTimedOut] = useState(false)
  const [attempt, setAttempt] = useState(0)

  const checklist = useQuery({
    queryKey: ['job', jobId, 'checklist'],
    queryFn: () => api.get('/api/v1/jobs/{job_id}/checklist', { params: { job_id: jobId } }),
    // Keep checking while any document is being read; stop when done, after a few
    // minutes, or when this panel is closed.
    refetchInterval: (query) => {
      const anyReading = query.state.data?.items.some((i) => i.documents.some((d) => isReading(d.status))) ?? false
      return anyReading && !timedOut ? pollConfig.ms : false
    },
  })

  const customerDocs = useQuery({
    queryKey: ['customer', customerId, 'documents'],
    queryFn: () => api.get('/api/v1/customers/{customer_id}/documents', { params: { customer_id: customerId } }),
    enabled: canChange,
  })

  const busy = Object.keys(uploading).length > 0
  useEffect(() => {
    onBusyChange?.(busy)
  }, [busy, onBusyChange])

  const reading = checklist.data?.items.some((i) => i.documents.some((d) => isReading(d.status))) ?? false
  useEffect(() => {
    if (!reading) return
    const t = setTimeout(() => setTimedOut(true), pollConfig.maxMs)
    return () => {
      clearTimeout(t)
      setTimedOut(false)
    }
  }, [reading, attempt])

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['job', jobId] })
    void qc.invalidateQueries({ queryKey: ['jobs'] })
    void qc.invalidateQueries({ queryKey: ['customer', customerId, 'documents'] })
  }

  const upload = async (item: ChecklistItem, file: File | undefined) => {
    if (!file) return
    setErrors((e) => ({ ...e, [item.id]: '' }))
    const problem = validateUpload(file)
    if (problem) {
      setErrors((e) => ({ ...e, [item.id]: problem }))
      return
    }
    setUploading((u) => ({ ...u, [item.id]: file.name }))
    try {
      await uploadDocument(file, {
        customerId,
        jobId,
        checklistItemId: item.id,
        documentTypeId: item.document_type_id,
      })
      setAttempt((n) => n + 1)
      await qc.invalidateQueries({ queryKey: ['job', jobId, 'checklist'] })
      refresh()
    } catch (err) {
      setErrors((e) => ({ ...e, [item.id]: messageOf(err) }))
    } finally {
      setUploading((u) => {
        const rest = { ...u }
        delete rest[item.id]
        return rest
      })
    }
  }

  const link = useMutation({
    mutationFn: ({ itemId, documentId }: { itemId: number; documentId: number }) =>
      api.post('/api/v1/jobs/{job_id}/checklist/{item_id}/documents', {
        params: { job_id: jobId, item_id: itemId },
        body: { document_id: documentId },
      }),
    onSuccess: (data: ChecklistResponse) => {
      qc.setQueryData(['job', jobId, 'checklist'], data)
      refresh()
    },
  })

  if (checklist.isPending) return <Loading label="Loading document checklist..." />
  if (checklist.isError) return <ErrorNotice error={checklist.error} onRetry={() => void checklist.refetch()} />
  const data = checklist.data
  if (data.items.length === 0) {
    return <p className="hint">This service needs no documents.</p>
  }

  return (
    <div>
      <p>
        <strong>
          {data.required_verified} of {data.required_total}
        </strong>{' '}
        required documents verified.{data.complete ? ' The checklist is complete.' : ''}
      </p>
      {reading && (
        <p role="status" className="hint">
          {timedOut
            ? 'Reading is taking longer than usual. You can keep working.'
            : 'Reading documents. You can keep working; this updates by itself.'}{' '}
          {timedOut && (
            <button
              type="button"
              className="btn"
              onClick={() => {
                setAttempt((n) => n + 1)
                void checklist.refetch()
              }}
            >
              Check again
            </button>
          )}
        </p>
      )}
      {link.isError && (
        <div role="alert" className="notice notice-error">
          {messageOf(link.error)}
        </div>
      )}
      <ul className="plain-list checklist">
        {data.items.map((item) => (
          <ChecklistRow
            key={item.id}
            item={item}
            canChange={canChange}
            uploadingName={uploading[item.id]}
            error={errors[item.id]}
            candidates={(customerDocs.data ?? []).filter(
              (d) =>
                d.reusable &&
                d.status === 'verified' &&
                !d.archived_at &&
                d.document_type?.id === item.document_type_id &&
                !item.documents.some((x) => x.id === d.id),
            )}
            linking={link.isPending && link.variables?.itemId === item.id}
            onUpload={(file) => void upload(item, file)}
            onReuse={(documentId) => link.mutate({ itemId: item.id, documentId })}
          />
        ))}
      </ul>
    </div>
  )
}

function ChecklistRow({
  item,
  canChange,
  uploadingName,
  error,
  candidates,
  linking,
  onUpload,
  onReuse,
}: {
  item: ChecklistItem
  canChange: boolean
  uploadingName?: string
  error?: string
  candidates: DocumentRead[]
  linking: boolean
  onUpload: (file: File | undefined) => void
  onReuse: (documentId: number) => void
}) {
  const id = useId()
  const [reuseId, setReuseId] = useState('')
  const canAdd = canChange && (item.allow_multiple || item.documents.length === 0 || item.state === 'rejected')
  return (
    <li className="checklist-item">
      <div className="row-between">
        <h3 id={`${id}-title`} style={{ margin: 0 }}>
          {item.document_type_name}
          {!item.is_required && <span className="hint"> (optional)</span>}
        </h3>
        <span className="status-text">{checklistStateLabel(item.state)}</span>
      </div>

      {item.documents.length > 0 && (
        <ul className="plain-list">
          {item.documents.map((d) => (
            <li key={d.id} className="row-between">
              <span>
                {d.filename} <span className="hint">(version {d.current_version})</span>
              </span>
              <span>
                <span className="status-text" role={isReading(d.status) ? 'status' : undefined}>
                  {isReading(d.status) ? `${documentStatusLabel(d.status)}...` : documentStatusLabel(d.status)}
                </span>
                {d.status === 'needs_review' && (
                  <>
                    {' '}
                    <Link to={reviewPath(d.id)}>
                      Review<span className="visually-hidden"> {d.filename}</span>
                    </Link>
                  </>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}

      {uploadingName && (
        <p role="status" className="hint">
          Uploading {uploadingName}...
        </p>
      )}
      {error && (
        <p role="alert" className="field-error">
          {error}
        </p>
      )}

      {canAdd && !uploadingName && (
        <div className="checklist-actions">
          <div className="field">
            <label htmlFor={`${id}-file`}>Upload {item.document_type_name}</label>
            <input
              id={`${id}-file`}
              type="file"
              accept={ACCEPT}
              aria-describedby={`${id}-help`}
              onChange={(e) => {
                const file = e.currentTarget.files?.[0]
                onUpload(file)
                e.currentTarget.value = ''
              }}
            />
            <span id={`${id}-help`} className="hint">
              JPEG, PNG or PDF, up to 10 MB.
            </span>
          </div>
          {candidates.length > 0 && (
            <div className="field">
              <label htmlFor={`${id}-reuse`}>Reuse a verified {item.document_type_name}</label>
              <div className="inline">
                <select id={`${id}-reuse`} value={reuseId} onChange={(e) => setReuseId(e.target.value)}>
                  <option value="">Choose a document</option>
                  {candidates.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.filename} (verified {formatDate(d.verified_at)})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn"
                  disabled={!reuseId || linking}
                  onClick={() => {
                    onReuse(Number(reuseId))
                    setReuseId('')
                  }}
                >
                  {linking ? 'Adding...' : 'Use this document'}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  )
}
