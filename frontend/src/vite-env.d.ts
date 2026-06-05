/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_CF_ACCESS_CLIENT_ID?: string;
  readonly VITE_CF_ACCESS_CLIENT_SECRET?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
