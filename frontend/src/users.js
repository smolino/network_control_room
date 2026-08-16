// Client-side user store — no backend auth exists in this app, so accounts
// (and their passwords, in plaintext) live in localStorage alongside the
// OTP secrets from totp.js. This gates the UI, not any API.

const USERS_KEY = "ncr_users";

function loadUsers() {
  const raw = localStorage.getItem(USERS_KEY);
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    } catch {
      // fall through to reseed a corrupted store
    }
  }
  const seeded = [{ username: "admin", password: "adminadmin", otpSecret: null }];
  localStorage.setItem(USERS_KEY, JSON.stringify(seeded));
  return seeded;
}

function saveUsers(users) {
  localStorage.setItem(USERS_KEY, JSON.stringify(users));
}

function findIndex(users, username) {
  return users.findIndex((u) => u.username.toLowerCase() === username.trim().toLowerCase());
}

export function getUsers() {
  return loadUsers();
}

export function getUser(username) {
  const users = loadUsers();
  return users[findIndex(users, username)] ?? null;
}

export function verifyCredentials(username, password) {
  const user = getUser(username);
  return user && user.password === password ? user : null;
}

export function createUser(username, password) {
  const clean = username.trim();
  if (!clean) throw new Error("Username can't be empty");
  if (password.length < 6) throw new Error("Password must be at least 6 characters");
  const users = loadUsers();
  if (findIndex(users, clean) !== -1) throw new Error("A user with that username already exists");
  users.push({ username: clean, password, otpSecret: null });
  saveUsers(users);
  return users;
}

export function deleteUser(username) {
  const users = loadUsers();
  if (users.length <= 1) throw new Error("Can't delete the last remaining user");
  const idx = findIndex(users, username);
  if (idx === -1) throw new Error("User not found");
  users.splice(idx, 1);
  saveUsers(users);
  return users;
}

export function updatePassword(username, newPassword) {
  if (newPassword.length < 6) throw new Error("Password must be at least 6 characters");
  const users = loadUsers();
  const idx = findIndex(users, username);
  if (idx === -1) throw new Error("User not found");
  users[idx] = { ...users[idx], password: newPassword };
  saveUsers(users);
  return users;
}

export function setOtpSecret(username, secret) {
  const users = loadUsers();
  const idx = findIndex(users, username);
  if (idx === -1) throw new Error("User not found");
  users[idx] = { ...users[idx], otpSecret: secret };
  saveUsers(users);
  return users;
}
