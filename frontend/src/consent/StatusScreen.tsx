import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export type NextLink = { to: string; label: string };
type Tone = "funded" | "pending" | "decline" | "ink";

const GLYPHS: Record<Tone, string> = { funded: "✓", pending: "⏱", decline: "✕", ink: "○" };

export default function StatusScreen({
  tone, eyebrow, title, children, links = [],
}: {
  tone: Tone; eyebrow: string; title: string; children?: ReactNode; links?: NextLink[];
}) {
  return (
    <section className={`status-screen tone-${tone}`} aria-labelledby="status-title">
      <div className="status-icon" aria-hidden="true">{GLYPHS[tone]}</div>
      <p className="eyebrow">{eyebrow}</p>
      <h1 id="status-title">{title}</h1>
      {children}
      {links.length > 0 && (
        <div className="status-actions">
          {links.map((l, i) => <Link key={l.to} className={`btn${i === 0 ? " btn-primary" : ""}`} to={l.to}>{l.label}</Link>)}
        </div>
      )}
    </section>
  );
}
