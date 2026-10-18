import { useEffect, useState } from "react";
import tandemMark from "../assets/tandem-mark-blue.png";
import PhoneStory from "./PhoneStory";
import DemoRegistration from "./DemoRegistration";
import "./landing.css";

export default function LandingPage() {
  const hashTicket = () => new URLSearchParams(window.location.hash.slice(1)).get("demo") || "";
  const [ticket, setTicket] = useState(hashTicket);
  const [demoOpen, setDemoOpen] = useState(() => !!hashTicket());
  useEffect(() => {
    const changed = () => { const token = hashTicket(); if (token) { setTicket(token); setDemoOpen(true); } };
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  function closeDemo() {
    setDemoOpen(false);
    setTicket("");
    if (hashTicket()) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  return <div className="landing">
    <header className="landing-header">
      <a href="/" className="landing-brand" aria-label="Tandem home"><img src={tandemMark} alt="" width="34" height="34" /><span>tandem<span>.</span></span></a>
      <button className="landing-try" onClick={() => setDemoOpen(true)}>Try the demo</button>
    </header>
    <main id="main">
      <PhoneStory onTryDemo={() => setDemoOpen(true)} />
    </main>
    {demoOpen && <DemoRegistration initialToken={ticket} onClose={closeDemo} />}
  </div>;
}
