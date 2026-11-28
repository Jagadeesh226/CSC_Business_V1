/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Empty or unset means same origin (the default). Never put secrets here. */
  readonly VITE_API_BASE_URL?: string
  /** Display name shown in the header. */
  readonly VITE_CENTRE_NAME?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
