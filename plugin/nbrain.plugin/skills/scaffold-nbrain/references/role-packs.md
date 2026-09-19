# Role packs

Three tracks. The core is shared — the ten rules, the ledger structure, verification,
the brief format, privacy handling, meeting-notes mining, name collisions. **Only the
question pack and the signal definitions change.**

Do not fork the scaffolder per role. Select a track, then apply that track's packs.

| Track | Who | What "slipping" means |
|---|---|---|
| `ic` | Developer, architect, analyst — owns delivery | *My* work is late or stalled |
| `lead` | Tech lead, staff engineer, EM-of-one — owns delivery **and** unblocks others | My work is late, **or** I'm the blocker for someone else |
| `manager` | People manager — owns outcomes through others | Someone is blocked and I don't know, or a decision is waiting on me |

Pick by evidence, not by title. Someone with no direct reports who organises three
delivery syncs is a `lead`, whatever their job title says. Confirm the inference with the
user rather than asserting it.

---

## Shared across all three

Every track asks the identity, names, noise, meetings and delivery questions in
`profiler-agent.md`. Every track gets the same rules file, ledger structure and brief
format. What follows is **additional**, not a replacement.

---

## Track: `ic`

The current reference implementation. Signals:

| Signal | Source | Verifiable |
|---|---|---|
| My tickets stale or past due | Ticketing | High |
| Reviews awaiting me | Code host | Often low — email-only |
| My changes open too long, or failing checks | Code host | Often low |
| Threads awaiting my reply beyond the SLA | Email, chat | High |
| Commitments I made, not delivered | Chat, meeting notes | High |
| Meetings with no agenda or no prep | Calendar | High |

Extra questions: preferred review turnaround, which repositories matter, whether build
failures on their own branches should be flagged or ignored as normal churn.

---

## Track: `lead`

IC signals **plus** the ones below. The defining addition: **a lead is often the
bottleneck, and won't notice.**

| Signal | Source | Verifiable |
|---|---|---|
| I am the blocker — someone is waiting on my review, decision, or answer | Chat, code host, ticket comments | Medium |
| A decision I own has been open too long | Chat, meeting notes | Medium |
| Work I delegated has gone quiet | Ticketing, team channels | Medium |
| Design or architecture doc awaiting my sign-off | Docs, chat | Medium |
| Cross-team dependency with no owner | Chat, tickets | Low |

Extra questions:
- Which decisions are yours to make rather than escalate?
- How long may a review sit before it's a problem?
- Which cross-team dependencies do you own the chase for?
- Should the brief separate "my delivery" from "others waiting on me"? (Recommend yes —
  they compete for the same hours, and leads systematically underweight the second.)

**Brief change:** add a "you are the bottleneck" block directly under Due Today. It ranks
above the lead's own delivery, because a blocked colleague costs more than a slipped
ticket of one's own.

---

## Track: `manager`

Ticket-based signals largely stop working here. A manager may have almost nothing assigned
and still be the busiest person on the team.

| Signal | Source | Verifiable |
|---|---|---|
| A decision or approval is waiting on me | Chat, email, meeting notes | High |
| A report is blocked | Team channels, tickets, standup notes | Medium |
| A report has gone quiet — no visible activity where there normally is | Team channels, tickets | **Low — inference from absence** |
| An escalation is aging without resolution | Email, chat, incident tickets | Medium |
| 1:1 cadence has drifted — haven't met someone in N weeks | Calendar | High |
| Commitments I made to my team | Chat, 1:1 notes | High |
| Commitments I made upward to my own manager | Chat, email, meeting notes | High |
| Hiring loops stalled at a stage | ATS, calendar, email | Medium |
| Team absence or capacity gap in the week ahead | Calendar | High |

Extra questions:
- Who are your direct reports, and who is a skip-level?
- Which decisions genuinely need you, versus ones your team should make without you?
- How often should you meet each report? Flag when the gap exceeds it.
- What counts as an escalation, and how long may one age?
- Who do you escalate to, and who escalates to you?
- Are you hiring? For which roles?
- Which forums do you owe a regular status update to?

**Brief change:** the section order inverts. Decisions waiting on you comes first, then
team blockers, then commitments, then your own items last. A manager's own tickets go near
the bottom or are omitted.

### Hiring — stage counts only

Report **process slippage, never candidate detail.**

Allowed: "Two loops stalled at onsite for over a week. One offer awaiting your approval."

Never in the brief or the ledger: candidate names, interview feedback, scores,
compensation, or demographic information. Those live in the ATS, which has access controls
this system does not.

If asked to include candidate detail, decline and explain why: the ledger is a markdown
file on a laptop, and it is the wrong container for that data.

### Reading a report's work — constraints on USE, not on access

Scope decided by the user: the sweep may read anything their permissions allow, including
team channels and accessible threads. That is broad, so how the output is framed matters
more than usual.

**Report blockers and risks. Never per-person output.**

| Write this | Never this |
|---|---|
| "The monitoring integration is blocked on an access request from last Monday." | "A report closed no tickets this week." |
| "Nobody has picked up the failing build on `main`." | "A report sent 4 messages yesterday, down from 20." |
| "No visible progress on the migration since Tuesday. Worth asking at standup." | "A report has been inactive for 3 days." |

**Forbidden outright — do not compute, store, or report:**
- Message, commit, or ticket counts per person, or any trend in them
- Response-time or working-hours patterns for anyone other than the user
- Any framing that reads as a productivity or performance metric
- Anything from a report's private one-to-one conversations with third parties

The test: **would you be comfortable if the report read this line?** If not, reframe it
around the work or drop it. A manager brief exists to surface where help is needed, not to
score people.

**Disclosure.** During setup, tell the manager plainly that this reads their team's visible
work, and recommend they tell the team it exists. Not enforceable, and still the right
default — a system a team discovers by accident is a trust problem regardless of how
carefully it was built.

**Honesty about weak signals.** "A report has gone quiet" is inference from absence, not a
verified state, and absence has innocent explanations — leave, focused work, working
somewhere the sweep cannot see. Always phrase as a prompt to ask, never as a finding:
"worth checking in with X" rather than "X is behind."
