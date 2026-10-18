import type { ShareStatus } from "../../types/contracts";

// Status is never color-only: glyph + text + color (TANDEM_SPEC 8.2).
export const SHARE_STATUS: Record<ShareStatus, { glyph: string; label: string; tone: string }> = {
  invited: { glyph: "◌", label: "Invited", tone: "ink" },
  viewing: { glyph: "⏱", label: "Deciding", tone: "pending" },
  card_failed: { glyph: "↻", label: "Retrying card", tone: "pending" },
  held: { glyph: "🔒", label: "Held, not charged", tone: "funded" },
  captured: { glyph: "✓", label: "Paid", tone: "funded" },
  declined: { glyph: "✕", label: "Out", tone: "decline" },
  released: { glyph: "🔓", label: "Released", tone: "ink" },
  refunded: { glyph: "↩", label: "Refunded", tone: "ink" },
};
