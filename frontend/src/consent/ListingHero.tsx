import { useState } from "react";
import { Calendar, Store } from "./Icons";
import type { ConsentPayload } from "./api";

function Mountains() {
  return (
    <svg viewBox="0 0 400 96" preserveAspectRatio="none" aria-hidden="true">
      <path d="M0 96 L70 38 L120 70 L190 18 L260 66 L320 34 L400 80 L400 96 Z" fill="#312e81" opacity="0.35" />
      <path d="M0 96 L60 64 L130 84 L210 46 L290 82 L350 60 L400 88 L400 96 Z" fill="#1e1b4b" opacity="0.55" />
      <path d="M186 22 L190 18 L198 26 Z" fill="#fff" opacity="0.8" />
    </svg>
  );
}

export default function ListingHero({ data }: { data: ConsentPayload }) {
  const [photoOk, setPhotoOk] = useState(Boolean(data.purchase.photo_url));
  return (
    <section className="card hero">
      <div className="hero-art">
        {photoOk && data.purchase.photo_url ? (
          <img src={data.purchase.photo_url} alt="" onError={() => setPhotoOk(false)} />
        ) : (
          <Mountains />
        )}
      </div>
      <div className="hero-body">
        <p className="invite">
          Hi <strong>{data.member.display_name}</strong>. {data.invited_by
            ? <><strong>{data.invited_by}</strong> invited you to </>
            : "You're invited to "}
          <strong>{data.group_name}</strong>.
        </p>
        <h1 className="hero-title">{data.purchase.title}</h1>
        <div className="chips">
          <span className="chip"><Calendar size={14} />{data.purchase.dates}</span>
          <span className="chip"><Store size={14} />{data.purchase.merchant_name}</span>
          <span className="chip">Demo listing</span>
        </div>
      </div>
    </section>
  );
}
