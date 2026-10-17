# DR-043: The Dashboard Raises Desktop Notifications for What Waits on the Person

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/gui/static/notify.js`; the bell, the hooks and the tab-title count in `src/gui/static/{index.html,app.css,app.js}`; the static allowlist in `src/gui/server.py`. No `logic/` or bridge change. Builds on DR-012 (approvals block), DR-029 (local page), DR-031 and DR-042 (work continues in sessions not on screen).

## Context

DR-031 and DR-042 mean work goes on where the person is not looking: another
session, a queued request, a run on a model that takes minutes. Some of it
cannot continue without them. An approval blocks its turn (DR-012) and is denied
after fifteen minutes unanswered. A held queue waits for Resume. Until now the
only signal was inside the page — a toast, an overlay, a dot in the rail —
which is invisible when the page is behind another window, the usual case for
a run that takes minutes.

## Decision

- **Desktop notifications through the browser's Notification API**, turned on
  with a bell in the header. The browser asks for permission the first time;
  nothing is raised before the person turns it on and the browser grants it.
  The choice is kept per browser.
- **Raised for:** an approval waiting (marked to stay until dismissed); a task
  finished, or ended without finishing; a task stopped by an error; a queue
  held. Clicking one brings the window forward and focuses that session.
- **Raised only when it is news:** the page is hidden or the event is in a
  session other than the one on screen. An approval is also raised when the
  page is visible but the window does not have focus, because an unanswered
  approval costs the run.
- **The tab title counts what waits** — pending approvals plus held queues —
  as "(2) AutoSE", with notifications on or off.
- Notifications are raised by the page itself. The server gains nothing: no
  endpoint, no push service, nothing leaves the machine (DR-029).

## Consequences

### Positive

- A person working in another window learns at once that a run needs them,
  instead of returning to a denied approval.
- Off by default and per browser: nobody is interrupted who did not ask to be.

### Negative

- Only an open dashboard tab can raise them; with the page closed, nothing
  announces anything.
- Browsers differ: some suppress notifications for a page in the foreground,
  some need the site's permission reset after a denial.

### Mitigations

- The tab-title count works with notifications off and in any browser.
- A denied permission is explained in the bell's tooltip and a toast, with
  where to change it.

## Alternatives considered

### Notify from the server (e.g. `notify-send`)

Rejected for now. It works without a page but only on the machine running the
dashboard, differs per operating system, and puts a desktop dependency into a
server that is otherwise portable standard library. A future long-running
gateway is the natural place for it.

### A push service

Rejected. It would send what AutoSE is doing to a third party, against DR-029's
local-first rule.
