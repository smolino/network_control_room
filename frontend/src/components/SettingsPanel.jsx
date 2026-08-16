import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { createTeam, deleteTeam, fetchSimulationStatus, setSimulationEnabled, updateTeam } from "../api.js";
import { buildOtpAuthUri, generateSecret, verifyTOTP } from "../totp.js";
import { createUser, deleteUser, getUser, getUsers, setOtpSecret, updatePassword } from "../users.js";

export default function SettingsPanel({ currentUser, teams, onTeamsChanged, onClose }) {
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
          <button className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}>
            Users
          </button>
          <button className={tab === "teams" ? "active" : ""} onClick={() => setTab("teams")}>
            Teams
          </button>
          <button className={tab === "simulation" ? "active" : ""} onClick={() => setTab("simulation")}>
            Simulation
          </button>
        </div>

        {tab === "security" && <SecurityTab currentUser={currentUser} />}
        {tab === "users" && <UsersTab currentUser={currentUser} />}
        {tab === "teams" && <TeamsTab teams={teams} onChanged={onTeamsChanged} />}
        {tab === "simulation" && <SimulationTab />}
      </div>
    </div>
  );
}

function SecurityTab({ currentUser }) {
  const [enabled, setEnabled] = useState(() => !!getUser(currentUser)?.otpSecret);
  const [setupSecret, setSetupSecret] = useState(null);
  const [qrDataUrl, setQrDataUrl] = useState(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [disableCode, setDisableCode] = useState("");

  useEffect(() => {
    if (!setupSecret) {
      setQrDataUrl(null);
      return;
    }
    const uri = buildOtpAuthUri(setupSecret, { accountName: currentUser });
    QRCode.toDataURL(uri, { margin: 1, width: 220 }).then(setQrDataUrl);
  }, [setupSecret, currentUser]);

  const startSetup = () => {
    setError("");
    setCode("");
    setSetupSecret(generateSecret());
  };

  const cancelSetup = () => {
    setSetupSecret(null);
    setCode("");
    setError("");
  };

  const confirmSetup = async (e) => {
    e.preventDefault();
    try {
      const ok = await verifyTOTP(setupSecret, code);
      if (!ok) {
        setError("Incorrect code — check your authenticator app and try again");
        return;
      }
      setOtpSecret(currentUser, setupSecret);
      setEnabled(true);
      setSetupSecret(null);
      setCode("");
      setError("");
    } catch (err) {
      setError(`Couldn't verify that code: ${err.message}`);
    }
  };

  const disableOtp = async (e) => {
    e.preventDefault();
    try {
      const secret = getUser(currentUser)?.otpSecret;
      const ok = await verifyTOTP(secret, disableCode);
      if (!ok) {
        setError("Incorrect code — enter your current 6-digit code to disable OTP");
        return;
      }
      setOtpSecret(currentUser, null);
      setEnabled(false);
      setDisableCode("");
      setError("");
    } catch (err) {
      setError(`Couldn't verify that code: ${err.message}`);
    }
  };

  return (
    <>
      <h3>One-time password (OTP)</h3>

      {!enabled && !setupSecret && (
        <>
          <p className="settings-hint">
            Two-factor authentication for <strong>{currentUser}</strong> is currently{" "}
            <strong>disabled</strong>. Enable it to require a 6-digit authenticator code at login, in
            addition to the password.
          </p>
          <button className="login-submit" onClick={startSetup}>
            Enable OTP
          </button>
        </>
      )}

      {!enabled && setupSecret && (
        <form onSubmit={confirmSetup} className="otp-setup">
          <p className="settings-hint">
            Scan this QR code with an authenticator app (Google Authenticator, Authy, 1Password,
            etc.), or enter the key manually.
          </p>
          {qrDataUrl && <img className="otp-qr" src={qrDataUrl} alt="OTP QR code" />}
          <code className="otp-secret">{setupSecret}</code>
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
            Two-factor authentication for <strong>{currentUser}</strong> is currently{" "}
            <strong>enabled</strong>. You'll be asked for a 6-digit code after your password at login.
          </p>
          <form onSubmit={disableOtp} className="otp-setup">
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
    </>
  );
}

function UsersTab({ currentUser }) {
  const [users, setUsers] = useState(() => getUsers());
  const [error, setError] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [resetTarget, setResetTarget] = useState(null);
  const [resetPassword, setResetPassword] = useState("");

  const refresh = () => setUsers(getUsers());

  const handleCreate = (e) => {
    e.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("Passwords don't match");
      return;
    }
    try {
      createUser(newUsername, newPassword);
      setNewUsername("");
      setNewPassword("");
      setConfirmPassword("");
      refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDelete = (username) => {
    if (!window.confirm(`Delete user "${username}"? This can't be undone.`)) return;
    setError("");
    try {
      deleteUser(username);
      refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleResetOtp = (username) => {
    if (
      !window.confirm(
        `Turn off two-factor authentication for "${username}"? Use this if they lost access to their authenticator app.`
      )
    )
      return;
    setOtpSecret(username, null);
    refresh();
  };

  const startResetPassword = (username) => {
    setResetTarget(username);
    setResetPassword("");
    setError("");
  };

  const handleResetPassword = (e) => {
    e.preventDefault();
    setError("");
    try {
      updatePassword(resetTarget, resetPassword);
      setResetTarget(null);
      setResetPassword("");
      refresh();
    } catch (err) {
      setError(err.message);
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
              <th>OTP</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.username}>
                <td>
                  {u.username}
                  {u.username === currentUser && <span className="you-tag"> (you)</span>}
                </td>
                <td>
                  {u.otpSecret ? (
                    <span className="badge up">enabled</span>
                  ) : (
                    <span className="badge unknown">disabled</span>
                  )}
                </td>
                <td className="users-table-actions">
                  <button type="button" onClick={() => startResetPassword(u.username)}>
                    Reset password
                  </button>
                  {u.otpSecret && u.username !== currentUser && (
                    <button type="button" onClick={() => handleResetOtp(u.username)}>
                      Reset OTP
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => handleDelete(u.username)}
                    disabled={u.username === currentUser || users.length <= 1}
                    title={u.username === currentUser ? "Can't delete the account you're signed in as" : undefined}
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
            New password for {resetTarget}
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
                      <span className="link" onClick={() => startEdit(team)}>Edit</span>{" "}
                      <span className="link" onClick={() => handleDelete(team)}>Remove</span>
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

function SimulationTab() {
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
          <button className="login-submit" onClick={toggle} disabled={saving}>
            {saving ? "Saving…" : enabled ? "Pause simulation" : "Resume simulation"}
          </button>
        </>
      )}
    </>
  );
}
