import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";

import { api, type ChatMessage, type ChatToolCall, type StopReason } from "../api";
import { useCurrentUser } from "../auth";
import { ErrorBanner, PageHeader, useAction } from "../ui";

interface Entry {
  role: "user" | "assistant";
  content: string;
  toolCalls?: ChatToolCall[];
  stoppedReason?: StopReason;
}

// The server is stateless: only the last turns are sent back as context (keeps LLM token use low).
const HISTORY_SENT = 20;
const STORAGE_KEY = "urumi.chat";

const STOP_NOTES: Partial<Record<StopReason, string>> = {
  max_iterations: "Stopped: step limit reached for this turn.",
  max_tool_calls: "Stopped: tool-call limit reached for this turn.",
  timeout: "Stopped: the turn took too long.",
  llm_error: "The language model could not answer.",
  malformed_tool_call: "Stopped: the model kept producing invalid tool calls.",
  error: "Stopped: internal error.",
};

function loadEntries(userId: string): Entry[] {
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "null") as { userId: string; entries: Entry[] } | null;
    return saved?.userId === userId ? saved.entries : [];
  } catch {
    return [];
  }
}

function saveEntries(userId: string, entries: Entry[]): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ userId, entries }));
  } catch {
    // Storage can be full or blocked; the conversation still works for this page view.
  }
}

export default function ChatPage() {
  const user = useCurrentUser();
  const [entries, setEntries] = useState<Entry[]>(() => loadEntries(user.id));
  const [draft, setDraft] = useState("");
  const { busy, error, run } = useAction();
  const endRef = useRef<HTMLDivElement>(null);

  // Block bodies on purpose: an effect must return nothing or a clean-up function, and
  // scrollIntoView() returns a Promise in newer browsers, which React would try to call on unmount.
  useEffect(() => {
    saveEntries(user.id, entries);
  }, [user.id, entries]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [entries, busy]);

  const suggestions = [
    "What orders came in today?",
    "Convert 100 USD to EUR",
    user.role === "admin" ? "Create a product called Urumi Mug priced 12.50" : "List the products in the store",
  ];

  const send = async (text: string) => {
    const content = text.trim();
    if (!content || busy) return;
    const next: Entry[] = [...entries, { role: "user", content }];
    setEntries(next);
    setDraft("");
    const history: ChatMessage[] = next.slice(-HISTORY_SENT).map(({ role, content }) => ({ role, content }));
    const turn = await run(() => api.chat(history));
    if (turn) {
      setEntries([...next, { role: "assistant", content: turn.answer, toolCalls: turn.tool_calls, stoppedReason: turn.stopped_reason }]);
    } else {
      // The request failed (not configured, busy, network): don't leave an unanswered question in the
      // history, or the model answers it later out of context. Put it back in the input to retry.
      setEntries(entries);
      setDraft(content);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send(draft);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send(draft);
    }
  };

  return (
    <div className="chat-page">
      <PageHeader
        title="Chat"
        subtitle={`The assistant calls gateway tools as you (${user.role}); every call is in the audit log with source "chat".`}
        actions={entries.length > 0 && (
          <button className="button" disabled={busy} onClick={() => setEntries([])}>New chat</button>
        )}
      />

      <div className="chat-log card">
        {entries.length === 0 && (
          <div className="empty">
            <p>Ask about orders, products, notes or currencies.</p>
            <div className="chips" style={{ justifyContent: "center" }}>
              {suggestions.map((s) => (
                <button key={s} className="button button-small" onClick={() => void send(s)}>{s}</button>
              ))}
            </div>
          </div>
        )}
        {entries.map((entry, i) => (
          <div key={i} className={`chat-entry chat-${entry.role}`}>
            {entry.toolCalls && entry.toolCalls.length > 0 && (
              <div className="tool-calls">
                {entry.toolCalls.map((call, j) => <ToolCallChip key={j} call={call} />)}
              </div>
            )}
            <div className="bubble">{entry.role === "assistant" ? renderLite(entry.content) : entry.content}</div>
            {entry.stoppedReason && STOP_NOTES[entry.stoppedReason] && (
              <div className="cell-sub">{STOP_NOTES[entry.stoppedReason]}</div>
            )}
          </div>
        ))}
        {busy && (
          <div className="chat-entry chat-assistant">
            <div className="bubble muted">Working… (calling tools as needed)</div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <ErrorBanner message={error} />
      <form className="chat-input" onSubmit={submit}>
        <textarea
          rows={2}
          maxLength={4000}
          placeholder="Message the assistant (Enter to send, Shift+Enter for a new line)"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          aria-label="Chat message"
        />
        <button className="button button-primary" disabled={busy || !draft.trim()}>Send</button>
      </form>
    </div>
  );
}

function ToolCallChip({ call }: { call: ChatToolCall }) {
  return (
    <details className={`tool-call ${call.is_error ? "tool-call-error" : ""}`}>
      <summary>
        <span className="mono">{call.name}</span>
        <span className="muted small"> · {call.duration_ms} ms</span>
        {call.is_error && <span className="badge badge-red" style={{ marginLeft: 6 }}>error</span>}
      </summary>
      <div className="tool-call-body">
        <div className="small muted">Arguments</div>
        <pre>{call.arguments ? JSON.stringify(call.arguments, null, 2) : "(invalid)"}</pre>
        <div className="small muted">Result</div>
        <pre>{call.result}</pre>
      </div>
    </details>
  );
}

/**
 * Minimal Markdown for model answers: ```code blocks```, | tables |, **bold** and `code`.
 * Builds React elements (never innerHTML), so text from tools can't inject HTML or scripts.
 */
function renderLite(text: string): ReactNode[] {
  return text.split(/```(?:[a-z]*\n)?([\s\S]*?)```/g).map((part, i) =>
    i % 2 === 1 ? <pre key={i} className="answer-code">{part.replace(/\n$/, "")}</pre> : <span key={i}>{withTables(part)}</span>,
  );
}

const TABLE_SEPARATOR = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

// A table is a header row, a |---| separator row, then rows starting with "|"; everything else stays text.
function withTables(text: string): ReactNode[] {
  const lines = text.split("\n");
  const out: ReactNode[] = [];
  let buffer: string[] = [];
  const flush = () => {
    if (buffer.length) out.push(<span key={out.length}>{inline(buffer.join("\n"))}</span>);
    buffer = [];
  };
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim().startsWith("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])) {
      const header = cells(lines[i]);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      i--;
      flush();
      out.push(
        <div key={out.length} className="answer-table-wrap">
          <table className="answer-table">
            <thead><tr>{header.map((h, j) => <th key={j}>{inline(h)}</th>)}</tr></thead>
            <tbody>{rows.map((r, k) => <tr key={k}>{header.map((_, j) => <td key={j}>{inline(r[j] ?? "")}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
    } else {
      buffer.push(lines[i]);
    }
  }
  flush();
  return out;
}

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={i}>{part.slice(1, -1)}</code>;
    return part;
  });
}
