import { initializeApp } from "https://www.gstatic.com/firebasejs/11.0.2/firebase-app.js";
import {
  getAuth,
  onAuthStateChanged,
} from "https://www.gstatic.com/firebasejs/11.0.2/firebase-auth.js";

const localSavesKey = "smalldays-saved";

async function request(user, method, eventKey) {
  const token = await user.getIdToken();
  const options = { method, headers: { Authorization: `Bearer ${token}` } };
  if (eventKey) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify({ event_key: eventKey });
  }
  const response = await fetch("api/member/saves", options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Save sync failed");
  return body;
}

function localKeys() {
  try {
    const keys = JSON.parse(localStorage.getItem(localSavesKey) || "[]");
    return Array.isArray(keys)
      ? keys.filter((key) => typeof key === "string")
      : [];
  } catch {
    return [];
  }
}

async function connect() {
  const response = await fetch("api/auth/config");
  const configuration = await response.json();
  if (!configuration.configured) return;
  const auth = getAuth(initializeApp(configuration.firebase));

  onAuthStateChanged(auth, async (user) => {
    if (!user) return;
    try {
      const current = await request(user, "GET");
      const known = new Set(current.event_keys || []);
      for (const eventKey of localKeys()) {
        if (!known.has(eventKey)) await request(user, "POST", eventKey);
      }
      const synced = await request(user, "GET");
      localStorage.setItem(
        localSavesKey,
        JSON.stringify(synced.event_keys || []),
      );
      window.dispatchEvent(
        new CustomEvent("small-days:remote-saves", {
          detail: synced.event_keys || [],
        }),
      );
    } catch {
      // Local saves continue to work if the network or authentication service is down.
    }
  });

  window.addEventListener("small-days:save-change", async ({ detail }) => {
    const user = auth.currentUser;
    if (!user || !detail?.eventKey) return;
    try {
      await request(user, detail.saved ? "POST" : "DELETE", detail.eventKey);
    } catch {
      // The browser copy remains available; the next successful sign-in retries it.
    }
  });
}

connect().catch(() => {});
