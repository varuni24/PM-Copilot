"use client";

import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Source = { name: string; detail?: string };
type Message = { role: "user" | "assistant"; content: string; sources?: Source[] };
type Conversation = { id: number; title: string; created_at: string; updated_at: string };
type ConversationDetail = Conversation & { messages: Message[] };
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const examples = [
  "What happened this week?",
  "What tasks are overdue?",
  "Summarize recent Slack updates",
  "What meetings do I have tomorrow?",
  "Find project files in Drive",
];

type IconName = "spark" | "plus" | "send" | "menu" | "close" | "chevron" | "sun" | "moon";

function Icon({ name }: { name: IconName }) {
  const paths = {
    spark: <path d="M12 3l1.15 4.05A5.4 5.4 0 0 0 16.95 11L21 12l-4.05 1.05A5.4 5.4 0 0 0 13.15 17L12 21l-1.15-4.05A5.4 5.4 0 0 0 7.05 13L3 12l4.05-1A5.4 5.4 0 0 0 10.85 7L12 3Z" />,
    plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
    send: <><path d="m4 4 16 8-16 8 3-8-3-8Z" /><path d="M7 12h13" /></>,
    menu: <><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h16" /></>,
    close: <><path d="m6 6 12 12" /><path d="M18 6 6 18" /></>,
    chevron: <path d="m9 18 6-6-6-6" />,
    sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"/></>,
    moon: <path d="M20.5 14.4A8 8 0 0 1 9.6 3.5 8.5 8.5 0 1 0 20.5 14.4Z" />,
  };
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function Markdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: (props) => <a {...props} target="_blank" rel="noreferrer" /> }}>{children}</ReactMarkdown>;
}

function readSources(value: unknown): Source[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const sources = value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    if (typeof record.name !== "string") return [];
    return [{ name: record.name, detail: typeof record.detail === "string" ? record.detail : undefined }];
  });
  return sources.length ? sources : undefined;
}

function relativeTime(value: string) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return "Just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return days < 7 ? `${days}d ago` : new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [openMenuId, setOpenMenuId] = useState<number | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, loading]);
  useEffect(() => {
    const saved = window.localStorage.getItem("pm-copilot-theme");
    const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    setTheme(saved === "dark" || saved === "light" ? saved : preferred);
  }, []);
  useEffect(() => {
    async function restoreConversations() {
      try {
        const response = await fetch(`${API_URL}/conversations`);
        if (!response.ok) throw new Error();
        const history = await response.json() as Conversation[];
        setConversations(history);
        const savedId = Number(window.localStorage.getItem("pm-copilot-conversation"));
        if (savedId && history.some((item) => item.id === savedId)) await openConversation(savedId);
      } catch { setError("Conversation history could not be loaded. You can still start a new chat."); }
      finally { setHistoryLoading(false); }
    }
    void restoreConversations();
  // Run once when the application opens.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function refreshConversations() {
    const response = await fetch(`${API_URL}/conversations`);
    if (response.ok) setConversations(await response.json() as Conversation[]);
  }

  async function openConversation(id: number) {
    if (loading) return;
    setError(""); setHistoryLoading(true); setOpenMenuId(null);
    try {
      const response = await fetch(`${API_URL}/conversations/${id}`);
      if (!response.ok) throw new Error();
      const conversation = await response.json() as ConversationDetail;
      setConversationId(id); setMessages(conversation.messages); setSidebarOpen(false);
      window.localStorage.setItem("pm-copilot-conversation", String(id));
    } catch { setError("That conversation could not be loaded. Please try again."); }
    finally { setHistoryLoading(false); }
  }

  async function renameConversation(conversation: Conversation) {
    setOpenMenuId(null);
    const title = window.prompt("Rename conversation", conversation.title)?.trim();
    if (!title || title === conversation.title) return;
    try {
      const response = await fetch(`${API_URL}/conversations/${conversation.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      if (!response.ok) throw new Error();
      const updated = await response.json() as Conversation;
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
      await refreshConversations();
    } catch { setError("The conversation could not be renamed. Please try again."); }
  }

  async function deleteConversation(id: number) {
    setOpenMenuId(null);
    if (!window.confirm("Delete this conversation?")) return;
    try {
      const response = await fetch(`${API_URL}/conversations/${id}`, { method: "DELETE" });
      if (!response.ok) throw new Error();
      setConversations((current) => current.filter((item) => item.id !== id));
      if (id === conversationId) {
        setConversationId(null); setMessages([]); setDraft("");
        window.localStorage.removeItem("pm-copilot-conversation");
      }
    } catch { setError("The conversation could not be deleted. Please try again."); }
  }

  function toggleTheme() {
    setTheme((current) => {
      const next = current === "light" ? "dark" : "light";
      window.localStorage.setItem("pm-copilot-theme", next);
      return next;
    });
  }

  async function submit(content: string) {
    const cleanContent = content.trim();
    if (!cleanContent || loading) return;
    const previousMessages = messages;
    const nextMessages: Message[] = [...previousMessages, { role: "user", content: cleanContent }];
    setMessages(nextMessages); setDraft(""); setError(""); setLoading(true); setSidebarOpen(false);
    try {
      const response = await fetch(`${API_URL}/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: nextMessages.map(({ role, content: text }) => ({ role, content: text })), conversation_id: conversationId }),
      });
      const data: unknown = await response.json().catch(() => null);
      const body = data && typeof data === "object" ? data as Record<string, unknown> : {};
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "The request could not be completed.");
      if (typeof body.reply !== "string") throw new Error("The Copilot returned an unexpected response.");
      if (typeof body.conversation_id === "number") {
        setConversationId(body.conversation_id);
        window.localStorage.setItem("pm-copilot-conversation", String(body.conversation_id));
      }
      setMessages([...nextMessages, { role: "assistant", content: body.reply, sources: readSources(body.sources) }]);
      await refreshConversations();
    } catch (caught) {
      const detail = caught instanceof Error ? caught.message : "The request could not be completed.";
      setError(detail.includes("fetch") || detail.includes("Failed") ? "We couldn’t connect to PM Copilot. Check that the backend is running, then try again." : detail);
      setMessages(previousMessages); setDraft(cleanContent);
    } finally { setLoading(false); inputRef.current?.focus(); }
  }

  function resetChat() { setConversationId(null); setOpenMenuId(null); setMessages([]); setDraft(""); setError(""); setSidebarOpen(false); window.localStorage.removeItem("pm-copilot-conversation"); inputRef.current?.focus(); }
  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
  }

  return (
    <main className="app-shell" data-theme={theme}>
      {sidebarOpen && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="sidebar-top">
          <div className="brand"><span className="brand-mark"><Icon name="spark" /></span><div><strong>PM Copilot</strong><span>Project workspace</span></div></div>
          <button className="mobile-close" onClick={() => setSidebarOpen(false)} aria-label="Close navigation"><Icon name="close" /></button>
          <button className="new-chat" onClick={resetChat} disabled={loading}><Icon name="plus" /><span>New chat</span></button>
        </div>
        <div className="sidebar-content">
          <p className="sidebar-label">Workspace</p>
          <section className="context-card" aria-label="Current project context"><div className="context-icon">DS</div><div><strong>DS701</strong><small>PM / TPM workspace</small></div></section>
          <section className="history-section"><h2>Conversations</h2><div className="conversation-list">{historyLoading && conversations.length === 0 ? <p className="history-empty">Loading…</p> : conversations.length === 0 ? <p className="history-empty">Your conversations will appear here.</p> : conversations.map((conversation) => <div className={`conversation-item ${conversation.id === conversationId ? "active" : ""}`} key={conversation.id}><button className="conversation-open" onClick={() => void openConversation(conversation.id)} disabled={loading}><span>{conversation.title}</span><small>{relativeTime(conversation.updated_at)}</small></button><button className="conversation-menu-button" aria-label={`Actions for ${conversation.title}`} onClick={(event) => { event.stopPropagation(); setOpenMenuId((current) => current === conversation.id ? null : conversation.id); }}>⋯</button>{openMenuId === conversation.id && <div className="conversation-menu" onClick={(event) => event.stopPropagation()}><button onClick={() => void renameConversation(conversation)}>Rename</button><button className="delete-option" onClick={() => void deleteConversation(conversation.id)}>Delete</button></div>}</div>)}</div></section>
        </div>
        <div className="sidebar-footer"><span className="local-mark">L</span><div><strong>Local workspace</strong><span>Private by default</span></div></div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <button className="menu-button" onClick={() => setSidebarOpen(true)} aria-label="Open navigation"><Icon name="menu" /></button>
          <div className="breadcrumb"><span>Workspace</span><b>/</b><strong>{conversationId ? conversations.find((item) => item.id === conversationId)?.title || "Conversation" : "New conversation"}</strong></div>
          <div className="topbar-actions"><button className="theme-toggle" onClick={toggleTheme} aria-label={`Switch to ${theme === "light" ? "dark" : "light"} mode`} title={`Switch to ${theme === "light" ? "dark" : "light"} mode`}><Icon name={theme === "light" ? "moon" : "sun"} /></button><div className="context-pill"><span>DS</span>DS701</div></div>
        </header>

        <section className={`chat ${messages.length === 0 ? "chat-empty" : ""}`} aria-label="Chat conversation" aria-live="polite">
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="welcome-mark"><Icon name="spark" /></div><p className="eyebrow">DS701 · Project copilot</p>
              <h1>Your project, at a glance.</h1><p className="welcome-copy">Bring tasks, conversations, meetings, and project knowledge into one focused view.</p>
              <div className="prompt-grid" aria-label="Example prompts">{examples.map((prompt) => <button key={prompt} onClick={() => void submit(prompt)} disabled={loading}><span>{prompt}</span><Icon name="chevron" /></button>)}</div>
            </div>
          ) : (
            <div className="message-list">
              {messages.map((message, index) => (
                <article className={`message-row ${message.role}`} key={`${message.role}-${index}`}>
                  {message.role === "assistant" && <div className="avatar"><Icon name="spark" /></div>}
                  <div className="message-stack"><span className="message-label">{message.role === "user" ? "You" : "PM Copilot"}</span><div className="bubble"><Markdown>{message.content}</Markdown></div>
                    {message.role === "assistant" && message.sources && <div className="source-list" aria-label="Response sources">{message.sources.map((source, sourceIndex) => <span className="source-chip" key={`${source.name}-${sourceIndex}`}>{source.name}{source.detail ? ` · ${source.detail}` : ""}</span>)}</div>}
                  </div>
                </article>
              ))}
              {loading && <article className="message-row assistant thinking-row" role="status"><div className="avatar"><Icon name="spark" /></div><div className="message-stack"><span className="message-label">PM Copilot</span><div className="thinking"><i /><i /><i /><span>Reviewing your project context</span></div></div></article>}
              <div ref={endRef} />
            </div>
          )}
        </section>

        <div className="composer-area">
          {error && <div className="error" role="alert"><strong>Something went wrong</strong><span>{error}</span></div>}
          <form className="composer" onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void submit(draft); }}>
            <label className="sr-only" htmlFor="message">Message PM Copilot</label>
            <textarea ref={inputRef} id="message" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={onComposerKeyDown} placeholder="Ask about your project…" rows={1} disabled={loading} />
            <button type="submit" disabled={!draft.trim() || loading} aria-label={loading ? "Waiting for response" : "Send message"}>{loading ? <span className="send-loader" /> : <Icon name="send" />}</button>
          </form>
          <p className="composer-hint">Enter to send <span>·</span> Shift + Enter for a new line</p>
        </div>
      </section>
    </main>
  );
}
