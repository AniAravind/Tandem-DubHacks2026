import { useEffect, useId, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import CanCanNever from "./CanCanNever";
import DeclineSheet from "./DeclineSheet";
import ExpiredLink from "./ExpiredLink";
import HeldSuccess from "./HeldSuccess";
import { Alert, Flask, Logo, XCircle } from "./Icons";
import ListingHero from "./ListingHero";
import MandateBox from "./MandateBox";
import OtpConfirm, { requestPasskey } from "./PasskeyConfirm";
import ReconfirmSheet from "./ReconfirmSheet";
import SecureCardField, { last4FromPan } from "./SecureCardField";
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
import { formatUsd } from "./money";
import "./consent.css";

type Screen = "loading" | "form" | "held" | "declined" | "expired" | "forbidden";

export default function ConsentPage() {
  const { token = "" } = useParams();
  const [params] = useSearchParams();
  const newShare = Number(params.get("new_share") || 0);
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

  useEffect(() => {
    let alive = true;
    getConsent(token)
      .then((payload) => {
        if (!alive) return;
        setData(payload);
        setCap(payload.default_cap_cents);
        setScreen(payload.share_status === "held" ? "held" : payload.share_status === "declined" ? "declined" : "form");
        if (newShare && newShare !== payload.share_cents) setShowReconfirm(true);
      })
      .catch((err: { status?: number; message?: string }) => {
        if (!alive) return;
        setMessage(err.message || "");
        setScreen(err.status === 403 ? "forbidden" : "expired");
      });
    return () => { alive = false; };
  }, [token, newShare]);

  useEffect(() => {
    if (error) document.getElementById(errorId)?.focus();
  }, [error, errorId]);

  async function place(assertion: Assertion) {
    if (!data) return;
    setBusy(true);
    setError("");
    try {
      const tok = await tokenize(token, last4FromPan(pan));
      setLast4(tok.card.last4);
      await approve(token, {
        agent_token_id: tok.agent_token_id,
        cap_cents: cap,
        consent_text_sha256: data.consent_text_sha256,
        assertion,
      });
      navigator.vibrate?.(10);
      setScreen("held");
    } catch (err: unknown) {
      const e = err as { code?: string; message?: string };
      const base = e.message || "Something went wrong. Nothing was charged.";
      setError(e.code === "card_declined" ? `${base} Try another card?` : base);
    } finally {
      setBusy(false);
    }
  }

  async function startApprove() {
    setError("");
    try {
      await place(await requestPasskey(data?.member.display_name || "member"));
    } catch {
      setNeedOtp(true);
    }
  }

  async function onDecline(reason: string) {
    setShowDecline(false);
    setBusy(true);
    try {
      await declineConsent(token, reason);
      setScreen("declined");
    } catch (err: unknown) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function onReconfirm() {
    if (!data) return;
    setBusy(true);
    try {
      await reconfirmShare(data.share_id, data.member.id, { method: "otp", code: DEMO_OTP });
      setShowReconfirm(false);
      setScreen("held");
    } finally {
      setBusy(false);
    }
  }

  async function onLeave() {
    if (!data) return;
    setBusy(true);
    try {
      await leaveShare(data.share_id, data.member.id).catch(() => declineConsent(token, "prefer_not_to_say"));
      setShowReconfirm(false);
      setScreen("declined");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="consent-shell">
      <div className="consent-page">
        <header className="consent-header">
          <span className="brand"><Logo />tandem</span>
          <span className="sim-pill"><Flask size={14} />SIMULATED PAYMENT</span>
        </header>

        {screen === "loading" && (
          <div aria-busy="true" aria-label="Loading your share">
            <div className="skeleton sk-hero" />
            <div className="skeleton sk-card" />
            <div className="skeleton sk-card" />
          </div>
        )}

        {screen === "expired" && <ExpiredLink message={message} />}
        {screen === "forbidden" && <ExpiredLink forbidden />}

        {screen === "declined" && (
          <section className="card state">
            <div className="state-icon neutral"><XCircle size={34} /></div>
            <h1 className="state-title">No problem. Nothing was held.</h1>
            <p className="muted">The group only sees that 1 spot opened up. Your reason stays private.</p>
          </section>
        )}

        {screen === "held" && data && (
          <HeldSuccess shareCents={data.share_cents} last4={last4} splitCount={data.purchase.split_count} groupId={data.group_id} />
        )}

        {screen === "form" && data && (
          <>
            <ListingHero data={data} />
            <ShareSummary data={data} />
            <MandateBox data={data} cap={cap} onCap={setCap} />
            <CanCanNever can={data.can} canNever={data.can_never} />
            <SecureCardField pan={pan} onPan={setPan} errorId={error ? errorId : undefined} />
            {error && (
              <p className="error" id={errorId} tabIndex={-1} role="alert"><Alert size={18} />{error}</p>
            )}
            <p className="sim-note"><Flask size={18} />Simulated payment: no real money moves in this demo. Use test cards.</p>

            <div className="actions">
              {needOtp ? (
                <OtpConfirm busy={busy} onAssert={place} />
              ) : (
                <button className="btn btn-primary btn-block" type="button" disabled={busy} onClick={startApprove}>
                  {busy ? <><span className="spinner" aria-hidden="true" />Placing hold…</> : `Approve & hold ${formatUsd(data.share_cents)}`}
                </button>
              )}
              <button className="btn btn-block" type="button" disabled={busy} onClick={() => setShowDecline(true)}>
                Not for me
              </button>
              <p className="leave-note">Confirm with Face ID / passkey · Leave anytime before booking</p>
            </div>
          </>
        )}

        {showDecline && <DeclineSheet onConfirm={onDecline} onClose={() => setShowDecline(false)} />}
        {showReconfirm && data && (
          <ReconfirmSheet
            fromCents={data.share_cents}
            toCents={newShare}
            capCents={cap}
            busy={busy}
            onConfirm={onReconfirm}
            onLeave={onLeave}
            onClose={() => setShowReconfirm(false)}
          />
        )}
      </div>
    </div>
  );
}
