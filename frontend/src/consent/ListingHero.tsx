import { useState } from "react";

export default function ListingHero({ title, photoUrl }: { title: string; photoUrl?: string }) {
  const [failed, setFailed] = useState(false);
  if (photoUrl && !failed) {
    return <img className="listing-photo" src={photoUrl} alt="" onError={() => setFailed(true)} />;
  }
  return (
    <div className="listing-photo listing-fallback" aria-hidden="true">
      <span>{title.slice(0, 1).toUpperCase()}</span>
    </div>
  );
}
