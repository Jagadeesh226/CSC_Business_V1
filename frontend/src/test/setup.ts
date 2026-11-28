import { afterEach } from 'vitest'
import { cleanup, configure } from '@testing-library/react'

// jsdom is slow when many test files run at once: wait longer before giving up.
configure({ asyncUtilTimeout: 4000 })

afterEach(() => {
  cleanup()
})
