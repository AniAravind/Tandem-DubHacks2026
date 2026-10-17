export function QuickReplies({ replies, onReply }: { replies: string[]; onReply: (t: string) => void }) {
  if (!replies.length) return null;
  return (
    <div className="quick-replies" role="group" aria-label="Quick replies">
      {replies.map((r) => (
        <button key={r} className="chip" onClick={() => onReply(r)}>{r}</button>
      ))}
    </div>
  );
}
