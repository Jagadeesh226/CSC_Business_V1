import { describe, expect, it } from 'vitest'
import { safeReturnPath } from './useAuth'

describe('safeReturnPath', () => {
  it.each([
    '//evil.example',
    '/\\evil.example',
    '/\\/evil.example',
    'https://evil.example',
    'javascript:alert(1)',
    'jobs',
    '',
    '/ok\nnext',
    '/a\\b',
  ])('rejects %j', (value) => {
    expect(safeReturnPath(value)).toBe('/')
  })

  it.each([undefined, null, 5, {}])('rejects non-string %j', (value) => {
    expect(safeReturnPath(value)).toBe('/')
  })

  it.each(['/', '/jobs', '/jobs/12?tab=docs#top', '/customers/3'])('keeps %s', (value) => {
    expect(safeReturnPath(value)).toBe(value)
  })
})
