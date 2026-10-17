// Message shapes from PHASE_0.md 6.3 (GET /groups/{gid}/messages). Card data shapes live in types/contracts.ts.

export interface CardEnvelope {
  type: string;
  data: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  seq: number;
  direction: "in" | "out";
  member: { id: string; display_name: string } | null;
  text: string;
  card: CardEnvelope | null;
  quick_replies: string[];
  private?: boolean;
  created_at: string;
}

export interface GroupMember {
  id: string;
  display_name: string;
  role: "organizer" | "member";
  status: string;
}

export interface Group {
  id: string;
  name: string;
  timezone: string;
  members: GroupMember[];
}
