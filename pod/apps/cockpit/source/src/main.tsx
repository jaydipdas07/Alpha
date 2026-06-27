import React from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthGuard } from 'lemma-sdk/react'
import { lemmaClient } from './lemma-client'
import { App } from './App'
import './styles.css'

// One QueryClient for the whole app. The lemma-sdk/react hooks are TanStack-Query
// hooks: they fetch once, cache + dedupe reads, and auto-refresh matching lists on
// a mutation — so views never hand-wire refetches or poll. Create it once, here.
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
})

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* AuthGuard gates auth exactly once: if <App/> renders, we are the
          signed-in pod user and every SDK call runs under their grants (RLS). */}
      <AuthGuard client={lemmaClient} loadingFallback={<div className="boot">authenticating…</div>}>
        <App />
      </AuthGuard>
    </QueryClientProvider>
  </React.StrictMode>,
)
