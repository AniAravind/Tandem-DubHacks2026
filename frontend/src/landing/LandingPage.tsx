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
    </main>
  </div>;
}
