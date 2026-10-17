import { BrowserRouter, Routes, Route, Link } from "react-router-dom";
import ConsentPage from "./consent/ConsentPage";
import "./styles/tokens.css";

const Sim = () => <span className="sim-badge">🧪 SIMULATED PAYMENTS</span>;
const Chat = () => <main><h1>Web chat</h1><Sim /></main>;          // Aryan replaces
const Dashboard = () => <main><h1>Trip dashboard</h1><Sim /></main>; // Rishab replaces

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<main><h1>tandem</h1><Link to="/chat/demo">Open demo chat</Link><Sim /></main>} />
        <Route path="/chat/:groupId" element={<Chat />} />
        <Route path="/c/:token" element={<ConsentPage />} />
        <Route path="/g/:groupId" element={<Dashboard />} />
      </Routes>
    </BrowserRouter>
  );
}
