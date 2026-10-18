import tandemMark from "../assets/tandem-mark.png";
import "./landing.css";

export default function LandingPage() {
  return <div className="landing">
    <header className="landing-header">
      <a href="/" className="landing-brand" aria-label="Tandem home"><img src={tandemMark} alt="" width="40" height="40" /><span>tandem<span>.</span></span></a>
      <span className="landing-header-note">A little less back-and-forth.</span>
    </header>
    <main id="main" className="landing-main">
      <section className="landing-hero" aria-labelledby="landing-title">
        <div className="landing-copy">
          <p className="landing-eyebrow"><span aria-hidden="true">✳</span> GOOD PLANS START WITH A MESSAGE</p>
          <h1 id="landing-title">Less figuring it out.<br /><em>More making it happen.</em></h1>
          <p className="landing-description">The weekend away. The perfect gift. The thing everyone’s been talking about. Tell Tandem what you have in mind—we’ll help you find options and bring the plan together.</p>
          <div className="landing-start"><span aria-hidden="true">↗</span><div><strong>Start in your messages.</strong><p>Your agent sends a private link when your plan is ready. Invite your people, choose together, and approve your own share.</p></div></div>
          <p className="landing-footnote">Just you? Tandem can help with that, too.</p>
        </div>
        <figure className="landing-conversation" aria-label="An example of planning with Tandem">
          <figcaption><img src={tandemMark} alt="" width="30" height="30" /><span>One message. A starting point.</span></figcaption>
          <div className="landing-bubble landing-bubble-user">A birthday gift for Maya, split between four of us. $30 each 🎁</div>
          <div className="landing-bubble landing-bubble-agent">Let’s find something she’ll love. What’s she into?</div>
          <div className="landing-bubble landing-bubble-user">Coffee, cooking, and a good game night.</div>
          <div className="landing-bubble landing-bubble-agent">Good starting point ☕ I’ll help narrow it down, then send you a link to bring everyone in.</div>
          <div className="landing-conversation-caption"><span aria-hidden="true">✦</span> From “we should” to a shared plan.</div>
        </figure>
      </section>
      <section className="landing-steps" aria-label="How Tandem works">
        <article><span className="landing-step-number">01</span><h2>Talk it through.</h2><p>Tell your agent what you’re buying, who’s in, and what you want to spend.</p></article>
        <article><span className="landing-step-number">02</span><h2>Find your fit.</h2><p>Narrow down the options and get a link to your shared plan.</p></article>
        <article><span className="landing-step-number">03</span><h2>Bring everyone along.</h2><p>Join from the link in your messages. Each person approves their own payment.</p></article>
      </section>
    </main>
    <footer className="landing-footer"><span>Better plans, together.</span><span>Plans open from your agent’s links. No account needed.</span></footer>
  </div>;
}
