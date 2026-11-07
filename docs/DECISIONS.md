# Decisions

Dated records of the choices that are settled, so they are not re-litigated
and so the code can be read against them. These are the operator's calls on
the Codexify integration, not proposals.

They are recorded here rather than in Codexify's ADR directory for a
specific reason: **ADR-108 is `Proposed` and its ratification is
Resonant's.** A decision that has not been ratified into an ADR does not
belong in the ADR directory, and writing one there would be a way of
routinely ratifying decisions by the back door. When ADR-108 is ratified,
these three are folded into it or superseded by it; until then this file is
the record.

---

## D1 — The REAPER handoff is the asset row

**Decided 2026-11-07.**

The handoff to REAPER is a file and a row. The Producer writes the `.rpp`;
the host registers it as a `MediaAsset` against the project; the Artist
opens the project in REAPER.

**Not built, and deliberately so:**

- a Codexify button that shells out to REAPER;
- a REAPER-side Lua action that polls Codexify for state.

**Why.** The thesis of this project is that the Producer is the thing worth
building and REAPER is the thing worth handing off to. A control channel in
either direction makes the two applications interdependent: each would then
have to be alive for the other to do anything, and a REAPER that is not
running would become a failure mode of the Producer rather than of the
Artist's own workstation. The file is the whole contract. It has no
liveness requirement, it diffs, it is version-controlled, and it is the
thing the Artist actually wants.

**State: already the behaviour.** The `.rpp` is registered as a
`MediaAsset` with `media_kind` `other`, against the project that asked for
it. Nothing needs to change to satisfy this decision; what changes is that
the alternatives are now closed rather than merely unmentioned.

---

## D2 — The popup is a browser overlay in the Codexify frontend, launched from REAPER

**Decided 2026-11-07.**

There is no dedicated Agent-DAW user interface. The interface is the
existing Guardian Chat, rendered in the Codexify frontend as a popup modal
that floats over the DAW. REAPER launches it.

Every message lands in a thread in a project in the main harness. **The
popup is a projection over established structure, not a second chat
surface.** There is one chat store, one set of threads, and one place the
conversation is durable. Opening the popup from REAPER and opening it from
the Codexify app are the same thread.

**Why the popup lives in the Codexify frontend rather than beside the DAW.**
A chat that lives beside the DAW is a chat with its own storage, its own
identity, its own notion of what is durable, and its own idea of what the
Artist said. That is four places for one conversation to live, and the
Artist's words are the most valuable thing in the system. A projection over
the existing thread has none of those problems: the message is written once,
to the thread that already exists, and the popup is a window onto it.

**What it needs, and what is not decided yet:**

| Piece | Owner | State |
| --- | --- | --- |
| A thread-addressable overlay route in the Codexify frontend | Codexify | **Does not exist.** The Codexify chat is selected in application state, not by URL. |
| The launcher REAPER runs to open it | Agent-DAW / Studio v1 | **Not built.** It belongs with the ReaPack bundle in the Studio v1 harness, which is not built. |
| The URL the launcher opens | Codexify | The host names it. The Producer does not invent a route in someone else's application. |

**Why nothing is shipped for this yet.** Both remaining pieces are
decisions that belong to a side that has not made them: the route name is
Codexify's to choose, and the launcher mechanism is Studio v1's. Writing
either now would be guessing at a contract that the other side has not
agreed to, and the result would be code that encodes somebody else's naming
choice.

**Honest scope for now:** the decision is recorded; the implementation
waits on a Codexify-side route and the Studio v1 harness. It is not
"almost done" — it is a decision with its prerequisites named.

---

## D3 — ADR-108 stays `Proposed`

**Decided 2026-11-07.**

ADR-108 is not ratified, and this repository does not act as if it were:

- its six open decisions are **not** implemented as follow-up pull requests;
- it is **not** reviewed point by point here.

Ratification is Resonant's call, and the open decisions are the ones that
need it — music sessions in the ordinary app, divergence after the REAPER
handoff, licensing and entitlement, live REAPER control for v1,
multi-agent delegated producer roles, and generative audio adapters.

**What this does not stop.** Fixing a defect in shipped code is not
implementing an ADR decision. The host-owned job attribution
(Codexify PR #1027) is a defect fix: before it, the host's attribution
depended on the service's age, and against an engine that discards the
context the host produced files and registered none of them while
reporting success. ADR-108 §7 is about artifact custody, and the fix
*strengthens* the position §7 describes — the Project is the durable home,
and now the host, rather than the service, is what knows it. ADR-108
carries a dated note saying so; its substance and its `Proposed` status are
unchanged.

**What this does stop.** A pull request that implements one of the six
open decisions, or a review that argues ADR-108's substance line by line,
would be doing the ratification by the back door. Neither is done here.

---

## Related

- [`CODEXIFY-INTEGRATION.md`](./CODEXIFY-INTEGRATION.md) — how the
  integration works, and the wiring checklist.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the Producer's own architecture.
- Codexify `docs/architecture/adr/108-codexify-music-producer-authority-and-agentdaw-service-boundary.md`
  — the proposed ADR these decisions sit beside.
