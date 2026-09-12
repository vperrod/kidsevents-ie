const status = document.getElementById("status");
const googleButton = document.getElementById("google");
const emailButton = document.getElementById("email");
const emailInput = document.getElementById("address");
const emailActions = document.getElementById("emailActions");
const signOutButton = document.getElementById("signout");
const savedEmailKey = "small-days-email-link-address";
const localSavesKey = "smalldays-saved";

function showStatus(message) {
  status.textContent = message;
  status.classList.add("show");
}

function setBusy(busy) {
  googleButton.disabled = busy;
  emailButton.disabled = busy;
}

function storedEventKeys() {
  try {
    const keys = JSON.parse(localStorage.getItem(localSavesKey) || "[]");
    return Array.isArray(keys)
      ? keys.filter((key) => typeof key === "string")
      : [];
  } catch {
    return [];
  }
}

async function serverRequest(user, method, eventKey) {
  const token = await user.getIdToken();
  const options = { method, headers: { Authorization: `Bearer ${token}` } };
  if (eventKey) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify({ event_key: eventKey });
  }
  const response = await fetch("api/member/saves", options);
  const body = await response.json();
  if (!response.ok)
    throw new Error(body.error || "We could not sync your saved plans.");
  return body;
}

async function syncLocalSaves(user) {
  const remote = await serverRequest(user, "GET");
  const remoteKeys = new Set(remote.event_keys || []);
  for (const eventKey of storedEventKeys()) {
    if (!remoteKeys.has(eventKey)) await serverRequest(user, "POST", eventKey);
  }
  const synced = await serverRequest(user, "GET");
  localStorage.setItem(localSavesKey, JSON.stringify(synced.event_keys || []));
  showStatus(`Signed in as ${synced.email}. Your saved plans are now synced.`);
}

async function initialise() {
  const response = await fetch("api/auth/config");
  const configuration = await response.json();
  if (!configuration.configured) {
    setBusy(true);
    showStatus(
      "Accounts are being connected. Public browsing and saved plans in this browser still work normally.",
    );
    return;
  }

  const [appSdk, authSdk] = await Promise.all([
    import("https://www.gstatic.com/firebasejs/11.0.2/firebase-app.js"),
    import("https://www.gstatic.com/firebasejs/11.0.2/firebase-auth.js"),
  ]);
  const { initializeApp } = appSdk;
  const {
    GoogleAuthProvider,
    getAuth,
    isSignInWithEmailLink,
    onAuthStateChanged,
    sendSignInLinkToEmail,
    signInWithEmailLink,
    signInWithPopup,
    signOut,
  } = authSdk;

  const auth = getAuth(initializeApp(configuration.firebase));
  let syncedUserId = null;
  onAuthStateChanged(auth, async (user) => {
    if (!user) {
      syncedUserId = null;
      googleButton.hidden = false;
      emailActions.hidden = false;
      signOutButton.hidden = true;
      return;
    }
    googleButton.hidden = true;
    emailActions.hidden = true;
    signOutButton.hidden = false;
    if (user.uid === syncedUserId) return;
    syncedUserId = user.uid;
    try {
      await syncLocalSaves(user);
    } catch (error) {
      showStatus(
        error.message ||
          "You are signed in, but your saved plans could not sync yet.",
      );
    }
  });
  if (isSignInWithEmailLink(auth, window.location.href)) {
    const rememberedEmail = localStorage.getItem(savedEmailKey);
    if (!rememberedEmail) {
      showStatus(
        "Open the sign-in link on the same device where you requested it, then try again.",
      );
      return;
    }
    setBusy(true);
    try {
      const result = await signInWithEmailLink(
        auth,
        rememberedEmail,
        window.location.href,
      );
      localStorage.removeItem(savedEmailKey);
      window.history.replaceState({}, document.title, window.location.pathname);
    } catch (error) {
      showStatus(
        error.message ||
          "That sign-in link could not be used. Please request another one.",
      );
    } finally {
      setBusy(false);
    }
  }

  googleButton.addEventListener("click", async () => {
    setBusy(true);
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
    } catch (error) {
      if (error.code !== "auth/popup-closed-by-user") {
        showStatus(error.message || "Google sign-in could not be opened.");
      }
    } finally {
      setBusy(false);
    }
  });
  signOutButton.addEventListener("click", async () => {
    await signOut(auth);
    showStatus("You are signed out. Your browser-saved plans remain here.");
  });
  emailButton.addEventListener("click", async () => {
    const email = emailInput.value.trim();
    if (!email) {
      showStatus("Enter an email address to receive a one-time sign-in link.");
      return;
    }
    setBusy(true);
    try {
      await sendSignInLinkToEmail(auth, email, {
        url: `${window.location.origin}${window.location.pathname}`,
        handleCodeInApp: true,
      });
      localStorage.setItem(savedEmailKey, email);
      showStatus(`A one-time sign-in link has been sent to ${email}.`);
    } catch (error) {
      showStatus(
        error.message ||
          "We could not send that sign-in link. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  });
}

initialise().catch(() =>
  showStatus("Accounts are temporarily unavailable. Please try again shortly."),
);
