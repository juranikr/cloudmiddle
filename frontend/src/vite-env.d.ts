/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_RUNTIME_LABEL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
