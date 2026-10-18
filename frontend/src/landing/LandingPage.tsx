import { Link } from "react-router-dom";
import tandemMark from "../assets/tandem-mark.png";
import PhoneStory from "./PhoneStory";
import "./landing.css";

export default function LandingPage() {
  return <div className="landing">
    <header className="landing-header">
      <a href="/" className="landing-brand" aria-label="Tandem home"><img src={tandemMark} alt="" width="34" height="34" /><span>tandem<span>.</span></span></a>
      <Link className="landing-try" to="/chat/demo">Try the demo</Link>
    </header>
    <main id="main">
      <PhoneStory />
      <section className="landing-steps" aria-label="How Tandem works">
        <article><span>1</span><h2>Text it</h2><p>Say what you’re buying and who’s in.</p></article>
        <article><span>2</span><h2>Pick together</h2><p>Compare options and vote in one link.</p></article>
        <article><span>3</span><h2>Pay your share</h2><p>Nobody’s charged until everyone’s in.</p></article>
      </section>
    </main>
    <footer className="landing-footer">Demo build · payments are simulated</footer>
  </div>;
}
