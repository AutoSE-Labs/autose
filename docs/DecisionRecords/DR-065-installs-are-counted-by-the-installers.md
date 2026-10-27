# DR-065: Installs Are Counted by the Installers, Not by AutoSE

- **Status:** Accepted
- **Date:** 2026-10-27
- **Decision owners:** AutoSE maintainers
- **Scope:** `install.sh` and `install.ps1` on the website (this repository's `gh-pages` branch, and its README); this repository's `install-counter` release; the maintainers' own tooling that reads the counts. No change to AutoSE itself.

## Context

We had no way to tell how many people install AutoSE. The installers clone this
repository instead of downloading a release file, so GitHub's release
download counts never move. The closest number, GitHub's clone count, is kept
for 14 days only and includes our own machines and CI. The website has no
analytics.

AutoSE's promise is local-first ("Local · no cloud" in the header): the
program sends nothing anywhere unless the person points it at a provider.
Counting must not break that.

## Decision

- **The installers count, AutoSE does not.** After a successful install or
  update, `install.sh` and `install.ps1` download one tiny file from this
  repository's `install-counter` release: `<install|update>-<linux|macos|windows|other>.txt`.
  GitHub counts each file's downloads. The file name is all that is sent.
  GitHub sees the request as it saw the clone just before it.
- **Only when it makes sense.** Nothing is counted for a fork (`AUTOSE_REPO`
  set to anything but AutoSE's repository), after a failed install, or with
  `AUTOSE_NO_COUNT=1`. The request has a 5-second limit, and its failure
  changes nothing. The installer says in one line when it counted, and how to
  skip it. The site README says the same.
- **The numbers stay with the maintainers.** A small script in the
  maintainers' private repository reads them, with GitHub's clone and visit
  numbers, and saves a daily history so those outlive GitHub's 14 days.
  Nothing shows them publicly.
- **Public later, if wanted.** The counter's totals can be read without a
  token from GitHub's releases API, so a line on autose.dev ("installed N
  times") would need no server.

## Consequences

- Install counts are per download, not per person. Someone who installs
  twice is counted twice. Updates are counted apart from installs.
- The counts are public in principle: anyone can read a release file's
  download count from GitHub. Nothing shows them.
- The `install-counter` release is a GitHub prerelease. It has no code, and is
  not offered as "Latest".
