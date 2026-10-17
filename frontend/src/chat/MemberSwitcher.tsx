import type { GroupMember } from "./types";

// Demo only: lets one laptop play every member (?as=Maya). Real sign-in replaces this.
export function MemberSwitcher({ members, meId, onChange }: { members: GroupMember[]; meId: string; onChange: (m: GroupMember) => void }) {
  return (
    <label className="switcher">
      <span>Chatting as</span>
      <select value={meId} onChange={(e) => {
        const m = members.find((x) => x.id === e.target.value);
        if (m) onChange(m);
      }}>
        {members.map((m) => <option key={m.id} value={m.id}>{m.display_name}{m.role === "organizer" ? " (organizer)" : ""}</option>)}
      </select>
    </label>
  );
}
