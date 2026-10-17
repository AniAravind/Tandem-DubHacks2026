import { useEffect, useState } from "react";

// Shown while Tandem works: dots for up to 6s, then a status line (TANDEM_SPEC 8.8). Render with key={since}.
export function TypingIndicator({ since }: { since: number | null }) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (since === null) return;
    const t = setTimeout(() => setSlow(true), Math.max(0, 6000 - (Date.now() - since)));
    return () => clearTimeout(t);
  }, [since]);
  if (since === null) return null;
  return (
    <div className="typing" role="status">
      {slow ? "Still searching. Checking 3 merchants…" : <><span className="dots" aria-hidden="true"><i /><i /><i /></span> Tandem is typing</>}
    </div>
  );
}
