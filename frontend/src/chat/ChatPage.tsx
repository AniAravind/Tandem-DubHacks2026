import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, setDemoMember } from "../api/client";
import { SimBadge } from "../components/cards/common";
import { Composer } from "./Composer";
import { DEMO_GROUP, resolveGroupId } from "./demo";
import { MemberSwitcher } from "./MemberSwitcher";
import { MessageList } from "./MessageList";
import { QuickReplies } from "./QuickReplies";
import { TypingIndicator } from "./TypingIndicator";
import type { ChatMessage, Group, GroupMember } from "./types";
import "./chat.css";

const POLL_MS = 2000;
const TYPING_MAX_MS = 30000;

export default function ChatPage() {
  const groupId = resolveGroupId(useParams().groupId);
  const [params, setParams] = useSearchParams();
  const [group, setGroup] = useState<Group | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [typingSince, setTypingSince] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lastSeq = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);

  const me: GroupMember | undefined = useMemo(() => {
    const members = group?.members ?? [];
    const as = (params.get("as") ?? "").toLowerCase();
    return members.find((m) => m.display_name.toLowerCase() === as) ?? members.find((m) => m.role === "organizer") ?? members[0];
  }, [group, params]);

  // Load the group. The demo roster is the fallback so /chat/demo works before anything else is set up.
  useEffect(() => {
    let cancelled = false;
    const seed = groupId === DEMO_GROUP.id ? DEMO_GROUP : null;
    if (seed) setDemoMember(seed.members[0].id);
    api.get<Group>(`/groups/${groupId}`)
      .then((g) => !cancelled && setGroup({ ...g, members: g.members.filter((m) => m.status === "active") }))
      .catch(() => !cancelled && (seed ? setGroup(seed) : setError("This chat isn't available. Check the link.")));
    return () => { cancelled = true; };
  }, [groupId]);

  const poll = useCallback(async () => {
    if (!me) return;
    setDemoMember(me.id);
    try {
      const r = await api.get<{ messages: ChatMessage[] }>(`/groups/${groupId}/messages?after_seq=${lastSeq.current}`);
      if (!r.messages.length) return;
      lastSeq.current = r.messages[r.messages.length - 1].seq;
      setMessages((prev) => [...prev, ...r.messages.filter((m) => !prev.some((p) => p.id === m.id))]);
      if (r.messages.some((m) => m.direction === "out")) setTypingSince(null);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 403 ? "You're not in this group." : "Reconnecting…");
    }
  }, [groupId, me]);

  useEffect(() => {
    poll();
    const t = setInterval(poll, POLL_MS);
    return () => clearInterval(t);
  }, [poll]);

  useEffect(() => {
    if (typingSince === null) return;
    const t = setTimeout(() => setTypingSince(null), TYPING_MAX_MS);
    return () => clearTimeout(t);
  }, [typingSince]);

  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    bottom.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "end" });
  }, [messages.length, typingSince]);

  const send = useCallback(async (text: string) => {
    try {
      await api.post("/chat/web", { group_id: groupId, text });
      if (/@tandem\b/i.test(text)) setTypingSince(Date.now());
      await poll();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't send. Try again.");
      throw e;
    }
  }, [groupId, poll]);

  const reply = useCallback((t: string) => { send(t).catch(() => undefined); }, [send]);

  const consentUrl = useMemo(() => {
    const dm = [...messages].reverse().find((m) => m.private && /\/c\/\S+/.test(m.text));
    return dm?.text.match(/\/c\/\S+/)?.[0];
  }, [messages]);
  const lastQuick = messages.length ? messages[messages.length - 1].quick_replies : [];

  if (!group || !me) {
    return <main className="chat-page"><p className="muted">{error ?? "Loading chat…"}</p></main>;
  }
  return (
    <main className="chat-page">
      <header className="chat-header">
        <div>
          <h1>{group.name}</h1>
          <p className="muted">{group.members.length} people · <SimBadge>SIMULATED PAYMENTS</SimBadge></p>
        </div>
        <MemberSwitcher members={group.members} meId={me.id} onChange={(m) => {
          // Each member sees different private messages, so reload the history as them.
          lastSeq.current = 0;
          setMessages([]);
          setParams({ as: m.display_name });
        }} />
      </header>
      {error ? <p className="banner" role="status">{error}</p> : null}
      {messages.length === 0 ? (
        <p className="empty">No messages yet. Try: “@tandem can you find us a cabin? like 150-200 each”</p>
      ) : null}
      <MessageList messages={messages} meId={me.id} onReply={reply} consentUrl={consentUrl} />
      <TypingIndicator key={typingSince ?? 0} since={typingSince} />
      <div ref={bottom} />
      <QuickReplies replies={lastQuick} onReply={reply} />
      <Composer onSend={send} />
    </main>
  );
}
