import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import ConsentPage from "./consent/ConsentPage";
import "./styles/tokens.css";
import Chat from "./chat/ChatPage";

import Dashboard from "./dashboard/DashboardPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/g/demo" replace />} />
        <Route path="/chat/:groupId" element={<Chat />} />
        <Route path="/c/:token" element={<ConsentPage />} />
        <Route path="/g/:groupId" element={<Dashboard />} />
      </Routes>
    </BrowserRouter>
  );
}
