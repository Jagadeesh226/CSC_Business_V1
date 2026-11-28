import type { paths } from '../api/schema'

/** Typed access to the backend. All requests in the app go through here. */

export const SESSION_ENDED_MESSAGE = 'Your session has ended. Please sign in again.'
export const PASSWORD_CHANGE_DETAIL = 'You must change your password first.'
export const NETWORK_MESSAGE = "Can't reach the server. Check your connection."
const FORBIDDEN_MESSAGE = "You don't have access to this."
export const CSRF_DETAIL = 'Missing or invalid CSRF token.'
export const CSRF_MESSAGE = 'Your session needs refreshing. Please sign in again.'
const GENERIC_MESSAGE = 'Something went wrong. Please try again.'
const CSRF_COOKIE = 'csc_csrf'

export type ApiErrorKind =
  | 'network'
  | 'session_ended'
  | 'password_change_required'
  | 'forbidden'
  | 'validation'
  | 'http'

export class ApiError extends Error {
  readonly status: number // 0 when the server could not be reached
  readonly kind: ApiErrorKind
  /** Field name -> message, filled for 422 responses where the field is known. */
  readonly fieldErrors: Record<string, string>

  constructor(
    kind: ApiErrorKind,
    status: number,
    message: string,
    fieldErrors: Record<string, string> = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.fieldErrors = fieldErrors
  }
}

interface Handlers {
  onSessionEnded?: (message: string) => void
  onPasswordChangeRequired?: () => void
}
let handlers: Handlers = {}

/** The app registers what should happen on 401 and on "change your password first". */
export function setApiHandlers(next: Handlers): void {
  handlers = next
}

// ---- types derived from the generated contract ----
type JsonOf<T> = T extends { content: { 'application/json': infer J } } ? J : void
type OpOf<P extends keyof paths, M extends string> = paths[P] extends Record<M, infer O> ? O : never
type OkOf<O> = O extends { responses: infer R }
  ? R extends { 200: infer A }
    ? JsonOf<A>
    : R extends { 201: infer A }
      ? JsonOf<A>
      : void
  : never
type QueryOf<O> = O extends { parameters: { query?: infer Q } } ? Q : never
type PathParamsOf<O> = O extends { parameters: { path: infer P } } ? P : never
type BodyOf<O> = O extends { requestBody: { content: { 'application/json': infer B } } } ? B : never

export type GetPath = { [P in keyof paths]: paths[P] extends { get: unknown } ? P : never }[keyof paths]
export type PostPath = { [P in keyof paths]: paths[P] extends { post: unknown } ? P : never }[keyof paths]
export type PatchPath = { [P in keyof paths]: paths[P] extends { patch: unknown } ? P : never }[keyof paths]
export type DeletePath = { [P in keyof paths]: paths[P] extends { delete: unknown } ? P : never }[keyof paths]
export type PatchBody<P extends PatchPath> = BodyOf<OpOf<P, 'patch'>>
export type GetResponse<P extends GetPath> = OkOf<OpOf<P, 'get'>>
export type GetQuery<P extends GetPath> = QueryOf<OpOf<P, 'get'>>
export type PostBody<P extends PostPath> = BodyOf<OpOf<P, 'post'>>
export type PostResponse<P extends PostPath> = OkOf<OpOf<P, 'post'>>

/** Path parameters are required exactly when the contract has them. */
type OptsArg<O, Extra> = [PathParamsOf<O>] extends [never]
  ? [opts?: Extra & { params?: undefined }]
  : [opts: Extra & { params: PathParamsOf<O> }]

interface RequestOptions {
  query?: Record<string, unknown>
  body?: unknown
  /** Multipart body (file uploads). The browser sets the content type and boundary. */
  form?: FormData
  /** Values for {placeholders} in the path. */
  params?: Record<string, string | number>
  /**
   * For sign-in and "who am I": a 401 here is an ordinary answer, not an
   * ended session, so no redirect happens and the server's text is kept.
   */
  public?: boolean
  signal?: AbortSignal
}

function baseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL ?? ''
}

export function readCsrfToken(): string | null {
  for (const part of document.cookie.split(';')) {
    const [name, ...rest] = part.trim().split('=')
    if (name === CSRF_COOKIE) return decodeURIComponent(rest.join('='))
  }
  return null
}

function buildUrl(path: string, opts: RequestOptions): string {
  let url = path.replace(/\{(\w+)\}/g, (_m, key: string) => {
    const value = opts.params?.[key]
    if (value === undefined || value === null || value === '') {
      throw new Error(`Missing path parameter "${key}" for ${path}`)
    }
    return encodeURIComponent(String(value))
  })
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(opts.query ?? {})) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const qs = search.toString()
  if (qs) url += `?${qs}`
  return baseUrl() + url
}

interface ValidationItem {
  loc?: unknown
  msg?: unknown
}

function mapValidation(detail: unknown): { message: string; fields: Record<string, string> } {
  if (typeof detail === 'string') return { message: detail, fields: {} }
  const fields: Record<string, string> = {}
  let first = ''
  if (Array.isArray(detail)) {
    for (const raw of detail as ValidationItem[]) {
      const msg = typeof raw?.msg === 'string' ? raw.msg : ''
      if (!msg) continue
      first ||= msg
      const loc = Array.isArray(raw.loc) ? raw.loc : []
      const field = loc.filter((p) => p !== 'body' && p !== 'query').join('.')
      if (field && !(field in fields)) fields[field] = msg
    }
  }
  return { message: first || 'Please check the details and try again.', fields }
}

async function readDetail(res: Response): Promise<unknown> {
  try {
    const data: unknown = await res.json()
    if (data && typeof data === 'object' && 'detail' in data) return (data as { detail: unknown }).detail
  } catch {
    // Not JSON (for example an HTML error page): never show it.
  }
  return undefined
}

async function toError(res: Response, opts: RequestOptions): Promise<ApiError> {
  const detail = await readDetail(res)
  const text = typeof detail === 'string' && detail.trim() ? detail : undefined
  const { status } = res

  // A proxy with nothing behind it answers 502/503/504 without our JSON body.
  if ((status === 502 || status === 503 || status === 504) && detail === undefined) {
    return new ApiError('network', status, NETWORK_MESSAGE)
  }
  if (status === 401) {
    if (opts.public) return new ApiError('http', 401, text ?? 'Sign in failed.')
    return new ApiError('session_ended', 401, SESSION_ENDED_MESSAGE)
  }
  if (status === 403) {
    if (text === CSRF_DETAIL) return new ApiError('session_ended', 403, CSRF_MESSAGE)
    if (text === PASSWORD_CHANGE_DETAIL) return new ApiError('password_change_required', 403, text)
    return new ApiError('forbidden', 403, FORBIDDEN_MESSAGE)
  }
  if (status === 404 || status === 409 || status === 410) {
    return new ApiError('http', status, text ?? GENERIC_MESSAGE)
  }
  if (status === 422) {
    const { message, fields } = mapValidation(detail)
    return new ApiError('validation', 422, message, fields)
  }
  return new ApiError('http', status, text ?? GENERIC_MESSAGE)
}

async function request(method: string, path: string, opts: RequestOptions = {}): Promise<unknown> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (opts.body !== undefined && !opts.form) headers['Content-Type'] = 'application/json'
  if (method !== 'GET') {
    const csrf = readCsrfToken()
    if (csrf) headers['X-CSRF-Token'] = csrf
  }

  const url = buildUrl(path, opts) // throws on a missing path parameter, before any request
  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers,
      credentials: 'include',
      body: opts.form ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
      signal: opts.signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError('network', 0, NETWORK_MESSAGE)
  }

  if (!res.ok) {
    const error = await toError(res, opts)
    if (!opts.public) {
      if (error.kind === 'session_ended') handlers.onSessionEnded?.(error.message)
      if (error.kind === 'password_change_required') handlers.onPasswordChangeRequired?.()
    }
    throw error
  }
  if (res.status === 204) return undefined
  try {
    return await res.json()
  } catch {
    return undefined
  }
}

export const api = {
  get<P extends GetPath>(
    path: P,
    ...args: OptsArg<OpOf<P, 'get'>, Omit<RequestOptions, 'body' | 'query' | 'params'> & { query?: GetQuery<P> }>
  ): Promise<GetResponse<P>> {
    return request('GET', path, (args[0] ?? {}) as RequestOptions) as Promise<GetResponse<P>>
  },
  post<P extends PostPath>(
    path: P,
    ...args: OptsArg<OpOf<P, 'post'>, Omit<RequestOptions, 'body' | 'params'> & { body?: PostBody<P> }>
  ): Promise<PostResponse<P>> {
    return request('POST', path, (args[0] ?? {}) as RequestOptions) as Promise<PostResponse<P>>
  },
  /** multipart/form-data upload (the file field is added by the caller). */
  postForm<P extends PostPath>(
    path: P,
    ...args: OptsArg<OpOf<P, 'post'>, Omit<RequestOptions, 'body' | 'params' | 'form'> & { form: FormData }>
  ): Promise<PostResponse<P>> {
    return request('POST', path, args[0] as RequestOptions) as Promise<PostResponse<P>>
  },
  patch<P extends PatchPath>(
    path: P,
    ...args: OptsArg<OpOf<P, 'patch'>, Omit<RequestOptions, 'body' | 'params'> & { body?: PatchBody<P> }>
  ): Promise<OkOf<OpOf<P, 'patch'>>> {
    return request('PATCH', path, (args[0] ?? {}) as RequestOptions) as Promise<OkOf<OpOf<P, 'patch'>>>
  },
  delete<P extends DeletePath>(
    path: P,
    ...args: OptsArg<OpOf<P, 'delete'>, Omit<RequestOptions, 'body' | 'params'>>
  ): Promise<OkOf<OpOf<P, 'delete'>>> {
    return request('DELETE', path, (args[0] ?? {}) as RequestOptions) as Promise<OkOf<OpOf<P, 'delete'>>>
  },
}
