import { useEffect, useState } from "react";
import QRCode from "qrcode";
import {
  changeOwnPassword,
  createTeam,
  createUser,
  deleteTeam,
  deleteUser,
  disableOtp,
  enableOtp,
  fetchSimulationStatus,
  fetchUsers,
  resetFleet,
  resolveAllOpenIncidents,
  setSimulationEnabled,
  startOtpSetup,
  updateTeam,
  updateUser,
} from "../api.js";
import { loadSession, ROLES, useCurrentUser, useHasRole } from "../auth.js";

const ROLE_HINT = {
  viewer: "read-only",
  operator: "+ acknowledge, dispatch and resolve incidents",
  admin: "+ fleet, teams, simulation, users",
};

export default function SettingsPanel({ teams, onTeamsChanged, onSessionChanged, onFleetReset, onClose }) {
  const isAdmin = useHasRole("admin");
  const [tab, setTab] = useState("security");

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card settings-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Settings</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="settings-tabs">
          <button className={tab === "security" ? "active" : ""} onClick={() => setTab("security")}>
            Security
          </button>
          {isAdmin && (
            <button className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}>
              Users
            </button>
          )}
          <button className={tab === "teams" ? "active" : ""} onClick={() => setTab("teams")}>
            Teams
          </button>
          <button className={tab === "simulation" ? "active" : ""} onClick={() => setTab("simulation")}>
            Simulation
          </button>
        </div>

        {tab === "security" && <SecurityTab onSessionChanged={onSessionChanged} />}
        {tab === "users" && isAdmin && <UsersTab />}
        {tab === "teams" && <TeamsTab teams={teams} onChanged={onTeamsChanged} />}
        {tab === "simulation" && <SimulationTab onFleetReset={onFleetReset} />}
      </div>
    </div>
  );
}

function ChangePasswordForm({ onSessionChanged }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setDone(false);
    if (next !== confirm) {
      setError("Passwords don't match");
      return;
    }
    try {
      // Revokes every other session; the response carries this tab's new token.
      onSessionChanged(await changeOwnPassword(current, next));
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <form className="otp-setup" onSubmit={handleSubmit}>
      <label>
        Current password
        <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
      </label>
      <label>
        New password
        <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
      </label>
      <label>
        Confirm new password
        <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      </label>
      {error && <div className="login-error">{error}</div>}
      {done && <p className="settings-hint">Password changed. Your other sessions have been signed out.</p>}
      <button type="submit" className="login-submit">
        Change password
      </button>
    </form>
  );
}

function SecurityTab({ onSessionChanged }) {
  const currentUser = useCurrentUser();
  const enabled = currentUser.otp_enabled;
  const [setup, setSetup] = useState(null);
  const [qrDataUrl, setQrDataUrl] = useState(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [disableCode, setDisableCode] = useState("");

  useEffect(() => {
    if (!setup) {
      setQrDataUrl(null);
      return;
    }
    QRCode.toDataURL(setup.otpauth_uri, { margin: 1, width: 220 }).then(setQrDataUrl);
  }, [setup]);

  // OTP changes only touch the user object - the token stays valid.
  const updateCurrentUser = (user) => {
    onSessionChanged({ ...loadSession(), user });
  };

  const startSetup = async () => {
    setError("");
    setCode("");
    try {
      setSetup(await startOtpSetup());
    } catch (err) {
      setError(err.message);
    }
  };

  const cancelSetup = () => {
    setSetup(null);
    setCode("");
    setError("");
  };

  const confirmSetup = async (e) => {
    e.preventDefault();
    try {
      updateCurrentUser(await enableOtp(setup.secret, code));
      setSetup(null);
      setCode("");
      setError("");
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDisable = async (e) => {
    e.preventDefault();
    try {
      updateCurrentUser(await disableOtp(disableCode));
      setDisableCode("");
      setError("");
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <>
      <p className="settings-hint">
        Signed in as <strong>{currentUser.username}</strong> with the <strong>{currentUser.role}</strong> role (
        {ROLE_HINT[currentUser.role]}).
      </p>

      <h3>One-time password (OTP)</h3>

      {!enabled && !setup && (
        <>
          <p className="settings-hint">
            Two-factor authentication is currently <strong>disabled</strong>. Enable it to require a 6-digit
            authenticator code at login, in addition to the password.
          </p>
          {error && <div className="login-error">{error}</div>}
          <button className="login-submit" onClick={startSetup}>
            Enable OTP
          </button>
        </>
      )}

      {!enabled && setup && (
        <form onSubmit={confirmSetup} className="otp-setup">
          <p className="settings-hint">
            Scan this QR code with an authenticator app (Google Authenticator, Authy, 1Password,
            etc.), or enter the key manually.
          </p>
          {qrDataUrl && <img className="otp-qr" src={qrDataUrl} alt="OTP QR code" />}
          <code className="otp-secret">{setup.secret}</code>
          <label>
            Enter the 6-digit code from your app to confirm
            <input
              type="text"
              inputMode="numeric"
              maxLength={6}
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
            />
          </label>
          {error && <div className="login-error">{error}</div>}
          <div className="otp-setup-actions">
            <button type="button" onClick={cancelSetup}>
              Cancel
            </button>
            <button type="submit" className="login-submit">
              Verify &amp; enable
            </button>
          </div>
        </form>
      )}

      {enabled && (
        <>
          <p className="settings-hint">
            Two-factor authentication is currently <strong>enabled</strong>. You'll be asked for a 6-digit
            code after your password at login.
          </p>
          <form onSubmit={handleDisable} className="otp-setup">
            <label>
              Enter your current 6-digit code to disable OTP
              <input
                type="text"
                inputMode="numeric"
                maxLength={6}
                value={disableCode}
                onChange={(e) => setDisableCode(e.target.value.replace(/\D/g, ""))}
              />
            </label>
            {error && <div className="login-error">{error}</div>}
            <button type="submit" className="logout-button">
              Disable OTP
            </button>
          </form>
        </>
      )}

      <h3>Password</h3>
      <ChangePasswordForm onSessionChanged={onSessionChanged} />
    </>
  );
}

function UsersTab() {
  const currentUser = useCurrentUser();
  const [users, setUsers] = useState([]);
  const [error, setError] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [newRole, setNewRole] = useState("viewer");
  const [resetTarget, setResetTarget] = useState(null);
  const [resetPassword, setResetPassword] = useState("");

  const refresh = () =>
    fetchUsers()
      .then(setUsers)
      .catch((err) => setError(err.message));

  useEffect(() => {
    refresh();
  }, []);

  // Every action below reports the backend's own refusal (last admin,
  // deleting yourself, weak password, ...) rather than duplicating its rules.
  const run = async (action) => {
    setError("");
    try {
      await action();
      await refresh();
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    }
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      setError("Passwords don't match");
      return;
    }
    const ok = await run(() => createUser({ username: newUsername, password: newPassword, role: newRole }));
    if (ok) {
      setNewUsername("");
      setNewPassword("");
      setConfirmPassword("");
      setNewRole("viewer");
    }
  };

  const handleRoleChange = (u, role) => run(() => updateUser(u.id, { role }));

  const handleDelete = (u) => {
    if (!window.confirm(`Delete user "${u.username}"? This can't be undone.`)) return;
    run(() => deleteUser(u.id));
  };

  const handleResetOtp = (u) => {
    if (
      !window.confirm(
        `Turn off two-factor authentication for "${u.username}"? Use this if they lost access to their authenticator app. It also signs them out everywhere.`
      )
    )
      return;
    run(() => updateUser(u.id, { reset_otp: true }));
  };

  const handleResetPassword = async (e) => {
    e.preventDefault();
    const ok = await run(() => updateUser(resetTarget.id, { password: resetPassword }));
    if (ok) {
      setResetTarget(null);
      setResetPassword("");
    }
  };

  return (
    <>
      <h3>Users</h3>

      {error && <div className="login-error">{error}</div>}

      <div className="table-scroll">
        <table className="users-table">
          <thead>
            <tr>
              <th>Username</th>
              <th>Role</th>
              <th>OTP</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>
                  {u.username}
                  {u.id === currentUser.id && <span className="you-tag"> (you)</span>}
                </td>
                <td>
                  <select value={u.role} onChange={(e) => handleRoleChange(u, e.target.value)}>
                    {ROLES.map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                </td>
                <td>
                  {u.otp_enabled ? (
                    <span className="badge up">enabled</span>
                  ) : (
                    <span className="badge unknown">disabled</span>
                  )}
                </td>
                <td className="users-table-actions">
                  <button type="button" onClick={() => { setResetTarget(u); setResetPassword(""); setError(""); }}>
                    Reset password
                  </button>
                  {u.otp_enabled && u.id !== currentUser.id && (
                    <button type="button" onClick={() => handleResetOtp(u)}>
                      Reset OTP
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => handleDelete(u)}
                    disabled={u.id === currentUser.id}
                    title={u.id === currentUser.id ? "Can't delete the account you're signed in as" : undefined}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {resetTarget && (
        <form className="otp-setup reset-password-form" onSubmit={handleResetPassword}>
          <label>
            New password for {resetTarget.username} (signs them out everywhere)
            <input
              type="password"
              autoFocus
              value={resetPassword}
              onChange={(e) => setResetPassword(e.target.value)}
            />
          </label>
          <div className="otp-setup-actions">
            <button type="button" onClick={() => setResetTarget(null)}>
              Cancel
            </button>
            <button type="submit" className="login-submit">
              Set password
            </button>
          </div>
        </form>
      )}

      <h3>Add user</h3>
      <form className="otp-setup create-user-form" onSubmit={handleCreate}>
        <label>
          Username
          <input type="text" value={newUsername} onChange={(e) => setNewUsername(e.target.value)} />
        </label>
        <label>
          Role
          <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>{r} — {ROLE_HINT[r]}</option>
            ))}
          </select>
        </label>
        <label>
          Password
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
          />
        </label>
        <label>
          Confirm password
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
          />
        </label>
        <button type="submit" className="login-submit">
          Create user
        </button>
      </form>
    </>
  );
}

const TEAM_KIND_LABEL = { maintenance: "Maintenance", soc: "SOC" };

function TeamList({ kind, teams, onChanged }) {
  const isAdmin = useHasRole("admin");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState("");
  const [editEmail, setEditEmail] = useState("");
  const [saving, setSaving] = useState(false);

  const handleAdd = async (e) => {
    e.preventDefault();
    if (!name.trim() || !email.trim()) return;
    setSaving(true);
    try {
      await createTeam({ kind, name: name.trim(), email: email.trim() });
      setName("");
      setEmail("");
      await onChanged();
    } finally {
      setSaving(false);
    }
  };

  const startEdit = (team) => {
    setEditingId(team.id);
    setEditName(team.name);
    setEditEmail(team.email);
  };

  const saveEdit = async (team) => {
    setSaving(true);
    try {
      await updateTeam(team.id, { kind, name: editName.trim(), email: editEmail.trim() });
      setEditingId(null);
      await onChanged();
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (team) => {
    if (!window.confirm(`Remove ${team.name} (${team.email})?`)) return;
    await deleteTeam(team.id);
    await onChanged();
  };

  return (
    <div style={{ flex: 1, minWidth: 280 }}>
      <h3>{TEAM_KIND_LABEL[kind]} teams</h3>
      <div className="table-scroll">
        <table className="users-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Email</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {teams.map((team) => (
              <tr key={team.id}>
                {editingId === team.id ? (
                  <>
                    <td>
                      <input value={editName} onChange={(e) => setEditName(e.target.value)} />
                    </td>
                    <td>
                      <input value={editEmail} onChange={(e) => setEditEmail(e.target.value)} />
                    </td>
                    <td>
                      <span className="link" onClick={() => saveEdit(team)}>Save</span>{" "}
                      <span className="link" onClick={() => setEditingId(null)}>Cancel</span>
                    </td>
                  </>
                ) : (
                  <>
                    <td>{team.name}</td>
                    <td>{team.email}</td>
                    <td>
                      {isAdmin && (
                        <>
                          <span className="link" onClick={() => startEdit(team)}>Edit</span>{" "}
                          <span className="link" onClick={() => handleDelete(team)}>Remove</span>
                        </>
                      )}
                    </td>
                  </>
                )}
              </tr>
            ))}
            {teams.length === 0 && (
              <tr>
                <td colSpan={3} style={{ color: "#5b6479" }}>No {TEAM_KIND_LABEL[kind].toLowerCase()} teams configured.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {isAdmin && (
      <form onSubmit={handleAdd} style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.6rem" }}>
        <input
          placeholder="Team name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          style={{ flex: 1 }}
        />
        <input
          placeholder="Email address"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          style={{ flex: 1 }}
        />
        <button type="submit" disabled={saving}>Add</button>
      </form>
      )}
    </div>
  );
}

function TeamsTab({ teams, onChanged }) {
  const maintenanceTeams = teams.filter((t) => t.kind === "maintenance");
  const socTeams = teams.filter((t) => t.kind === "soc");
  return (
    <div style={{ display: "flex", gap: "2rem", flexWrap: "wrap" }}>
      <TeamList kind="maintenance" teams={maintenanceTeams} onChanged={onChanged} />
      <TeamList kind="soc" teams={socTeams} onChanged={onChanged} />
    </div>
  );
}

function SimulationTab({ onFleetReset }) {
  const isAdmin = useHasRole("admin");
  const [enabled, setEnabled] = useState(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetchSimulationStatus()
      .then((s) => setEnabled(s.enabled))
      .catch((err) => setError(`Couldn't load simulation status: ${err.message}`));
  }, []);

  const toggle = async () => {
    setSaving(true);
    setError("");
    try {
      const s = await setSimulationEnabled(!enabled);
      setEnabled(s.enabled);
    } catch (err) {
      setError(`Couldn't update simulation status: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <h3>Trap simulation</h3>

      {error && <div className="login-error">{error}</div>}

      {enabled === null && !error && <p className="settings-hint">Loading…</p>}

      {enabled !== null && (
        <>
          <p className="settings-hint">
            The simulator is currently <strong>{enabled ? "running" : "paused"}</strong>.{" "}
            {enabled
              ? "It's generating SNMP traps across the fleet (link flaps, BGP/CPU/temperature events, etc.)."
              : "No new traps are being sent - existing routers, incidents, and backups are untouched."}
          </p>
          {isAdmin ? (
            <button className="login-submit" onClick={toggle} disabled={saving}>
              {saving ? "Saving…" : enabled ? "Pause simulation" : "Resume simulation"}
            </button>
          ) : (
            <p className="settings-hint">Only admins can pause or resume the simulation.</p>
          )}
        </>
      )}

      {isAdmin && <ResetIncidents />}
      {isAdmin && <DeleteFleet onFleetReset={onFleetReset} />}
    </>
  );
}

// Debug/demo reset, admin-only - kept out of the operator views so it can't
// be mistaken for normal incident handling.
function ResetIncidents() {
  const [resetting, setResetting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const reset = async () => {
    if (
      !window.confirm(
        "Resolve ALL open incidents system-wide and mark every link and BGP session back up? This cannot be undone."
      )
    ) {
      return;
    }
    setResetting(true);
    setMessage("");
    setError("");
    try {
      const resolved = await resolveAllOpenIncidents();
      setMessage(`Reset ${resolved.length} open incident${resolved.length === 1 ? "" : "s"}.`);
    } catch (err) {
      setError(`Couldn't reset incidents: ${err.message}`);
    } finally {
      setResetting(false);
    }
  };

  return (
    <>
      <h3>Reset incidents</h3>
      <p className="settings-hint">
        Resolves every open incident (reason "reset") and restores all link and BGP state - for clearing out a demo
        or test run, not for day-to-day handling.
      </p>
      {error && <div className="login-error">{error}</div>}
      {message && <div className="form-success">{message}</div>}
      <button className="login-submit" onClick={reset} disabled={resetting}>
        {resetting ? "Resetting…" : "Reset all open incidents"}
      </button>
    </>
  );
}

const DELETE_FLEET_CONFIRMATION = "DELETE";

// Wipes the fleet so the map starts from scratch - see
// backend/app/api/simulation.py:reset_fleet.
function DeleteFleet({ onFleetReset }) {
  const [confirmation, setConfirmation] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const remove = async () => {
    setDeleting(true);
    setMessage("");
    setError("");
    try {
      const deleted = await resetFleet();
      setConfirmation("");
      setMessage(
        `Deleted ${deleted.routers} routers, ${deleted.bgp_links} BGP links and ${deleted.incidents} incidents. ` +
          "Add routers again from Add Fleet."
      );
      onFleetReset?.();
    } catch (err) {
      setError(`Couldn't delete the fleet: ${err.message}`);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <>
      <h3>Delete entire fleet</h3>
      <p className="settings-hint">
        Removes every router, BGP link and repeater, and everything recorded about them: incidents, traps,
        configuration backups and auto-heal history. Users, teams and router models are kept. The simulator stops
        re-seeding its demo fleet, so the map stays empty until you add routers from Add Fleet. This cannot be undone.
      </p>
      {error && <div className="login-error">{error}</div>}
      {message && <div className="form-success">{message}</div>}
      <label className="settings-hint" style={{ display: "block", marginBottom: "0.4rem" }}>
        Type <strong>{DELETE_FLEET_CONFIRMATION}</strong> to confirm
        <input
          value={confirmation}
          onChange={(e) => setConfirmation(e.target.value)}
          style={{ display: "block", width: "100%", marginTop: "0.3rem" }}
          aria-label="Type DELETE to confirm"
        />
      </label>
      <button
        className="login-submit"
        style={{ background: "#b91c1c" }}
        onClick={remove}
        disabled={deleting || confirmation !== DELETE_FLEET_CONFIRMATION}
      >
        {deleting ? "Deleting…" : "Delete entire fleet"}
      </button>
    </>
  );
}
