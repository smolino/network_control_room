// Client side of the backend's auth (backend/app/auth.py): the bearer token
// + user returned by POST /api/auth/login live in sessionStorage for this
// tab, and every API call/WebSocket sends the token. Roles are enforced by
// the backend - the helpers below only decide what the UI bothers to show.
import { createContext, useContext } from "react";

const SESSION_KEY = "ncr_session";
export const UNAUTHORIZED_EVENT = "ncr:unauthorized";

const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };
export const ROLES = Object.keys(ROLE_RANK);

// Before accounts moved to the backend, users (with plaintext passwords) and
// their OTP secrets lived in localStorage under this key - don't leave them
// lying around in browsers that used the old version.
try {
  localStorage.removeItem("ncr_users");
} catch {
  // storage unavailable
}

export function loadSession() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(SESSION_KEY));
    return parsed?.token && parsed?.user ? parsed : null;
  } catch {
    return null;
  }
}

export function saveSession(session) {
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function clearSession() {
  sessionStorage.removeItem(SESSION_KEY);
}

export function getToken() {
  return loadSession()?.token ?? null;
}

export function hasRole(user, role) {
  return !!user && (ROLE_RANK[user.role] ?? -1) >= ROLE_RANK[role];
}

// The signed-in user, provided by App.
export const SessionContext = createContext(null);

export function useCurrentUser() {
  return useContext(SessionContext);
}

export function useHasRole(role) {
  return hasRole(useContext(SessionContext), role);
}
