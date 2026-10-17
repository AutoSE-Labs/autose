# DR-023: Re-Validate the Address Check on Every Redirect Hop

- **Status:** Accepted
- **Date:** 2026-08-30
- **Decision owners:** AutoSE maintainers
- **Scope:** Network tool handlers (`fetch_url`, `web_search`); refines DR-012

## Context

DR-012 gates side-effecting and network tools behind explicit approval. The
network handlers add a second boundary approval cannot express: approving
"fetch this URL" is not approving a request to a loopback, link-local, or
private address. `_refuse_non_public_url()` resolves the hostname and refuses
if any resolved address is non-global. This matters more for AutoSE than for a
typical agent, because a local-first product is by definition running next to
an inference endpoint on localhost, and on a cloud host the metadata service is
one link-local hop away.

The check ran only against the URL passed in. `urllib`'s default opener follows
redirects, and nothing re-ran the check on the new location. So the guard held
for one hop and then stopped applying, while the module docstring claimed it
closed "the local SSRF path even on an approved call."

Demonstrated against the implementation: a server answering `302` with a
`Location` of `http://127.0.0.1:<port>/latest/meta-data/` returned the internal
body in full. A public, approvable URL was sufficient to reach an address the
guard existed to refuse — the approval prompt showed the user the safe URL, and
the request went somewhere else.

## Decision

The address check runs on **every hop**, not only the URL the user approved.

`_GuardedRedirectHandler` subclasses `urllib.request.HTTPRedirectHandler` and
re-runs `_refuse_non_public_url()` against each `Location` before following it,
raising `BlockedRedirectError` when the destination fails. Both `fetch_url` and
`web_search` go through an opener built with it, and both translate that error
into a distinct message naming the refusal — so the model is told the redirect
was blocked rather than being handed a generic HTTP failure it might retry.

Subclassing rather than disabling redirects and following them by hand is
deliberate: urllib's own redirect-loop detection and depth limit keep applying,
and a hand-rolled loop would have to re-implement both correctly to gain
nothing.

## Consequences

### Positive

- A public URL can no longer be used to reach a private address by redirection,
  which is the realistic shape of this attack — an attacker controls a page,
  not AutoSE's approval prompt.
- The module's documented guarantee and its behaviour now agree.
- Both network tools are covered by one mechanism, so a future third network
  tool inherits it by using the same opener.

### Negative

- A legitimate site that redirects through a private address — some corporate
  proxies and captive portals do — now fails where it previously worked. Given
  the product is local-first and the alternative is an open SSRF path, refusing
  is the right default, but it is a real behaviour change.
- The guard resolves each hostname again per hop, so a redirect chain costs
  extra DNS lookups.

### Mitigations

- The refusal message names the address that failed, so a user hitting a
  legitimate internal redirect can see exactly why rather than guessing.
- Tests exercise a real loopback redirect rather than a mocked opener, because
  the defect lived in urllib's redirect handling and a fake standing in for
  urllib would have passed against the broken code.

## Alternatives considered

### Disable redirects entirely

Rejected. A large share of ordinary web pages redirect at least once — HTTP to
HTTPS, bare domain to `www`, shortened links — so refusing all of them makes
`fetch_url` unreliable for its actual purpose while adding no safety over
validating each hop.

### Validate only the final URL after following the chain

Rejected. The request to the internal address has already been made by then,
and for something like a metadata endpoint the request *is* the compromise; the
response body arriving back at AutoSE is incidental.

### Pin the first hop's resolved address and require later hops to match

Rejected as both too strict and insufficient. Legitimate redirects routinely
cross hosts, and an attacker-controlled chain can stay on one host while still
resolving somewhere private. Checking the property that matters — is this
address global — is simpler and stronger than checking that it has not changed.

### Treat this as a bug fix and skip the DR

Rejected under DR-001's bar. The fix embeds a policy choice a contributor could
reasonably question — validate each hop rather than refuse redirects outright —
and the rejected options above are exactly the debate that would otherwise be
re-run.
