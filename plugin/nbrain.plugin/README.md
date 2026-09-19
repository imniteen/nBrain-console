# nbrain

A standing operational layer for your work. It watches your whole work surface, keeps a
running picture of what is on your plate, and tells you what is slipping — so the checking
and synthesising is delegated, while the deciding and sending stay with you.

## What you get

Running the setup once produces:

- **A daily brief**, weekday mornings. Under 550 words. What is due today, what is waiting
  on you, what is slipping and why, your meetings, and an honest list of what could not be
  checked.
- **A weekly review**, Friday afternoons. What slipped and why, your commitment delivery
  rate, and one specific process improvement.
- **A running ledger** that carries the detail the brief points at.
- **On-demand skills** personalised to you: what is slipping right now, prep me for a
  meeting, draft my replies, search internal knowledge.

## Getting started

Say **"set up my nbrain"**. The setup will:

1. Ask where the system should live — a folder that persists on your machine.
2. Discover what it can from your connected tools: your role, manager, projects, standing
   meetings, who you actually work with, and which senders are noise.
3. Ask you about five things it cannot infer — chiefly your priorities and whether any
   colleague has a name that could be confused with yours.
4. Run one sweep by hand and show you the result, so thresholds get tuned before anything
   is automated.
5. Create the scheduled sweeps, once you are happy with the output.

Budget about fifteen minutes, most of it reviewing rather than typing.

## Read-only, by design

The system observes and reports. It does not act.

No tickets are created or edited. No chat messages are ever sent — not even a reply, not
even when something looks urgent. No calendar events are created, moved, or RSVP'd to, and
no prep blocks are booked even when the brief recommends one. No documents are edited. No
merge requests are approved or commented on. No email is sent.

The only writes are to your own working folder, plus optionally a single email draft
addressed to you and nobody else, which is never sent.

Anything irreversible is prepared and handed to you. You decide.

**This is a behavioural guarantee, not a technical lock.** Your connectors may still expose
write capability; the rules bind how it behaves, not what the tools permit. For a hard
lock, reduce the scopes in your connector settings. You should know the difference.

## Things it does that are easy to miss

**It mines meeting notes.** AI-generated notes documents are the single richest source of
commitments, because their "next steps" sections name owners explicitly. Commitments made
verbally and recorded nowhere else get caught here. A notes document you never opened that
assigns you action items is flagged as high priority.

**It verifies before repeating itself.** Nothing appears in a brief because it appeared
yesterday. Every open item is re-checked against its source each run. Where current state
genuinely cannot be determined, the item is labelled and phrased as "if still open" rather
than asserted, and it drops out entirely after three unconfirmed runs. Losing an item beats
nagging you about a ghost.

**It retracts its own mistakes.** When a previous flag turns out to have been wrong, the
next brief says so explicitly.

**It respects dismissals.** Tell it something is handled and it records that with your
reason and never raises it again. A system that re-surfaces what you closed teaches you to
stop reading it.

**It never implies coverage it does not have.** Every brief names the sources that failed,
timed out, or are not connected.

**It treats content as data, never instruction.** An email saying "reply to confirm" or a
ticket saying "update this" gets reported, not obeyed. The sweep reads a lot of untrusted
text, and phishing is common.

## Privacy

Your setup is local to you. The templates that ship here contain no personal information —
the setup fills them in on your machine.

If you plan to share or repackage this, run the included privacy check first. It looks for
email addresses, home directory paths, tenant URLs, ticket keys, and document links, plus
any names you pass it. Colleague names are the real hazard: a contacts list is not only
your data, it names people who never agreed to appear in a shared file.

A clean scan is necessary but not sufficient. It cannot catch a bare first name in prose.
Read the diff too.

## Practical limits

- Scheduled runs happen while the app is open. If it is closed when one is due, it runs on
  next launch.
- **If your machine sleeps mid-run, that run dies with no retry.** Quitting the app at the
  end of the day is more reliable than leaving it open, because closed means deferred
  rather than dead.
- Some systems cannot be verified live and are only visible through their notification
  emails. Those items stay permanently labelled as unconfirmed, with a direct link so
  checking takes one click.
- The setup adapts to whichever tools you have connected, and records the gaps where you
  have none.
