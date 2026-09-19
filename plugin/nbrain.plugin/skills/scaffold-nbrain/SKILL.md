---
name: "scaffold-nbrain"
description: "Set up a personal nbrain — a work-tracking system for a new user — discovers their role, projects, people and noise from connected sources, interviews only for genuine gaps, then writes their memory.md, radar.md and CLAUDE.md rules and creates their scheduled sweeps. Use when someone says \"set up my nbrain\", \"set up my second brain\", \"onboard me to nbrain\", \"scaffold nbrain for me\", or asks to replicate this work-tracking system for another person."
---

# Scaffold an nbrain

Sets up the personal work-tracking system for one user: discover hard, ask only what can't be inferred, then write the files and create the schedules.

## RULE ZERO — NEVER PUT ONE USER'S DATA IN ANOTHER USER'S ARTEFACTS

This skill and everything it ships must contain **zero personal information**. Not the person who first built the system, not their colleagues, not their file paths.

- **Templates ship. Personalised copies never do.** Distributable files use `{{PLACEHOLDER}}` tokens. The scaffolder fills them in per user at setup time, locally.
- **Colleague names are the real hazard.** A tier-1 people list is not just the user's own data — it names other employees who never consented to appear in a shared artefact.
- **Before packaging anything for distribution, run the privacy gate** (`references/check-for-pii.sh`). It checks structural identifiers (email addresses, home paths, tenant URLs, ticket keys, chat and document links) plus names passed at runtime. Non-zero exit means do not package.
- A clean scan is **necessary, not sufficient.** Read the diff as well — a bare first name in prose passes every structural check.

If asked to share or package these skills, do the scrub first and report the result. Never assume a file is clean because it was written from a template.

## The principle

**Infer aggressively, ask sparingly.** In the reference build, discovery supplied role, manager, standing meetings, 1:1 partners, noise list, ticket projects and repositories. The user answered five questions.

**Show your inferences.** Every discovered fact goes in the file with its evidence, so a wrong inference is visible rather than silently load-bearing.

## Step 1 — Working folder

Ask where the system should live, or use `request_cowork_directory`. It must be writable and persist between sessions.

**Warn if the path looks like a git checkout** — committing a work journal or a credential to a shared repo is a bad day. Suggest a path outside any repo.

Confirm the folder is reachable before continuing. A system whose state files vanish is worse than none.

## Step 2 — Discover

**If a profiling agent is available as a tool** (e.g. a read-only Glean agent), call it with the user's name and email. It returns identity, name variants, projects, key people, standing meetings, noise, open commitments, coverage gaps, and a list of unanswered questions. That last list becomes your interview.

**Otherwise discover directly.** Probe connectors and record what's actually enabled — never assume:

- **Org directory** — title, department, location, manager, reports.
- **Calendar** — next 7 days. Recurring one-to-one events reveal tier-1 relationships; meetings the user organises reveal ownership. Note cadence and whether invites carry agendas.
- **Ticketing** — items assigned to and reported by the user, oldest-updated first. **Check whether due dates exist at all** — if they don't, "past due" is uncomputable and staleness is the only proxy. Record that finding.
- **Email** — 30 days. Separate humans from automation. Automation splits into *mine for signal* (build notifications, monitoring, ticket mail) and *suppress* (newsletters, marketing, share notices, HR admin). Flag anything phishing-shaped.
- **Chat** — search for the user's own recent messages. Richest commitment source; treat as essential.
- **Meeting notes** — recent AI-notes documents; read the two or three most relevant. Their "Next steps" sections name owners explicitly and often hold commitments recorded nowhere else. Note any the user never opened.
- **Code hosting** — check for a connector. If none, check whether notification emails arrive instead, and whether the sandbox can reach the host at all. Record the answer.

## Step 2.5 — Pick the role track

**Do this before interviewing.** Read `references/role-packs.md` and select one of three
tracks: `ic`, `lead`, or `manager`. The core is shared; only the question pack and the
signal definitions change.

Pick by evidence, not job title. Someone with no direct reports who organises three
delivery syncs is a `lead` whatever their title says. Someone with reports whose ticket
queue is nearly empty is a `manager`, and running the `ic` track on them produces a system
that watches almost nothing and looks broken.

Propose the track with your reasoning and let the user correct it. Getting this wrong is
not a small miss — it determines what the whole system treats as important.

If the track is `manager`, read the constraints in `role-packs.md` on reading a report's
work before gathering anything. They limit how team data may be framed, and they are not
optional.

## Step 3 — Interview only the gaps

Use `AskUserQuestion`, multiple choice, pre-filled from discovery. **Five questions or fewer.** Never ask what you found.

Add the selected track's extra questions from `references/role-packs.md` to the ones below.

Always ask these, because they can't be inferred reliably:

1. **Priorities and ranking.** Show the projects found; ask which matter most and whether any carry equal weight. Equal weight matters — the brief must then flag when one starves the other.
2. **Reply urgency tiers.** Show people grouped by relationship; ask whose message should never wait. Ask explicitly who's missing.
3. **Name variants and collisions.** Show mis-transcriptions found. **Ask directly whether any colleague has a confusable name.** Highest-consequence question in the interview — a wrong alias attributes someone else's commitments to this user.
4. **Sweep timing** — weekday time for the daily sweep, and when the weekly review runs.
5. **Delivery** — email draft to self, a file in the folder, or both. If email, confirm they understand a draft is technically a write and they're permitting that narrow exception.

## Step 4 — Write the files

From the bundled templates in `references/`, replacing every placeholder:

**`CLAUDE.md`** — from `references/CLAUDE.template.md`. Loads automatically in every future session in that folder, which is what makes the rules durable rather than a promise. Fill in name and email, folder path, name variants and collisions, unverifiable sources, per-system link patterns, write-capable connectors, phishing seen.

**`memory.md`** — identity, priorities, tiered key people with urgency, active projects with systems and what slipping looks like for each, standing meetings, two-bucket noise list, sweep config and thresholds, source status, pointer to `CLAUDE.md` for rules.

**`radar.md`** — the ledger, structured and empty: Waiting on me, Replies you are waiting on from others, Slipping, Commitments I made, Meetings needing attention, Watch list, Dismissed, Resolved this week, Sweep log, Known blind spots. Include the Verified-column legend. Populate on first sweep, not now.

**Personalise the companion skills** from `references/skill-templates/` via `save_skill` — what's slipping, prep me for a meeting, draft my replies, search internal knowledge. These personalised versions are local to this user and **must never be packaged or shared.**

Never dump raw email or calendar content into any of these files. Summarise.

## Step 5 — Dry-run before scheduling

**Run the sweep manually once and show the output. Do not schedule first.** The dry run is where thresholds get tuned and false assumptions surface. In the reference build it revealed that the ticketing system had no due dates at all, that the chat index was the only place commitments lived, and that meeting notes had been wrongly classified as noise.

Ask what's wrong with the brief. Fix it. Then schedule.

## Step 6 — Create the scheduled tasks

Two tasks, cron in local time. Each prompt fully self-contained — a scheduled run has no memory of this conversation. Each begins by reading the rules, memory, ledger and format spec, and **stops and reports** if the folder is unreachable rather than running partially.

Tell the user plainly:
- Tasks run while the app is open; if closed when due, they run on next launch.
- **If the machine sleeps mid-run the task dies with no retry.** Quitting the app at end of day is more reliable than leaving it open — closed means deferred rather than dead.
- Keep the sweep short; cap searches per source. A fast complete sweep beats a thorough dead one.
- Click "Run now" once to pre-approve tools, or scheduled runs may stall on permission prompts.

## Design rules worth carrying over

Learned by getting them wrong first:

- **The brief is an alert; the ledger is the record.** Cap the brief hard, around 550 words, and truncate with "+N more in the ledger". A brief that duplicates the ledger becomes a six-minute read and gets skimmed.
- **Verify before carrying forward.** Never repeat an item because it appeared yesterday.
- **Label what you cannot verify** and phrase it as "if still open". Auto-drop after three unconfirmed runs.
- **Retract wrong flags explicitly.** Self-correction is what earns trust over months.
- **Meeting notes over chat over email**, in that order, for finding commitments.
- **Name every blind spot** in every brief. Coverage you don't have must never be implied.

## Bundled reference files

Read these from `references/` as needed — do not rely on memory for their contents:

| File | Use |
|---|---|
| `CLAUDE.template.md` | The ten operating rules. Fill every `{{PLACEHOLDER}}` and write it as `CLAUDE.md` in the user's folder. This is what makes the rules durable across sessions rather than a promise. |
| `role-packs.md` | The three role tracks — `ic`, `lead`, `manager` — each with its question pack, signal definitions, and brief section order. Includes the constraints on how a manager's team data may be used. Read before interviewing. |
| `brief-format.md` | Brief format spec: hard word caps, section order, email HTML constraints, palette, badges, and how to compute the four extras. The caps are limits, not suggestions. |
| `profiler-agent.md` | Config for an optional read-only profiling agent, plus the full question checklist mapping every field that must be confirmed. Read before interviewing — it lists what to ask even where evidence was found. |
| `skill-templates/*.template.md` | The four companion skills. Personalise via `save_skill` per user. Never ship a filled-in copy. |
| `check-for-pii.sh` | Privacy gate. Run before packaging or sharing anything. Non-zero exit means do not ship. |
