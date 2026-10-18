import { useEffect, useId, useState, useRef, useCallback } from "react";
import { useParams } from "react-router-dom";
import CanCanNever from "./CanCanNever";
import DeclineSheet from "./DeclineSheet";
import ExpiredLink from "./ExpiredLink";
import HeldSuccess from "./HeldSuccess";
import MandateBox from "./MandateBox";
import PasskeyConfirm from "./PasskeyConfirm";
import ReconfirmSheet from "./ReconfirmSheet";
import SecureCardField from "./SecureCardField";
import ShareSummary from "./ShareSummary";
import {
  DEMO_OTP,
  approve,
  declineConsent,
  getConsent,
  leaveShare,
  reconfirmShare,
  tokenize,
  type Assertion,
  type ConsentPayload,
} from "./api";
import { formatUsd, last4FromPan } from "./money";
import { Link } from "react-router-dom";
import { Mark, Icon } from "../components/layout/AppShell";
import "./consent.css";

type Screen = "loading" | "form" | "held" | "declined" | "expired" | "forbidden" | "captured" | "released" | "refunded";

export default function ConsentPage() {
  const { token = "" } = useParams();
  const [screen, setScreen] = useState<Screen>("loading");
  const [data, setData] = useState<ConsentPayload | null>(null);
  const [cap, setCap] = useState(0);
  const [pan, setPan] = useState("4111111111111111");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [last4, setLast4] = useState("1111");
  const [showDecline, setShowDecline] = useState(false);
  const [showReconfirm, setShowReconfirm] = useState(false);
  const [needOtp, setNeedOtp] = useState(false);
  const [message, setMessage] = useState("");
  const errorId = useId();
  const cardToken = useRef<{last4: string; transient: string; id?: string} | null>(null);
  const applyStatus = useCallback((payload: ConsentPayload) => {
    setData(payload);
    const status = payload.share_status;
    setScreen(status === "held" ? "held" : status === "captured" ? "captured" : status === "released" ? "released" : status === "refunded" ? "refunded" : status === "declined" ? "declined" : "form");
  }, []);
  const refresh = useCallback(async () => { applyStatus(await getConsent(token)); }, [token, applyStatus]);
  useEffect(() => {
    if (screen !== "held") return;
    const timer = setInterval(() => { refresh().catch(() => undefined); }, 2000);
    return () => clearInterval(timer);
  }, [screen, token, refresh]);

  useEffect(() => {
    let alive = true;
    getConsent(token)
      .then((payload) => {
        if (!alive) return;
        setData(payload);
        setCap(payload.default_cap_cents);
        applyStatus(payload);
      })
      .catch((err: { status?: number; message?: string }) => {
        if (!alive) return;
        setMessage(err.message || "");
        setScreen(err.status === 403 ? "forbidden" : "expired");
      });
    return () => { alive = false; };
  }, [token, applyStatus]);

  async function place(assertion: Assertion) {
    if (!data) return;
    setBusy(true);
    setError("");
    try {
      const digits = pan.replace(/\D/g, "");
      const cardLast4 = last4FromPan(digits);
      if (!cardToken.current || cardToken.current.last4 !== cardLast4) cardToken.current = {last4: cardLast4, transient: crypto.randomUUID().replaceAll("-", "")};
      const attempt = cardToken.current;
      if (!attempt.id) {
        const tok = await tokenize(token, cardLast4, attempt.transient);
        attempt.id = tok.agent_token_id;
        setLast4(tok.card.last4);
      }
      const rendered = await getConsent(token, cap);
      await approve(token, {
        agent_token_id: attempt.id!,
        cap_cents: cap,
        consent_text_sha256: rendered.consent_text_sha256,
        assertion,
      });
      if (navigator.vibrate) navigator.vibrate(10);
      await refresh();
    } catch (err: unknown) {
      const e = err as { code?: string; message?: string };
      setError(e.message || "Something went wrong. Nothing was charged.");
      document.getElementById(errorId)?.focus();
    } finally {
      setBusy(false);
    }
  }

  async function startApprove() {
    try {
      const cred = await navigator.credentials.create({
        publicKey: {
          challenge: crypto.getRandomValues(new Uint8Array(32)),
          rp: { name: "Tandem", id: window.location.hostname },
          user: { id: crypto.getRandomValues(new Uint8Array(16)), name: "tandem", displayName: data?.member.display_name || "member" },
          pubKeyCredParams: [{ type: "public-key", alg: -7 }],
          timeout: 15000,
        },
      });
      if (!cred) throw new Error("cancelled");
      await place({ method: "passkey", credential_id: cred.id, client_data_json: "demo", signature: "demo-passkey" });
    } catch {
      setNeedOtp(true);
    }
  }

  async function onDecline(reason: string) {
    setShowDecline(false);
    try { await declineConsent(token, reason); setScreen("declined"); } catch (err) { setError(err instanceof Error ? err.message : "Couldn’t update your choice. Try again."); }
  }

  return (
    <main className="consent-page">
      <header className="consent-header">
        <Link to="/" className="brand"><Mark/><span>tandem<span className="brand-period">.</span></span></Link>
        <span className="sim-badge">SIMULATED PAYMENT</span>
      </header>

      {screen === "loading" && (
        <div aria-busy="true">
          <div className="skeleton" />
          <div className="skeleton" />
          <div className="skeleton" />
        </div>
      )}

      {screen === "expired" && <ExpiredLink message={message} />}
      {screen === "forbidden" && (
        <section className="consent-card">
          <h1>This link isn&apos;t for you</h1>
          <p>Links are personal.</p>
        </section>
      )}
      {screen === "declined" && (
        <section className="consent-card">
          <h1>No problem. Nothing was held.</h1>
          <p className="muted">The group only sees that a spot opened up.</p>
        </section>
      )}
      {(["captured", "released", "refunded"] as Screen[]).includes(screen) && <section className="consent-card"><h1>{screen === "captured" ? "Paid — booking confirmed." : screen === "refunded" ? "Your share was refunded." : "Hold released."}</h1><p>Simulated payment: no real money moves.</p></section>}
      {screen === "held" && data && (
        <HeldSuccess shareCents={data.share_cents} last4={last4} splitCount={data.purchase.split_count} groupId={data.group_id} onLeave={async () => { await leaveShare(data.share_id, data.member.id); await refresh(); }} />
      )}

      {screen === "form" && data && (
        <div className="consent-form-grid"><section className="consent-details">
          <p className="eyebrow">YOUR PLAN. YOUR PERMISSION.</p><h1>A great trip starts<br/>with everyone in.</h1>
          <p className="consent-invitation">Hi {data.member.display_name}. {data.invited_by === data.member.display_name ? "You’re organizing" : `${data.invited_by} invited you to`} <strong>{data.group_name}</strong>.</p>
          <div className="mobile-share-summary"><ShareSummary data={data}/></div>
          {data.purchase.photo_url && (
            <img className="listing-photo" src={data.purchase.photo_url} alt="" onError={e=>{e.currentTarget.style.display="none";}} />
          )}
          <h2 className="consent-listing-title">{data.purchase.title}</h2>
          <p className="muted">{[data.purchase.dates, data.purchase.merchant_name].filter(Boolean).join(" · ")}</p>
          <p className="muted">Simulated payment: no real money moves in this demo. Use test cards.</p>
          <CanCanNever can={data.can} canNever={data.can_never} /><div className="consent-trust"><Icon name="shield"/><span>Your consent belongs to you. You can leave before booking.</span></div></section><section className="consent-review" aria-label="Review and approve your share"><ShareSummary data={data} />
          {data.offers[0] && (
            <p>Offer applied: −{formatUsd(Math.round((data.share_cents * data.offers[0].percent_back) / 100))} (simulated)</p>
          )}
          <MandateBox data={data} cap={cap} onCap={(value) => { setCap(value); getConsent(token, value).then(setData).catch(() => undefined); }} />
          <SecureCardField pan={pan} onPan={setPan} errorId={error ? errorId : undefined} />
          {error && (
            <p className="error" id={errorId} tabIndex={-1} role="alert">
              {error} Try another card?
            </p>
          )}
          <div className="consent-actions">
            <button className="btn consent-approve" type="button" disabled={busy} onClick={startApprove}>
              {busy ? "Placing hold…" : `Approve & hold ${formatUsd(data.share_cents)}`}
            </button>
            {needOtp && <PasskeyConfirm busy={busy} onAssert={place} />}
            <button className="btn" type="button" disabled={busy} onClick={() => setShowDecline(true)}>Not for me</button>
            <p className="muted">Leave anytime before booking.</p>
            {data.reconfirmation?.available ? <button className="btn" type="button" onClick={() => setShowReconfirm(true)}>Review changed share</button> : data.reconfirmation ? <p className="muted">Your updated share isn’t ready to confirm yet. Please check back shortly.</p> : null}
          </div></section></div>
      )}

      {data?.plan_url && <p className="consent-plan-return"><Link className="btn" to={data.plan_url}>Back to your trip plan <Icon name="arrow"/></Link></p>}
      {showDecline && <DeclineSheet onPick={onDecline} onClose={() => setShowDecline(false)} />}
      {showReconfirm && data && (
        <ReconfirmSheet
          fromCents={data.reconfirmation?.from_cents ?? data.share_cents}
          toCents={data.reconfirmation?.to_cents ?? data.share_cents}
          capCents={cap}
          onConfirm={async () => {
            try {
              await reconfirmShare(data.share_id, data.member.id, { method: "otp", code: DEMO_OTP });
              setShowReconfirm(false);
              await refresh();
            } catch (err) { setError(err instanceof Error ? err.message : "Could not confirm the changed share."); setShowReconfirm(false); }
          }}
          onLeave={async () => {
            await leaveShare(data.share_id, data.member.id);
            setShowReconfirm(false);
            setScreen("declined");
          }}
          onClose={() => setShowReconfirm(false)}
        />
      )}
    </main>
  );
}
