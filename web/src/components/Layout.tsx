import { NavLink } from "react-router-dom";
import { type ReactNode } from "react";
import { useAuth } from "../auth";

export function Layout({ children }: { children: ReactNode }) {
  const { me, logout } = useAuth();
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          Enterprise Agent Platform
          <span className="tag">admin</span>
        </div>
        <nav className="nav">
          <NavLink to="/agents">Agents</NavLink>
          <NavLink to="/runs" end>
            Runs
          </NavLink>
          <NavLink to="/documents">Documents</NavLink>
        </nav>
        <div className="who">
          <span className="muted">
            {me?.tenant_slug} · {me?.email}
          </span>
          <button className="ghost" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>
      <main className="content">{children}</main>
    </div>
  );
}
