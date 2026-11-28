import { describe, expect, it } from 'vitest'
import { MAX_UPLOAD_BYTES, validateUpload } from './files'

const file = (name: string, type: string, size = 10) => new File([new Uint8Array(size)], name, { type })

describe('validateUpload', () => {
  it('accepts JPEG, PNG and PDF up to 10 MB', () => {
    expect(validateUpload(file('a.pdf', 'application/pdf'))).toBeNull()
    expect(validateUpload(file('a.PNG', 'image/png'))).toBeNull()
    expect(validateUpload(file('a.jpeg', 'image/jpeg'))).toBeNull()
    expect(validateUpload(file('a.pdf', 'application/pdf', MAX_UPLOAD_BYTES))).toBeNull()
  })
  it('refuses other types, wrong extensions, empty and oversized files', () => {
    expect(validateUpload(file('a.txt', 'text/plain'))).toMatch(/not allowed/)
    expect(validateUpload(file('a.exe', 'application/pdf'))).toMatch(/not allowed/)
    expect(validateUpload(file('a.pdf', 'application/pdf', 0))).toMatch(/empty/)
    expect(validateUpload(file('a.pdf', 'application/pdf', MAX_UPLOAD_BYTES + 1))).toMatch(/10 MB/)
  })
})
