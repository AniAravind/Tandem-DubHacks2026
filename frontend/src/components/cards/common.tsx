export interface CardProps<T> {
  data: T;
  onReply: (text: string) => void;   // sends a chat message as the current member (votes are "1", "2", "3")
  consentUrl?: string;               // the current member's private consent link, if they have one
  meId?: string;
  onDemoShare?: (action: "hold" | "decline") => void;   // fake-engine demo only (no consent page yet)
}

export function CatalogTag() {
  return <span className="card-demo">Catalog</span>;
}
