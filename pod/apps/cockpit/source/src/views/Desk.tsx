import { useEffect, useRef, useState } from 'react'
import { Sparkles, Send, Bot, User } from 'lucide-react'
import { AgentThread } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { errMessage } from '../lib'
import { EmptyState } from '../ui'

// M2.5 — the desk copilot, in the cockpit. A multi-turn chat with the `desk` pod
// agent (reads the tables + /knowledge; never trades, never sees the holdout).
// Until the desk agent is deployed to the pod, this shows a clear unavailable
// state. The agent is the single source of truth for what desk can do (TEST-8).

const DESK_AGENT = 'desk'

interface Msg {
  id?: string
  role?: string
  kind?: string
  text?: string
}

// Structural subset of the AgentThread render-prop result we use (avoids pinning
// the SDK's full type; assignable from UseConversationMessagesResult).
interface Thread {
  messages: unknown[]
  sendMessage: (content: string) => Promise<unknown>
  isRunning: boolean
  isStreaming: boolean
  streamingText: string
  error: Error | null
}

export function Desk() {
  return (
    <AgentThread client={lemmaClient} podId={lemmaClient.podId} agentName={DESK_AGENT}>
      {(thread) => <DeskChat thread={thread} />}
    </AgentThread>
  )
}

function DeskChat({ thread }: { thread: Thread }) {
  const [input, setInput] = useState('')
  const logRef = useRef<HTMLDivElement>(null)
  // One turn emits several assistant messages by kind; show the user's messages
  // and the assistant's final `TEXT` answers (skip THINKING/TOOL chatter). Kind is
  // UPPERCASE in the SDK; role is lowercase.
  const visible = (thread.messages as Msg[]).filter(
    (m) => m.role === 'user' || (m.role === 'assistant' && m.kind === 'TEXT'),
  )

  // Follow the conversation: scroll to newest on new messages / streaming.
  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [visible.length, thread.streamingText, thread.isStreaming])

  function send() {
    const t = input.trim()
    if (!t || thread.isRunning) return
    setInput('')
    thread.sendMessage(t).catch(() => setInput(t)) // restore the text if the send fails
  }

  return (
    <div className="desk">
      <div className="desk-log" ref={logRef}>
        {thread.error ? (
          <div className="alert">
            Desk is unavailable: {errMessage(thread.error)}. The <code>desk</code> agent + its grants must
            be deployed to the pod first (operator-authorized: <code>lemma pods import pod/</code>).
          </div>
        ) : visible.length === 0 && !thread.isStreaming ? (
          <EmptyState
            icon={Sparkles}
            head="Ask desk"
            sub="Your mission-control copilot — ask about strategies, backtests, discovery runs, risk, or how the rigor gate works. Desk reads the pod tables + the /knowledge corpus; it never trades and never sees the holdout."
            hint="e.g. “which backtests were rejected, and why?”"
          />
        ) : (
          <>
            {visible.map((m, i) => (
              <div key={m.id ?? i} className={`bubble ${m.role === 'user' ? 'me' : 'agent'}`}>
                <span className="bubble-who">
                  {m.role === 'user' ? <User size={13} /> : <Bot size={13} />}
                </span>
                <span className="bubble-text">{m.text ?? ''}</span>
              </div>
            ))}
            {thread.isStreaming ? (
              <div className="bubble agent">
                <span className="bubble-who">
                  <Bot size={13} />
                </span>
                <span className="bubble-text">{thread.streamingText || '…'}</span>
              </div>
            ) : null}
          </>
        )}
      </div>

      <form
        className="desk-composer"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask desk about strategies, backtests, risk…"
          aria-label="Message desk"
        />
        <button type="submit" className="btn ok" disabled={thread.isRunning || !input.trim()}>
          <Send size={15} /> Send
        </button>
      </form>
    </div>
  )
}
