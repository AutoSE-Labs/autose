# DR-053: YOLO Mode — a Switch That Lets Everything Run Without Asking

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `Bridge.set_yolo`, `Bridge._ask`, `Bridge._review_plan` and the `yolo` field of the state payload in `src/gui/bridge.py`; `POST /api/yolo`; the YOLO switch, banner and frame in `src/gui/static/`. DR-012's gate and DR-047's unattended rule are unchanged.

## Context

DR-012 puts every risky call to the person, and `--yes` (auto-approve) skips
that only when the operator says so on the command line, before the dashboard
starts. People running long, watched sessions asked for the same thing as a
switch on the page: let the run be autonomous for a while, then turn the
questions back on, without a restart.

## Decision

- A **YOLO** switch in the dashboard header. On, every approval and every plan
  review in every open session is answered yes at once, and what is already
  waiting is answered when it turns on. Off, everything asks again; standing
  allowances (DR-048, DR-052) are untouched either way.
- Turning it **on** asks for confirmation in plain words. Turning it **off** is
  one click, from the header or from the banner.
- While it is on the page cannot be mistaken for normal: a red banner with
  hazard tape across the top ("YOLO mode is on … Turn off"), a red frame round
  the whole window, a pulsing red switch, and "Nothing asks" where the page
  normally says where it runs.
- It is **never saved**: every dashboard start begins with it off.
- Scheduled and watched runs (DR-047) are not covered: nobody chose this for
  them, so their calls are still refused.
- Every call still goes through the executor and is recorded as an event,
  with the answer "YOLO mode: the user lets every call run without asking".

## Consequences

### Positive

- Long autonomous runs no longer stall on questions the person would answer yes.

### Negative

- With it on, a model can delete files, push, or reach the network unasked.
  The banner and frame are the only guard; that is the point of the switch.
