/* Desktop notifications: tell the person when something waits for them
 * (DR-043).
 *
 * Through the browser's Notification API: no server involvement, nothing sent
 * anywhere but the operating system's own notification centre, and only after
 * the person turns it on and the browser grants permission. A notification is
 * raised only when it is news — the page is hidden, or the thing happened in
 * a session other than the one on screen — so working in the tab is not
 * interrupted by announcements of what is already in front of you.
 *
 * The on/off choice is a per-browser convenience, kept in localStorage and
 * wrapped so a page without storage still works.
 */

const NOTIFY_KEY = "autose.notifications";

function notifySupported() {
  return typeof window.Notification === "function";
}

function notifyWanted() {
  try {
    return localStorage.getItem(NOTIFY_KEY) === "on";
  } catch (error) {
    return false;
  }
}

function notifyActive() {
  return notifySupported() && notifyWanted() && Notification.permission === "granted";
}

/* Turn notifications on (asking the browser if it has not been asked) or off.
 * Returns the resulting state: "on", "off", "denied" or "unsupported". */
async function setNotifications(on) {
  if (!notifySupported()) return "unsupported";
  let result = "off";
  if (on) {
    let permission = Notification.permission;
    if (permission === "default") {
      try {
        permission = await Notification.requestPermission();
      } catch (error) {
        permission = "denied";
      }
    }
    result = permission === "granted" ? "on" : "denied";
  }
  try {
    localStorage.setItem(NOTIFY_KEY, result === "on" ? "on" : "off");
  } catch (error) {
    // Storage unavailable: the choice lasts for this page only.
  }
  return result;
}

/* Raise one notification. `tag` replaces an earlier one about the same thing
 * instead of stacking; `onOpen` runs when it is clicked. */
function raiseNotification({ title, body, tag, onOpen, urgent = false }) {
  if (!notifyActive()) return;
  try {
    const note = new Notification(title, {
      body,
      tag,
      icon: "/favicon.svg",
      requireInteraction: urgent,
    });
    note.onclick = () => {
      window.focus();
      if (onOpen) onOpen();
      note.close();
    };
  } catch (error) {
    // Some platforms refuse construction outside a service worker; the
    // in-page toast and the title count still say it.
  }
}

/* The tab title carries how many things wait, so a glance at the tab bar
 * answers "does it need me?" even with notifications off. */
function setTitleCount(count, base = "AutoSE") {
  document.title = count > 0 ? `(${count}) ${base}` : base;
}
