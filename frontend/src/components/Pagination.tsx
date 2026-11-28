export function Pagination({
  page,
  totalPages,
  total,
  onPage,
  label = 'Pages',
}: {
  page: number
  totalPages: number
  total: number
  onPage: (page: number) => void
  label?: string
}) {
  if (total === 0) return null
  return (
    <nav className="pager" aria-label={label}>
      <button type="button" className="btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span aria-live="polite">
        Page {page} of {Math.max(totalPages, 1)} ({total} {total === 1 ? 'result' : 'results'})
      </span>
      <button type="button" className="btn" disabled={page >= totalPages} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </nav>
  )
}
