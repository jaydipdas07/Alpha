import { LemmaClient } from 'lemma-sdk'

// Shared Lemma client for this app. Runtime config comes from .env.local
// (VITE_LEMMA_API_URL / VITE_LEMMA_AUTH_URL / VITE_LEMMA_POD_ID), which
// `lemma apps init` writes for you. import.meta.env is typed via
// "vite/client" in tsconfig.json.
export const lemmaClient = new LemmaClient({
  apiUrl: import.meta.env.VITE_LEMMA_API_URL,
  authUrl: import.meta.env.VITE_LEMMA_AUTH_URL,
  podId: import.meta.env.VITE_LEMMA_POD_ID,
})
