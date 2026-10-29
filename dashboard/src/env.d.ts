/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API origin, optionally with a path prefix; empty for same origin. Public, not a secret. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
