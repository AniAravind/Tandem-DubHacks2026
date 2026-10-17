import type { ReactNode } from "react";

type P = { size?: number; className?: string };

function Svg({ size = 20, className, children }: P & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

export const Lock = (p: P) => (
  <Svg {...p}><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></Svg>
);
export const LockCheck = (p: P) => (
  <Svg {...p}><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /><path d="m9.5 16 1.75 1.75L14.5 14.5" /></Svg>
);
export const Check = (p: P) => <Svg {...p}><path d="M20 6 9 17l-5-5" /></Svg>;
export const X = (p: P) => <Svg {...p}><path d="M18 6 6 18M6 6l12 12" /></Svg>;
export const XCircle = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="m15 9-6 6M9 9l6 6" /></Svg>;
export const Clock = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></Svg>;
export const Store = (p: P) => (
  <Svg {...p}><path d="M3 9l1.5-5h15L21 9" /><path d="M4 9v11h16V9" /><path d="M3 9a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0" /></Svg>
);
export const Shield = (p: P) => <Svg {...p}><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z" /></Svg>;
export const Flask = (p: P) => <Svg {...p}><path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3" /><path d="M7 15h10" /></Svg>;
export const Calendar = (p: P) => <Svg {...p}><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M3 10h18M8 3v4M16 3v4" /></Svg>;
export const Alert = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 8v4M12 16h.01" /></Svg>;
export const Tag = (p: P) => <Svg {...p}><path d="M20 12 12 20l-9-9V3h8z" /><circle cx="7.5" cy="7.5" r="1.5" /></Svg>;

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size * 1.6} height={size} viewBox="0 0 38 24" aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="var(--tandem-600)" strokeWidth="3" />
      <circle cx="26" cy="12" r="9" fill="none" stroke="var(--tandem-600)" strokeWidth="3" opacity="0.55" />
    </svg>
  );
}
