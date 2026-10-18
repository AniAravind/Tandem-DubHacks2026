import { BrowserRouter, Routes, Route } from "react-router-dom";
import LandingPage from "./landing/LandingPage";
import ConsentPage from "./consent/ConsentPage";
import "./styles/tokens.css";
import Chat from "./chat/ChatPage";

import Dashboard from "./dashboard/DashboardPage";
import PlanPage from "./plans/PlanPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/chat/:groupId" element={<Chat />} />
        <Route path="/c/:token" element={<ConsentPage />} />
        <Route path="/p/:planId" element={<PlanPage />} />
        <Route path="/g/:groupId" element={<Dashboard />} />
      </Routes>
    </BrowserRouter>
  );
}
