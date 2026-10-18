import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import QRCode from "qrcode";
import tandemMark from "../assets/tandem-mark-blue.png";
import "./demo-registration.css";

type Connection = {
  status: "queued" | "provisioning" | "connecting" | "ready" | "active" | "failed";
  destination?: string;
  error?: { code: string; message: string };
};
type Enrollment = Connection & { token: string };
const STORAGE = "tandem.onboarding";

function rememberedTicket() {
  try { return sessionStorage.getItem(STORAGE) || ""; } catch { return ""; }
}
function rememberTicket(token: string) {
  try { if (token) sessionStorage.setItem(STORAGE, token); else sessionStorage.removeItem(STORAGE); } catch { /* storage is optional */ }
}
async function readResponse<T>(response: Response): Promise<T> {
  let data;
  try { data = await response.json(); } catch { throw new Error("We couldn’t reach the demo. Please try again."); }
  if (!response.ok) throw new Error(data.error?.message || "We couldn’t connect. Please try again.");
  return data as T;
}

export default function DemoRegistration({ initialToken, onClose }: { initialToken: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [phone, setPhone] = useState("");
  const [token, setToken] = useState(initialToken || rememberedTicket);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [qr, setQr] = useState("");
  const [copied, setCopied] = useState(false);
  const [pollAttempt, setPollAttempt] = useState(0);
  const iphone = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const android = /Android/.test(navigator.userAgent);
  const ready = connection?.status === "ready" || connection?.status === "active";
  const messagesUrl = ready && connection.destination ? `sms:${connection.destination}` : "";
  const failed = connection?.status === "failed";
  const [waitingLong, setWaitingLong] = useState(false);

  useEffect(() => {
    const active = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    element?.showModal();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { element?.close(); document.body.style.overflow = overflow; active?.focus(); };
  }, []);

  useEffect(() => {
    if (!token || android) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const abort = new AbortController();
    const started = Date.now();
    async function poll() {
      try {
        const response = await fetch("/api/v1/demo/registrations/status", {
          headers: { "X-Tandem-Onboarding": token }, cache: "no-store", signal: abort.signal,
        });
        if (response.status === 410) {
          if (!cancelled) { setToken(""); rememberTicket(""); setConnection(null); }
        }
        const status = await readResponse<Connection>(response);
        if (cancelled) return;
        setConnection(status);
        setError("");
        if (status.status === "failed") return;
        if (Date.now() - started > 180000 && status.status !== "ready" && status.status !== "active") {
          setWaitingLong(true);
          return;
        }
        timer = setTimeout(poll, status.status === "ready" || status.status === "active" ? 15000 : 2000);
      } catch (problem) {
        if (cancelled) return;
        setConnection(null);
        setError(problem instanceof Error ? problem.message : "Connection interrupted. Please try again.");
      }
    }
    void poll();
    return () => { cancelled = true; abort.abort(); clearTimeout(timer); };
  }, [token, android, pollAttempt]);

  useEffect(() => {
    if (!messagesUrl || iphone || android) return;
    let cancelled = false;
    void QRCode.toDataURL(messagesUrl, {
      width: 232, margin: 4, errorCorrectionLevel: "M",
      color: { dark: "#141924", light: "#e9edf4" },
    })
      .then(value => { if (!cancelled) setQr(value); }).catch(() => { if (!cancelled) setQr(""); });
    return () => { cancelled = true; };
  }, [messagesUrl, iphone, android]);

  async function register(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const raw = phone.trim();
      const digits = raw.replace(/\D/g, "");
      if (!/^\d{10}$/.test(digits) && !/^1\d{10}$/.test(digits)) {
        throw new Error("Enter a 10-digit US or Canadian phone number.");
      }
      const number = digits.length === 11 ? `+${digits}` : `+1${digits}`;
      const response = await fetch("/api/v1/demo/registrations", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ phone: number }),
        signal: AbortSignal.timeout(30000),
      });
      const enrollment = await readResponse<Enrollment>(response);
      rememberTicket(enrollment.token);
      setConnection(enrollment);
      setToken(enrollment.token);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "We couldn’t connect. Please try again.");
    } finally { setBusy(false); }
  }

  function reset() {
    setToken(""); rememberTicket(""); setConnection(null); setError(""); setWaitingLong(false); setQr("");
  }

  return <dialog ref={dialog} className={`demo-dialog${ready && !iphone && !android ? " demo-dialog-wide" : ""}`} aria-labelledby="demo-title" onCancel={onClose} onClick={event => {
    if (event.target === event.currentTarget) {
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }
  }}>
    <button className="demo-close" onClick={onClose} aria-label="Close demo registration">×</button>
    <img className="demo-mark" src={tandemMark} alt="" width="48" height="48" />
    <p className="demo-eyebrow">a little less back and forth</p>
    <h2 id="demo-title">{android ? "Meet tandem on iMessage." : ready ? "You’re ready to text." : token ? "Let’s get connected." : "Your plans start with a text."}</h2>
    {android ? <p>The texting demo works with iMessage on iPhone. You can still try tandem in your browser below.</p> : <>
      {!token ? <>
        <p>Connect the number you’ll text from, then meet tandem in iMessage.</p>
        <form onSubmit={register}>
          <label htmlFor="demo-phone">Your phone number</label>
          <input id="demo-phone" type="tel" autoComplete="tel-national" inputMode="tel" maxLength={32} required
            placeholder="(206) 555-0123"
            value={phone} onChange={event => setPhone(event.target.value)} aria-describedby="demo-phone-note" />
          <small id="demo-phone-note">US and Canada (+1). Use your iMessage number. You send the first text.</small>
          <button className="btn primary demo-primary" disabled={busy || !phone.trim()}>{busy ? "Connecting…" : "Connect my number"}<span aria-hidden="true">↗</span></button>
        </form>
      </> : <>
        <div className="demo-connection" role="status" aria-live="polite">
          {error ? "Connection paused" : failed ? connection.error?.message : ready ? "Connected to tandem" : waitingLong ? "This is taking longer than expected. Check again in a moment." : "Connecting your number to iMessage…"}
        </div>
        {ready && connection.destination && <div className="demo-ready-grid">
          <div className="demo-ready-details">
          <p>Open Messages and send a hello—or tell tandem what you’re planning.</p>
          {iphone && <a className="btn primary demo-primary" href={messagesUrl}>Try now <span aria-hidden="true">↗</span></a>}
          <button className="demo-number" onClick={() => {
            void navigator.clipboard?.writeText(connection.destination!).then(() => setCopied(true)).catch(() => setCopied(false));
          }}>{connection.destination}<span>{copied ? "Copied" : "Copy number"}</span></button>
          <small className="demo-note">Send from the number you registered. Opening Messages doesn’t send anything.</small>
          </div>
          {!iphone && <aside className="demo-qr-panel" aria-label="Open tandem in Messages">
            <span className="demo-qr-label">Open in iMessage</span>
            {qr && <img className="demo-qr" src={qr} width="156" height="156" alt="Scan with your iPhone camera to open Messages addressed to tandem" />}
            <p className="demo-scan">Scan with your iPhone camera.</p>
          </aside>}
        </div>}
        {!ready && !failed && !error && !waitingLong && <button className="btn primary demo-primary" disabled>Connecting…</button>}
        {(error || waitingLong) && <button className="btn demo-primary" onClick={() => { setError(""); setWaitingLong(false); setPollAttempt(value => value + 1); }}>Check connection again</button>}
        {failed && <button className="btn demo-primary" onClick={reset}>Try registering again</button>}
        {!failed && <button className="demo-change" onClick={reset}>Use a different number</button>}
      </>}
    </>}
    {error && <p className="demo-error" role="alert">{error}</p>}
    <div className="demo-footer"><Link to="/chat/demo" onClick={onClose}>Try in your browser instead <span aria-hidden="true">→</span></Link></div>
  </dialog>;
}
