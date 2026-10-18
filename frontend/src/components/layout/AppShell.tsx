import { NavLink, Link, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { DEMO_GROUP } from "../../chat/demo";
import "./shell.css";
import tandemMark from "../../assets/tandem-mark.png";

export function Mark() {
  return <img className="tandem-mark" src={tandemMark} width="38" height="38" alt="" aria-hidden="true" />;
}
export function Icon({name}: {name: "grid"|"chat"|"arrow"|"shield"|"clock"|"people"}) {
  const paths = {grid:"M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",chat:"M21 11a8 8 0 0 1-8 8H7l-4 3V11a9 9 0 0 1 18 0Z M7 10h10 M7 14h6",arrow:"M5 12h14 M13 6l6 6-6 6",shield:"M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Z M8 12l3 3 5-6",clock:"M12 8v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",people:"M16 21v-3a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v3 M9 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M18 3a4 4 0 0 1 0 8 M22 21v-3a4 4 0 0 0-3-4"};
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]}/></svg>;
}
export default function AppShell({children, groupId=DEMO_GROUP.id, planName="Your buying plan"}: {children:ReactNode; groupId?:string; planName?:string}) {
  const path=useLocation().pathname;
  return <div className="app-layout"><a className="skip-link" href="#workspace">Skip to content</a><aside className="sidebar"><Link to="/" className="brand"><Mark/><span>tandem<span className="brand-period">.</span></span></Link><div className="workspace-label">YOUR SPACE</div><nav aria-label="Main navigation"><NavLink className={path.startsWith("/g/")?"active":""} to={`/g/${groupId}`}><Icon name="grid"/>Overview</NavLink><NavLink className={path.startsWith("/chat/")?"active":""} to={`/chat/${groupId}`}><Icon name="chat"/>Group chat</NavLink></nav><div className="sidebar-trip"><span className="trip-symbol">T</span><div><strong>{planName}</strong><small>One plan. Everyone in.</small></div></div><div className="sidebar-bottom"><Icon name="shield"/><p>Better together.<br/><span>No one fronts the money.</span></p><span className="demo-label">DEMO ENVIRONMENT</span></div></aside><div className="workspace" id="workspace" tabIndex={-1}>{children}</div></div>;
}
