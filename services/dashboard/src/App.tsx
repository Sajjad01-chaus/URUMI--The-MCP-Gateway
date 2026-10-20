import { NavLink, Navigate, Outlet, Route, Routes } from "react-router-dom";

import { api } from "./api";
import { RequireAuth, useAuth, useCurrentUser } from "./auth";
import AcceptInvitePage from "./pages/AcceptInvite";
import AuditPage from "./pages/Audit";
import ChatPage from "./pages/Chat";
import ConnectPage from "./pages/Connect";
import KeysPage from "./pages/Keys";
import LoginPage from "./pages/Login";
import MembersPage from "./pages/Members";
import PermissionsPage from "./pages/Permissions";
import ServersPage from "./pages/Servers";
import StatsPage from "./pages/Stats";
import StoresPage from "./pages/Stores";
import ToolsPage from "./pages/Tools";
import { RoleBadge, useLoad } from "./ui";

function Layout() {
  const user = useCurrentUser();
  const { logout } = useAuth();
  const meta = useLoad(api.meta);
  const isAdmin = user.role === "admin";
  const link = ({ isActive }: { isActive: boolean }) => `nav-link${isActive ? " active" : ""}`;
  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Main">
        <div className="brand">
          <div className="brand-mark">U</div>
          <span>Urumi Gateway</span>
        </div>
        <div className="nav-section">Gateway</div>
        <NavLink to="/chat" className={link}>Chat</NavLink>
        <NavLink to="/stores" className={link}>Stores</NavLink>
        <NavLink to="/servers" className={link}>Servers</NavLink>
        <NavLink to="/tools" className={link}>Tools</NavLink>
        {isAdmin && <NavLink to="/permissions" className={link}>Tool permissions</NavLink>}
        <NavLink to="/audit" className={link}>Audit log</NavLink>
        {isAdmin && <NavLink to="/stats" className={link}>Stats</NavLink>}
        <div className="nav-section">Access</div>
        <NavLink to="/keys" className={link}>API keys</NavLink>
        <NavLink to="/connect" className={link}>Connect a client</NavLink>
        {isAdmin && <NavLink to="/members" className={link}>Members</NavLink>}
        <div className="sidebar-footer">
          <div>
            <div className="cell-main">{user.name || user.email}</div>
            <div className="cell-sub">{user.name && <>{user.email} · </>}<RoleBadge role={user.role} /></div>
          </div>
          <button className="button button-small" onClick={() => void logout()}>Sign out</button>
          <div className="cell-sub">gateway {meta.data?.gateway_version ?? "…"}</div>
        </div>
      </nav>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/invite/:token" element={<AcceptInvitePage />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/stores" element={<StoresPage />} />
        <Route path="/servers" element={<ServersPage />} />
        <Route path="/tools" element={<ToolsPage />} />
        <Route path="/permissions" element={<RequireAuth admin><PermissionsPage /></RequireAuth>} />
        <Route path="/members" element={<RequireAuth admin><MembersPage /></RequireAuth>} />
        <Route path="/keys" element={<KeysPage />} />
        <Route path="/connect" element={<ConnectPage />} />
        <Route path="/audit" element={<AuditPage />} />
        <Route path="/stats" element={<RequireAuth admin><StatsPage /></RequireAuth>} />
      </Route>
      <Route path="*" element={<Navigate to="/servers" replace />} />
    </Routes>
  );
}
