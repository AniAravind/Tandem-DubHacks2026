import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import CanCanNever from "./CanCanNever";
import DeclineSheet from "./DeclineSheet";
import HeldSuccess from "./HeldSuccess";
import ListingHero from "./ListingHero";
import MandateBox from "./MandateBox";
import PasskeyConfirm from "./PasskeyConfirm";
import { requestPasskey } from "./passkey";
import ReconfirmPanel from "./ReconfirmPanel";
import SecureCardField from "./SecureCardField";
import ShareSummary from "./ShareSummary";
import StatusScreen, { type NextLink } from "./StatusScreen";
import {
  approve,
  declineConsent,
  getConsent,
  leaveShare,
  reconfirmShare,
  tokenize,
  type Assertion,
  type ConsentPayload,
  type ConsentRequestError,
} from "./api";
import { formatUsd, last4FromPan } from "./money";
import { Mark } from "../components/layout/AppShell";
import "./consent.css";

type Screen = "loading" | "form" | "held" | "declined" | "expired" | "forbidden" | "locked" | "captured" | "released" | "refunded";
type Action = "approve" | "reconfirm";

const POLL_MS = 3000;
const GONE = new Set(["link_expired", "link_superseded", "deadline_passed", "share_superseded"]);
const LOCKED = new Set(["too_many_declines", "rate_limited"]);

function screenFor(p: ConsentPayload): Screen {
  switch (p.share_status) {
    case "held": case "captured": case "released": case "refunded": case "declined":
      return p.share_status;
    case "card_failed":
      return p.attempts_left === 0 ? "locked" : "form";
    default:
      return "form";
  }
}

function nextLinks(d: ConsentPayload | null): NextLink[] {
  if (!d) return [];
  return [
    d.plan_url ? { to: d.plan_url, label: "Back to your plan" } : { to: `/chat/${d.group_id}`, label: "Back to group chat" },
    { to: `/g/${d.group_id}`, label: "View group overview" },
  ];
}

function triesLeft(n: number | undefined): string {
  return n == null ? "" : ` ${n} ${n === 1 ? "try" : "tries"} left.`;
}

export default function ConsentPage() {
  const { token = "" } = useParams();
  const [screen, setScreen] = useState<Screen>("loading");
  const [data, setData] = useState<ConsentPayload | null>(null);
  const [cap, setCap] = useState(0);
  const [pan, setPan] = useState("4111111111111111");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [last4, setLast4] = useState("");
  const [confirming, setConfirming] = useState<Action | null>(null);
  const [declining, setDeclining] = useState(false);
  const errorId = useId();
  const cardToken = useRef<{ last4: string; transient: string; id?: string } | null>(null);
  const passkeyUnavailable = useRef(false);
  const passkeyAbort = useRef<AbortController | null>(null);
  const [verifying, setVerifying] = useState(false);

  const applyStatus = useCallback((payload: ConsentPayload) => {
    setData(payload);
    setScreen(screenFor(payload));
  }, []);

  const showGone = useCallback((err: ConsentRequestError) => {
    setNotice(err.code === "link_superseded" || err.code === "share_superseded"
      ? "The plan changed, so this link was replaced. Open the newest private link Tandem sent you to confirm your updated share."
      : err.message || "This link expired. Ask Tandem in your chat for a new one.");
    setScreen("expired");
  }, []);

  const refresh = useCallback(async () => {
    try {
      applyStatus(await getConsent(token));
    } catch (err) {
      const e = err as ConsentRequestError;
      if (e.code && GONE.has(e.code)) showGone(e);
      else throw e;
    }
  }, [token, applyStatus, showGone]);

  useEffect(() => {
    let alive = true;
    getConsent(token)
      .then((payload) => {
        if (!alive) return;
        setCap(payload.default_cap_cents);
        applyStatus(payload);
      })
      .catch((err: ConsentRequestError) => {
        if (!alive) return;
        if (err.status === 403) setScreen("forbidden");
        else showGone(err);
      });
    return () => { alive = false; };
  }, [token, applyStatus, showGone]);

  useEffect(() => {
    if (screen !== "held") return;
    const timer = setInterval(() => { refresh().catch(() => undefined); }, POLL_MS);
    return () => clearInterval(timer);
  }, [screen, refresh]);

  function fail(message: string) {
    setError(message);
    requestAnimationFrame(() => document.getElementById(errorId)?.focus());
  }

  async function handleError(err: unknown) {
    const e = err as ConsentRequestError;
    const code = e.code ?? "";
    if (GONE.has(code)) return showGone(e);
    if (LOCKED.has(code)) { setNotice(e.message); setScreen("locked"); return; }
    if (code === "card_declined") {
      setConfirming(null);
      setData((d) => d && { ...d, share_status: "card_failed", attempts_left: e.attempts_left ?? d.attempts_left });
      if (e.attempts_left === 0) { setNotice("Your card was declined 3 times, so new attempts on this share are paused."); setScreen("locked"); return; }
      return fail(`${e.message} Try a different card.${triesLeft(e.attempts_left)}`);
    }
    if (code === "token_inactive") cardToken.current = null;
    if (code === "consent_text_changed" || code === "illegal_state") {
      setConfirming(null);
      await refresh().catch(() => undefined);
      return fail(code === "consent_text_changed" ? "Some details changed. Review them and approve again." : e.message);
    }
    if (code !== "otp_invalid") setConfirming(null);
    fail(e.message || "Something went wrong. Nothing was charged.");
  }

  async function placeHold(assertion: Assertion) {
    const digits = pan.replace(/\D/g, "");
    const cardLast4 = last4FromPan(digits);
    if (!cardToken.current || cardToken.current.last4 !== cardLast4) {
      cardToken.current = { last4: cardLast4, transient: crypto.randomUUID().replaceAll("-", "") };
    }
    const attempt = cardToken.current;
    if (!attempt.id) {
      const tok = await tokenize(token, cardLast4, attempt.transient);
      attempt.id = tok.agent_token_id;
    }
    setLast4(attempt.last4);
    const rendered = await getConsent(token, cap);
    await approve(token, { agent_token_id: attempt.id, cap_cents: cap, consent_text_sha256: rendered.consent_text_sha256, assertion });
  }

  async function run(action: Action, assertion: Assertion) {
    if (!data) return;
    setBusy(true);
    setError("");
    try {
      if (action === "approve") await placeHold(assertion);
      else await reconfirmShare(data.share_id, data.member.id, assertion);
      setConfirming(null);
      if (navigator.vibrate) navigator.vibrate(10);
      await refresh();
    } catch (err) {
      await handleError(err);
    } finally {
      setBusy(false);
    }
  }

  async function start(action: Action) {
    if (!data) return;
    if (action === "approve" && pan.replace(/\D/g, "").length !== 16) return fail("Enter the full 16-digit test card number.");
    setError("");
    if (!passkeyUnavailable.current) {
      const abort = new AbortController();
      passkeyAbort.current = abort;
      setVerifying(true);
      const assertion = await requestPasskey(data.member.display_name, abort.signal);
      if (passkeyAbort.current !== abort) return;
      setVerifying(false);
      passkeyAbort.current = null;
      if (assertion) return run(action, assertion);
      passkeyUnavailable.current = true;
    }
    setConfirming(action);
  }

  function switchToCode(action: Action) {
    passkeyAbort.current?.abort();
    passkeyAbort.current = null;
    passkeyUnavailable.current = true;
    setVerifying(false);
    setConfirming(action);
  }

  async function onDecline(reason: string) {
    await declineConsent(token, reason);
    setDeclining(false);
    await refresh();
  }

  async function onLeave() {
    if (!data) return;
    await leaveShare(data.share_id, data.member.id);
    await refresh();
  }

  const links = nextLinks(data);
  const change = data?.reconfirmation;
  const quickConfirm = Boolean(change?.within_cap && change.available);
  const action: Action = quickConfirm ? "reconfirm" : "approve";
  const actionLabel = data ? (quickConfirm ? `Confirm ${formatUsd(data.share_cents)}` : `Approve & hold ${formatUsd(data.share_cents)}`) : "";

  return (
    <main className="consent-page">
      <header className="consent-header">
        <Link to="/" className="brand"><Mark /><span>tandem<span className="brand-period">.</span></span></Link>
        <span className="payment-preview-note">Payment preview &middot; no real charges</span>
      </header>

      {screen === "loading" && (
        <div className="consent-skeleton" aria-busy="true" aria-label="Loading your share">
          <div /><div /><div />
        </div>
      )}

      {screen === "expired" && (
        <StatusScreen tone="pending" eyebrow="LINK NO LONGER ACTIVE" title="This link expired" links={links}>
          <p className="status-lede">{notice}</p>
          <p className="fine">Nothing was held or charged through this link.</p>
        </StatusScreen>
      )}
      {screen === "forbidden" && (
        <StatusScreen tone="ink" eyebrow="PRIVATE LINK" title="This link isn’t for you">
          <p className="status-lede">Links are personal. Open the one Tandem sent to you privately.</p>
        </StatusScreen>
      )}
      {screen === "locked" && (
        <StatusScreen tone="decline" eyebrow="HOLD PAUSED" title="We couldn’t place your hold" links={links}>
          <p className="status-lede">{notice || "Too many attempts on this share."} Nothing was charged.</p>
          <p className="fine">You can still say no, so the group can keep the plan moving without you.</p>
          <button className="btn" type="button" onClick={() => setDeclining(true)}>Not for me</button>
        </StatusScreen>
      )}
      {screen === "declined" && (
        <StatusScreen tone="ink" eyebrow="YOU'RE OUT" title="No problem. Nothing is held." links={links}>
          <p className="status-lede">{change ? "Your earlier hold was released too. " : ""}Your reason stays private. The group only sees that a spot opened up.</p>
        </StatusScreen>
      )}
      {screen === "released" && data && (
        <StatusScreen tone="ink" eyebrow="HOLD RELEASED" title="Your hold was released" links={links}>
          <p className="status-lede">
            {["failed", "released"].includes(data.purchase_state ?? "")
              ? "Not everyone was in before the deadline, so every hold was released."
              : "You left this purchase."} Nothing was charged.
          </p>
        </StatusScreen>
      )}
      {screen === "captured" && data && (
        <StatusScreen tone="funded" eyebrow="BOOKED TOGETHER" title="Paid. Booking confirmed." links={links}>
          <p className="status-lede"><strong className="money">{formatUsd(data.share_cents)}</strong>, your exact share of {data.purchase.title}, was captured after the signed checkout succeeded.</p>

        </StatusScreen>
      )}
      {screen === "refunded" && data && (
        <StatusScreen tone="funded" eyebrow="REFUNDED" title="Your share was refunded" links={links}>
          <p className="status-lede">The booking was cancelled and your share went back to your card.</p>

        </StatusScreen>
      )}
      {screen === "held" && data && <HeldSuccess data={data} last4={last4} links={links} onLeave={onLeave} />}

      {screen === "form" && data && (
        <div className="consent-form-grid">
          <div className="consent-col">
            <section className="consent-intro" aria-labelledby="consent-title">
              <h1 id="consent-title">Hi {data.member.display_name}, here’s your share.</h1>
              <p className="consent-invitation">
                {data.invited_by && data.invited_by !== data.member.display_name ? `${data.invited_by} invited you to ` : "You’re organizing "}
                <strong>{data.group_name}</strong>.
              </p>
              <ListingHero photoUrl={data.purchase.photo_url} />
              <h2 className="consent-listing-title">{data.purchase.title}</h2>
              <p className="muted">{[data.purchase.dates, data.purchase.merchant_name].filter(Boolean).join(" · ")}</p>
            </section>
            <section className="consent-promises" aria-label="What Tandem can and can never do">
              <CanCanNever can={data.can} canNever={data.can_never} />
            </section>
          </div>

          <div className="consent-col consent-review">
            <section className="consent-terms" aria-label="Review your share">
              {change && <ReconfirmPanel change={change} />}
              {data.share_status === "card_failed" && !error && (
                <p className="notice decline" role="status">
                  Your last card wasn’t approved. Nothing was charged. Try a different card.{triesLeft(data.attempts_left)}
                </p>
              )}
              <ShareSummary data={data} />
              {!quickConfirm && (
                <MandateBox data={data} cap={cap} onCap={(value) => { setCap(value); getConsent(token, value).then(setData).catch(() => undefined); }} />
              )}
            </section>
            <section className="consent-pay" aria-label="Approve or decline">
              {!quickConfirm && (
                <SecureCardField
                  pan={pan}
                  disabled={busy || verifying || confirming !== null}
                  onPan={(value) => { setPan(value); setError(""); }}
                  errorId={error ? errorId : undefined}
                />
              )}
              {error && <p className="error" id={errorId} tabIndex={-1} role="alert">{error}</p>}
              <div className="consent-actions">
                {confirming ? (
                  <PasskeyConfirm busy={busy} submitLabel={actionLabel} onAssert={(a) => run(confirming, a)} onCancel={() => { setConfirming(null); setError(""); }} />
                ) : (
                  <div className="choice-row">
                    <button className="btn primary consent-choice" type="button" disabled={busy || verifying} onClick={() => start(action)}>
                      {verifying ? "Waiting for passkey…" : busy ? "Placing hold…" : actionLabel}
                    </button>
                    {verifying ? (
                      <button className="btn consent-choice" type="button" onClick={() => switchToCode(action)}>Use a code instead</button>
                    ) : (
                      <button className="btn consent-choice" type="button" disabled={busy} onClick={() => setDeclining(true)}>Not for me</button>
                    )}
                  </div>
                )}
                <p className="fine center">{quickConfirm ? "Same card, same merchant. Only the amount changes." : "Review your approval. You can leave before the plan is finalized."}</p>
              </div>
            </section>
          </div>
        </div>
      )}

      {declining && <DeclineSheet onPick={onDecline} onClose={() => setDeclining(false)} />}
    </main>
  );
}
