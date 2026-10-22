import { useState, type FormEvent } from "react";
import { ApiError } from "../api";
import { useAuth } from "../auth";

export function Login() {
  const { login } = useAuth();
  const [tenant, setTenant] = useState("acme");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(tenant.trim(), email.trim(), password);
    } catch (err) {
      // The API answers every bad login with one 401 and never says which field was wrong -
      // that is deliberate, so the form stays a door, not a directory of who has an account.
      // The UI must not leak more than the API does, so one message covers them all.
      if (err instanceof ApiError && err.status === 401) setError("Invalid credentials.");
      else if (err instanceof ApiError && err.status === 0) setError(err.message);
      else setError("Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="center">
      <form className="card login" onSubmit={onSubmit}>
        <h1 className="brand">
          Enterprise Agent Platform
          <span className="tag">admin</span>
        </h1>
        <label>
          Tenant
          <input value={tenant} onChange={(e) => setTenant(e.target.value)} autoFocus />
        </label>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && <div className="error">{error}</div>}
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
