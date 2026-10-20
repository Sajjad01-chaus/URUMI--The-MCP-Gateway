import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";

import {
  api,
  type ChatConversation,
  type ChatStreamEvent,
  type ChatToolCall,
  type PersistedChatMessage,
  type StopReason,
} from "../api";
import { useCurrentUser } from "../auth";
import { ErrorBanner, PageHeader, messageOf, useAction, useLoad } from "../ui";

interface Entry {
  id: string;
  role: "user" | "assistant";
  content: string;
  toolCalls: ChatToolCall[];
  stoppedReason: StopReason | null;
}

const STOP_NOTES: Partial<Record<StopReason, string>> = {
  max_iterations: "Stopped: step limit reached for this turn.",
  max_tool_calls: "Stopped: tool-call limit reached for this turn.",
  timeout: "Stopped: the turn took too long.",
  llm_error: "The language model could not answer.",
  malformed_tool_call: "Stopped: the model kept producing invalid tool calls.",
  error: "Stopped: internal error.",
};

function entry(message: PersistedChatMessage): Entry {
  return {
    id: String(message.id), role: message.role, content: message.content,
    toolCalls: message.tool_calls, stoppedReason: message.stopped_reason,
  };
}

export default function ChatPage() {
  const user = useCurrentUser();
  const { data: conversations, error: loadError, reload, setData: setConversations } = useLoad(api.chatConversations);
  const { busy, error, setError, run } = useAction();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [fresh, setFresh] = useState(false);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (fresh || selectedId || !conversations?.length) return;
    const id = conversations[0].id;
    setSelectedId(id);
    void api.chatConversation(id)
      .then((conversation) => setEntries(conversation.messages.map(entry)))
      .catch((cause) => setError(messageOf(cause)));
  }, [conversations, fresh, selectedId, setError]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [entries, busy]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 144)}px`;
  }, [draft]);

  const suggestions = [
    "What orders came in today?",
    "Convert 100 USD to EUR",
    user.role === "admin" ? "Create a product called Urumi Mug priced 12.50" : "List the products in the store",
  ];

  const openConversation = async (id: string) => {
    if (busy || id === selectedId) return;
    setFresh(false);
    setSelectedId(id);
    const conversation = await run(() => api.chatConversation(id));
    if (conversation) setEntries(conversation.messages.map(entry));
  };

  const newConversation = () => {
    if (busy) return;
    setFresh(true);
    setSelectedId(null);
    setEntries([]);
    setDraft("");
    setError(null);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  };

  const updateStreamingAssistant = (change: (current: Entry) => Entry) => {
    setEntries((current) => {
      const next = [...current];
      const last = next[next.length - 1];
      if (last?.role === "assistant" && last.id === "streaming") next[next.length - 1] = change(last);
      return next;
    });
  };

  const handleStream = (event: ChatStreamEvent) => {
    if (event.event === "conversation") {
      setConversations((current) => {
        const row: ChatConversation = {
          id: event.data.id, title: event.data.title,
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        };
        return [row, ...(current ?? []).filter((item) => item.id !== row.id)];
      });
    } else if (event.event === "tool") {
      updateStreamingAssistant((current) => ({ ...current, toolCalls: [...current.toolCalls, event.data] }));
    } else if (event.event === "delta") {
      updateStreamingAssistant((current) => ({ ...current, content: current.content + event.data.text }));
    } else if (event.event === "done") {
      updateStreamingAssistant((current) => ({
        ...current,
        id: event.data.message_id === null ? current.id : String(event.data.message_id),
        stoppedReason: event.data.stopped_reason,
      }));
    }
  };

  const send = async (text: string) => {
    const content = text.trim();
    if (!content || busy) return;
    setDraft("");
    const previous = entries;
    const completedId = await run(async () => {
      let id = selectedId;
      if (!id) {
        const created = await api.createChatConversation();
        id = created.id;
        setSelectedId(id);
        setFresh(false);
        setConversations((current) => [created, ...(current ?? [])]);
      }
      setEntries([
        ...previous,
        { id: `user-${Date.now()}`, role: "user", content, toolCalls: [], stoppedReason: null },
        { id: "streaming", role: "assistant", content: "", toolCalls: [], stoppedReason: null },
      ]);
      await api.streamChatTurn(id, content, handleStream);
      return id;
    });
    if (completedId) {
      await reload();
      return;
    }
    setDraft(content);
    if (selectedId) {
      try {
        const persisted = await api.chatConversation(selectedId);
        setEntries(persisted.messages.map(entry));
      } catch {
        setEntries(previous);
      }
    } else {
      setEntries(previous);
    }
  };

  const renameConversation = async (conversation: ChatConversation) => {
    const title = window.prompt("Conversation title", conversation.title)?.trim();
    if (!title || title === conversation.title) return;
    const updated = await run(() => api.renameChatConversation(conversation.id, title));
    if (updated) setConversations((current) => current?.map((row) => row.id === updated.id ? updated : row));
  };

  const deleteConversation = async (conversation: ChatConversation) => {
    if (!window.confirm(`Delete “${conversation.title}”? This cannot be undone.`)) return;
    const removed = await run(async () => {
      await api.deleteChatConversation(conversation.id);
      return true;
    });
    if (!removed) return;
    setConversations((current) => current?.filter((row) => row.id !== conversation.id));
    if (selectedId === conversation.id) newConversation();
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send(draft);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send(draft);
    }
  };

  return (
    <div className="chat-page">
      <PageHeader
        title="Chat"
        subtitle={`Persistent conversations; tools run as you (${user.role}) and remain visible in the audit log.`}
        actions={<button className="button button-primary" disabled={busy} onClick={newConversation}>New chat</button>}
      />

      <ErrorBanner message={error ?? loadError} />
      <div className="chat-workspace card">
        <aside className="chat-history" aria-label="Conversation history">
          <div className="chat-history-title">Conversations</div>
          {conversations?.map((conversation) => (
            <div key={conversation.id} className={`chat-history-row ${selectedId === conversation.id ? "active" : ""}`}>
              <button className="chat-history-open" onClick={() => void openConversation(conversation.id)}>
                <span>{conversation.title}</span>
                <small>{new Date(conversation.updated_at).toLocaleDateString()}</small>
              </button>
              <div className="chat-history-actions">
                <button className="icon-button" aria-label={`Rename ${conversation.title}`} onClick={() => void renameConversation(conversation)}>✎</button>
                <button className="icon-button" aria-label={`Delete ${conversation.title}`} onClick={() => void deleteConversation(conversation)}>×</button>
              </div>
            </div>
          ))}
          {conversations?.length === 0 && <div className="small muted chat-history-empty">No saved conversations yet.</div>}
        </aside>

        <section className="chat-main">
          <div className="chat-log">
            {entries.length === 0 && (
              <div className="empty chat-empty">
                <p>Ask about orders, products, notes or currencies.</p>
                <div className="chips" style={{ justifyContent: "center" }}>
                  {suggestions.map((suggestion) => (
                    <button key={suggestion} className="button button-small" onClick={() => void send(suggestion)}>{suggestion}</button>
                  ))}
                </div>
              </div>
            )}
            {entries.map((item) => (
              <div key={item.id} className={`chat-entry chat-${item.role}`}>
                {item.toolCalls.length > 0 && (
                  <div className="tool-calls">
                    {item.toolCalls.map((call, index) => <ToolCallChip key={`${call.name}-${index}`} call={call} />)}
                  </div>
                )}
                <div className="bubble">
                  {item.role === "assistant"
                    ? item.content ? renderLite(item.content) : <span className="muted">Working…</span>
                    : item.content}
                </div>
                {item.stoppedReason && STOP_NOTES[item.stoppedReason] && (
                  <div className="cell-sub">{STOP_NOTES[item.stoppedReason]}</div>
                )}
              </div>
            ))}
            <div ref={endRef} />
          </div>

          <form className="chat-input" onSubmit={submit}>
            <textarea
              ref={inputRef}
              rows={1}
              maxLength={4000}
              placeholder="Message the assistant"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              aria-label="Chat message"
            />
            <button className="button button-primary" disabled={busy || !draft.trim()}>Send</button>
            <div className="chat-input-hint">Enter to send · Shift+Enter for a new line</div>
          </form>
        </section>
      </div>
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

function renderLite(text: string): ReactNode[] {
  return text.split(/```(?:[a-z]*\n)?([\s\S]*?)```/g).map((part, index) =>
    index % 2 === 1
      ? <pre key={index} className="answer-code">{part.replace(/\n$/, "")}</pre>
      : <span key={index}>{withTables(part)}</span>,
  );
}

const TABLE_SEPARATOR = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function withTables(text: string): ReactNode[] {
  const lines = text.split("\n");
  const out: ReactNode[] = [];
  let buffer: string[] = [];
  const flush = () => {
    if (buffer.length) out.push(<span key={out.length}>{inline(buffer.join("\n"))}</span>);
    buffer = [];
  };
  for (let index = 0; index < lines.length; index++) {
    if (lines[index].trim().startsWith("|") && index + 1 < lines.length && TABLE_SEPARATOR.test(lines[index + 1])) {
      const header = cells(lines[index]);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) rows.push(cells(lines[index++]));
      index--;
      flush();
      out.push(
        <div key={out.length} className="answer-table-wrap">
          <table className="answer-table">
            <thead><tr>{header.map((heading, cellIndex) => <th key={cellIndex}>{inline(heading)}</th>)}</tr></thead>
            <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{header.map((_, cellIndex) => <td key={cellIndex}>{inline(row[cellIndex] ?? "")}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
    } else {
      buffer.push(lines[index]);
    }
  }
  flush();
  return out;
}

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={index}>{part.slice(1, -1)}</code>;
    return part;
  });
}
