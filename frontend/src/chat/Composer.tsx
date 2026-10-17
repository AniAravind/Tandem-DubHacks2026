import { useState, type FormEvent } from "react";

export function Composer({ onSend, disabled }: { onSend: (text: string) => Promise<void>; disabled?: boolean }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    try {
      await onSend(t);
      setText("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="composer" onSubmit={submit}>
      <label htmlFor="chat-input" className="sr-only">Message the group</label>
      <input id="chat-input" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000}
             placeholder="Message the group (mention @tandem)" autoComplete="off" disabled={disabled} />
      <button className="btn primary" type="submit" disabled={disabled || busy || !text.trim()}>Send</button>
    </form>
  );
}
