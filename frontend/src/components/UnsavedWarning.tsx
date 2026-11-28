import { useEffect, useRef } from 'react'
import { useBlocker } from 'react-router-dom'

/**
 * Warns before the person leaves the page while `dirty`: the browser's own
 * prompt for closing or reloading the tab, and an in-app dialog for links.
 */
export function UnsavedWarning({ dirty, message }: { dirty: boolean; message: string }) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname,
  )
  const stayRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const blocked = blocker.state === 'blocked'
  useEffect(() => {
    if (blocked) stayRef.current?.focus()
  }, [blocked])

  // Nothing is pending any more: let the navigation through.
  useEffect(() => {
    if (!dirty && blocker.state === 'blocked') blocker.proceed()
  }, [dirty, blocker])

  if (!blocked) return null
  return (
    <div className="dialog-backdrop">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="leave-title"
        aria-describedby="leave-text"
        className="dialog card"
        onKeyDown={(e) => {
          if (e.key === 'Escape') blocker.reset()
        }}
      >
        <h2 id="leave-title">Leave this page?</h2>
        <p id="leave-text">{message}</p>
        <div className="actions">
          <button type="button" className="btn btn-primary" ref={stayRef} onClick={() => blocker.reset()}>
            Stay on this page
          </button>
          <button type="button" className="btn" onClick={() => blocker.proceed()}>
            Leave without saving
          </button>
        </div>
      </div>
    </div>
  )
}
