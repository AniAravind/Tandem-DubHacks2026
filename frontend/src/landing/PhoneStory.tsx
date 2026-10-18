import { useEffect, useRef, useState } from "react";
import tandemMark from "../assets/tandem-mark-blue.png";

// A 3D phone rises off the table while a real iMessage thread with Tandem plays out as you scroll. Prices match the seeded
// Tahoe demo (test payments only).
type Msg = { from: "me" | "tandem"; at: number; lines: string[]; link?: boolean; typing?: number };

const THREAD: Msg[] = [
  { from: "me", at: 0.17, lines: ["tahoe nov 14–16, 6 of us, ~$200 each 🏔️"] },
  { from: "tandem", at: 0.26, typing: 0.05, lines: ["Found 3 cabins that fit everyone:", "1. Pinecrest A-Frame · $175 each", "2. Cedar Hollow · $162 each", "3. Lakeview Bungalow · $178 each"] },
  { from: "me", at: 0.33, lines: ["1"] },
  { from: "tandem", at: 0.41, typing: 0.05, lines: ["Done. Here’s the plan for the group 👇"], link: true },
  { from: "tandem", at: 0.63, typing: 0.04, lines: ["5 of 6 holds in. Nobody’s charged until everyone is."] },
  { from: "tandem", at: 0.76, typing: 0.04, lines: ["Booked 🎉 Pinecrest A-Frame, $175 each.", "Nobody fronted a cent."] },
];
const CAPTIONS: [number, string][] = [
  [0.12, "Text Tandem like you’d text a friend."],
  [0.3, "It finds options that fit everyone’s budget."],
  [0.45, "Everyone holds only their own share."],
  [0.72, "It books the moment everyone’s in."],
];

const clamp = (v: number, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
const ease = (v: number) => 1 - Math.pow(1 - clamp(v), 3);

function useScrollProgress(ref: React.RefObject<HTMLElement | null>) {
  const [p, setP] = useState(0);
  useEffect(() => {
    let frame = 0;
    const update = () => {
      frame = 0;
      const el = ref.current;
      if (!el) return;
      const span = el.offsetHeight - window.innerHeight;
      setP(span > 0 ? clamp(-el.getBoundingClientRect().top / span) : 1);
    };
    const onScroll = () => { if (!frame) frame = requestAnimationFrame(update); };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => { window.removeEventListener("scroll", onScroll); window.removeEventListener("resize", onScroll); cancelAnimationFrame(frame); };
  }, [ref]);
  return p;
}

function StatusBar() {
  return <div className="im-status" aria-hidden="true">
    <b>9:41</b>
    <span className="im-status-icons">
      <svg viewBox="0 0 18 12"><rect x="0" y="8" width="3" height="4" rx="1" /><rect x="5" y="5.5" width="3" height="6.5" rx="1" /><rect x="10" y="3" width="3" height="9" rx="1" /><rect x="15" y="0" width="3" height="12" rx="1" /></svg>
      <svg viewBox="0 0 16 12"><path d="M8 11.5 5.6 9a3.4 3.4 0 0 1 4.8 0L8 11.5Zm-4.2-4.3L2.3 5.7a8 8 0 0 1 11.4 0l-1.5 1.5a5.9 5.9 0 0 0-8.4 0ZM.7 4.1-.8 2.6a12.4 12.4 0 0 1 17.6 0l-1.5 1.5a10.3 10.3 0 0 0-14.6 0Z" /></svg>
      <svg viewBox="0 0 27 12"><rect x=".5" y=".5" width="23" height="11" rx="3.2" fill="none" stroke="currentColor" opacity=".4" /><rect x="2" y="2" width="20" height="8" rx="2" /><rect x="24.5" y="4" width="2" height="4" rx="1" opacity=".4" /></svg>
    </span>
  </div>;
}

function Bubble({ msg, tail, delivered }: { msg: Msg; tail: boolean; delivered: boolean }) {
  return <div className={`im-row im-${msg.from}`}>
    <div className={`im-bubble ${tail ? "im-tail" : ""}`}>
      {msg.lines.map((line) => <span key={line}>{line}</span>)}
    </div>
    {msg.link && <div className="im-preview">
      <div className="im-preview-art"><img src={tandemMark} alt="" /><span>Tahoe weekend</span></div>
      <div className="im-preview-meta"><b>Tahoe weekend · 6 people</b><span>tandem.app</span></div>
    </div>}
    {delivered && <small className="im-delivered">Delivered</small>}
  </div>;
}

export default function PhoneStory({ onTryDemo }: { onTryDemo: () => void }) {
  const section = useRef<HTMLElement>(null);
  const tilt = useRef<HTMLDivElement>(null);
  const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const scrolled = useScrollProgress(section);
  const p = reduced ? 1 : scrolled;

  // A gentle extra tilt that follows the pointer (desktop only); CSS variables avoid re-rendering React.
  useEffect(() => {
    if (reduced) return;
    const onMove = (e: PointerEvent) => {
      if (e.pointerType !== "mouse" || !tilt.current) return;
      tilt.current.style.setProperty("--mx", `${((e.clientX / window.innerWidth) - 0.5) * 10}deg`);
      tilt.current.style.setProperty("--my", `${((e.clientY / window.innerHeight) - 0.5) * -8}deg`);
    };
    window.addEventListener("pointermove", onMove);
    return () => window.removeEventListener("pointermove", onMove);
  }, [reduced]);

  const lift = ease(p / 0.14);                             // lying on the table -> standing, facing you
  const outro = ease((p - 0.86) / 0.07);
  const shown = THREAD.filter((m) => p >= m.at);
  const typing = THREAD.find((m) => m.typing && p >= m.at - m.typing && p < m.at);
  const lastMine = [...shown].reverse().find((m) => m.from === "me");
  const caption = [...CAPTIONS].reverse().find(([at]) => p >= at);
  const rx = 58 * (1 - lift), rz = -24 * (1 - lift), ry = -16 + 6 * lift;

  return <section ref={section} className={`phone-story ${reduced ? "is-static" : ""}`} aria-label="How Tandem works, shown as an iMessage conversation">
    <div className="ps-stage">
      <div className="ps-copy">
        <div className="ps-hero" style={{ opacity: 1 - ease((p - 0.06) / 0.06), transform: `translateY(${-ease((p - 0.06) / 0.06) * 14}px)` }}>
          <h1>Group plans, settled in a text.</h1>
          <p>Everyone pays only their share. Nobody fronts the money.</p>
          <div className="ps-actions"><button className="btn primary" onClick={onTryDemo}>Try the live demo</button></div>
        </div>
        <div className="ps-caption" style={{ opacity: caption ? 1 : 0 }}>
          <div className="ps-progress" aria-hidden="true">{CAPTIONS.map(([at]) => <i key={at} className={p >= at ? "on" : ""} />)}</div>
          <p key={caption?.[1]}>{caption?.[1]}</p>
        </div>
      </div>
      <div className="ps-scene">
        <div className="ps-floor" style={{ transform: `translateX(-50%) scale(${0.7 + lift * 0.3}, ${1.6 - lift})`, opacity: 0.35 + lift * 0.3 }} aria-hidden="true" />
        <div className="ps-tilt" ref={tilt}>
          <div className="ps-phone"
            style={{ transform: `rotateX(calc(${rx}deg + var(--my, 0deg))) rotateY(calc(${ry}deg + var(--mx, 0deg))) rotateZ(${rz}deg) translateY(${(1 - lift) * 8}%)` }}>
            {Array.from({ length: 9 }, (_, i) => <div key={i} className="ps-slice" style={{ transform: `translateZ(${(i - 4) * 1.6}px)` }} aria-hidden="true" />)}
            <div className="ps-face ps-back" aria-hidden="true"><span className="ps-cams"><i /><i /><i /></span><img src={tandemMark} alt="" /></div>
            <div className="ps-face ps-front">
              <div className="ps-island" aria-hidden="true" />
              <div className="im">
                <StatusBar />
                <header className="im-head">
                  <span className="im-back" aria-hidden="true">‹</span>
                  <div className="im-contact"><span className="im-avatar"><img src={tandemMark} alt="" /></span><b>Tandem <i aria-hidden="true">›</i></b></div>
                  <svg className="im-video" viewBox="0 0 24 16" aria-hidden="true"><rect x="1" y="2" width="15" height="12" rx="3" /><path d="m17 6.5 5-3v9l-5-3z" /></svg>
                </header>
                <div className="im-thread" aria-live="polite" style={{ paddingBottom: `calc(8px + ${outro * 44}%)` }}>
                  {typing && <div className="im-row im-tandem"><div className="im-bubble im-tail im-typing" aria-label="Tandem is typing"><i /><i /><i /></div></div>}
                  {[...shown].reverse().map((m, i, list) => <Bubble key={m.at} msg={m}
                    tail={i === 0 || list[i - 1].from !== m.from} delivered={m === lastMine && !typing && list[0] === m} />)}
                  <p className="im-stamp"><b>iMessage</b>Today 9:41 AM</p>
                </div>
                <div className="im-compose" aria-hidden="true">
                  <span className="im-plus">+</span>
                  <span className="im-field">iMessage<svg viewBox="0 0 12 16"><rect x="3.5" y="1" width="5" height="9" rx="2.5" /><path d="M1.5 7.5a4.5 4.5 0 0 0 9 0M6 12v3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" /></svg></span>
                </div>
                <div className="ps-cta" style={{ opacity: outro, pointerEvents: outro > 0.5 ? "auto" : "none" }}>
                  <p>Your turn.</p>
                  <button className="ps-cta-btn" onClick={onTryDemo} tabIndex={outro > 0.5 ? 0 : -1}>Try the live demo</button>
                  <small>Demo · test payments only</small>
                </div>
                <span className="im-home" aria-hidden="true" />
              </div>
            </div>
          </div>
        </div>
      </div>
      {p < 0.04 && !reduced && <span className="ps-scroll" aria-hidden="true">↓ scroll</span>}
    </div>
  </section>;
}
