import { useState } from "react";

// Shows the listing photo when there is one. Without a photo it renders nothing: the title below names the listing.
export default function ListingHero({ photoUrl }: { photoUrl?: string }) {
  const [failed, setFailed] = useState(false);
  if (!photoUrl || failed) return null;
  return <img className="listing-photo" src={photoUrl} alt="" onError={() => setFailed(true)} />;
}
