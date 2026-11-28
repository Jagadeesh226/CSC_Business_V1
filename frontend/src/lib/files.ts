export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024
export const ACCEPT = '.pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg'
const TYPES = new Set(['application/pdf', 'image/png', 'image/jpeg'])
const EXTENSIONS = /\.(pdf|png|jpe?g)$/i

/** Returns a message when the file cannot be uploaded, otherwise null. */
export function validateUpload(file: File): string | null {
  const typeOk = file.type ? TYPES.has(file.type) : EXTENSIONS.test(file.name)
  if (!typeOk || !EXTENSIONS.test(file.name)) {
    return `"${file.name}" is not allowed. Choose a JPEG, PNG or PDF file.`
  }
  if (file.size === 0) return `"${file.name}" is empty.`
  if (file.size > MAX_UPLOAD_BYTES) {
    return `"${file.name}" is larger than 10 MB. Choose a smaller file.`
  }
  return null
}
