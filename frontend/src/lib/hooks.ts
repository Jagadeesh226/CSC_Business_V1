import { useEffect, useState } from 'react'

/** Value that follows `value` after `ms` of quiet (for search-as-you-type). */
export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

export const debounceConfig = { ms: 300 }
