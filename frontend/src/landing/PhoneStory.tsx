import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import tandemMark from "../assets/tandem-mark.png";

// Scroll-driven iPhone: the phone turns around, a Tandem text lands, Messages opens and the real demo
// conversation plays out. Prices match the seeded Tahoe demo (payments are simulated).
type Line = { from: "me" | "tandem"; text: string; at: number; kind?: "options" | "link" | "status" | "done" };

const LINES: Line[] = [
  { from: "me", text: "tahoe nov 14–16, 6 of us, ~$200 each 🏔️", at: 0.36 },
  { from: "tandem", text: "Found 3 cabins that fit everyone", at: 0.44, kind: "options" },
  { from: "me", text: "1", at: 0.53 },
  { from: "tandem", text: "Plan’s ready. Send it to the group:", at: 0.6, kind: "link" },
  { from: "tandem", text: "5 of 6 in. Nobody’s charged until everyone is.", at: 0.69, kind: "status" },
  { from: "tandem", text: "Booked! $175 each. Nobody fronted a cent.", at: 0.78, kind: "done" },
];
const OPTIONS = [["Pinecrest A-Frame", "$175"], ["Cedar Hollow", "$162"], ["Lakeview Bungalow", "$178"]];
const STEPS = [0, 0.18, 0.3, 0.6, 0.86];

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
      const rect = el.getBoundingClientRect();
      const span = el.offsetHeight - window.innerHeight;
      setP(span > 0 ? clamp(-rect.top / span) : 1);
    };
    const onScroll = () => { if (!frame) frame = requestAnimationFrame(update); };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => { window.removeEventListener("scroll", onScroll); window.removeEventListener("resize", onScroll); cancelAnimationFrame(frame); };
  }, [ref]);
  return p;
}

function Bubble({ line }: { line: Line }) {
  return <div className={`ps-bubble ps-${line.from} ${line.kind ? `ps-k-${line.kind}` : ""}`}>
    {line.kind === "done" ? <span aria-hidden="true">🎉 </span> : line.kind === "options" ? <span aria-hidden="true">🔎 </span> : line.kind === "link" ? <span aria-hidden="true">✨ </span> : null}
    {line.text}
    {line.kind === "options" && <ol className="ps-options">{OPTIONS.map(([name, price]) => <li key={name}><span>{name}</span><b>{price}</b></li>)}</ol>}
    {line.kind === "link" && <span className="ps-link"><img src={tandemMark} alt="" width="22" height="22" />Tahoe weekend<small>tandem · plan</small></span>}
    {line.kind === "status" && <span className="ps-meter" aria-hidden="true"><i style={{ width: "83%" }} /></span>}
  </div>;
}

export default function PhoneStory() {
  const section = useRef<HTMLElement>(null);
  const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const scrolled = useScrollProgress(section);
  const p = reduced ? 1 : scrolled;

  const turn = ease(p / 0.16);                       // back -> front
  const lock = clamp((p - 0.16) / 0.05);             // lock screen + notification
  const open = ease((p - 0.27) / 0.06);              // Messages slides up
  const outro = ease((p - 0.86) / 0.08);             // closing call to action
  const shown = LINES.filter((l) => p >= l.at);
  const step = STEPS.filter((s) => p >= s).length - 1;

  return <section ref={section} className={`phone-story ${reduced ? "is-static" : ""}`} aria-label="How Tandem works, shown in a text conversation">
    <div className="ps-stage">
      <div className="ps-copy" style={{ opacity: 1 - ease((p - 0.08) / 0.1) * 0.85 }}>
        <h1>Group plans, settled<br />in a text.</h1>
        <p>Tell Tandem what you want. Everyone pays only their share.</p>
      </div>
      <div className="ps-phone-wrap" style={{ transform: `translateY(${(1 - turn) * 6}%) scale(${0.92 + turn * 0.08})` }}>
        <div className="ps-phone" style={{ transform: `rotateY(${180 - turn * 180}deg) rotateZ(${(1 - turn) * -8}deg)` }}>
          <div className="ps-face ps-back" aria-hidden="true">
            <span className="ps-cams"><i /><i /><i /></span>
            <img src={tandemMark} alt="" className="ps-back-logo" />
          </div>
          <div className="ps-face ps-front">
            <div className="ps-island" aria-hidden="true" />
            <div className="ps-lock" style={{ opacity: 1 - open }} aria-hidden={open > 0.5}>
              <span className="ps-time">9:41</span>
              <span className="ps-date">Saturday, November 1</span>
              <div className="ps-note" style={{ opacity: lock, transform: `translateY(${(1 - lock) * -14}px)` }}>
                <img src={tandemMark} alt="" width="28" height="28" />
                <div><b>Tandem</b><span>Found 3 cabins that fit everyone 🏔️</span></div><small>now</small>
              </div>
            </div>
            <div className="ps-app" style={{ transform: `translateY(${(1 - open) * 100}%)` }} aria-hidden={open < 0.5}>
              <div className="ps-app-head"><img src={tandemMark} alt="" width="34" height="34" /><b>Tandem</b></div>
              {/* column-reverse keeps the newest message at the bottom and lets older ones scroll off the top */}
              <div className="ps-thread" aria-live="polite" style={{ paddingBottom: `calc(14px + ${outro * 46}%)` }}>
                {[...shown].reverse().map((l) => <Bubble line={l} key={l.at} />)}
              </div>
              <div className="ps-cta" style={{ opacity: outro, pointerEvents: outro > 0.5 ? "auto" : "none" }}>
                <p>Your turn.</p>
                <Link className="ps-cta-btn" to="/chat/demo" tabIndex={outro > 0.5 ? 0 : -1}>Try the live demo</Link>
                <small>Demo data · payments are simulated</small>
              </div>
            </div>
          </div>
        </div>
      </div>
      <ol className="ps-dots" aria-hidden="true">{STEPS.map((s, i) => <li key={s} className={i === step ? "on" : ""} />)}</ol>
      {p < 0.05 && !reduced && <span className="ps-scroll" aria-hidden="true">↓ scroll</span>}
    </div>
  </section>;
}
