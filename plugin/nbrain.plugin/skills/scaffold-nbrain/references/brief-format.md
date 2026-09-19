# Brief format spec

Chosen by the user 13 Aug 2026: **dashboard on top, narrative underneath, detail below that.**
All four extras enabled: priority balance, commitment delivery score, changed since
yesterday, tomorrow preview.

Applies to the daily sweep. The weekly review uses the same visual language but leads
with the narrative rather than the metric strip.

---

## The brief is an ALERT. radar.md is the RECORD.

Learned the hard way on 13 Aug 2026: the first HTML brief ran to ~1,400 words because it
reproduced `radar.md` in full. That is a six-minute read and it defeats the purpose.

**The brief's job is to make the user act today.** Anything he doesn't need in order to act
today belongs in `radar.md`, which he can open when he wants the full picture. Duplication
is the failure mode to guard against, not incompleteness.

When a section would run long, **truncate and point at the file** — "4 more in radar.md" —
rather than listing everything.

## Hard caps. Not guidance — limits.

| Section | Cap |
|---|---|
| Total | **550 words.** Over that, cut — do not reformat. |
| Metric strip | 4 numbers, no commentary |
| Today's one thing | 2 sentences |
| Due today | **3 items max.** More than 3 things due today is a planning problem; say that in one line instead of listing them. |
| Assessment | **4 sentences.** The hardest cap to respect and the most important. |
| Do this, in order | 5 actions, **one line each** including the effort hint |
| Priority balance | 2 bars + 1 sentence |
| Changed since yesterday | 3 lines total — one each for new, moved, cleared |
| Waiting on you | **Top 5 by urgency, one line each.** No sub-detail. Then "+N more in radar.md". |
| Slipping | **Top 3.** Then "+N more". Cause in the same line, not underneath. |
| Today's meetings | Only meetings needing action. Roll the rest into one summary line. |
| Tomorrow | 3 lines |
| Not checked | **One line.** A paragraph of caveats trains him to skip the section that matters most. |

**Read-time contract:**
- Sections 1–3 (metric strip, one thing, due today): 30 seconds, **6 lines maximum**.
- Sections 4–5 (assessment, ordered actions): 30 seconds.
- Everything after: skim, not read.

If the urgent block exceeds 6 lines the sweep is over-flagging. Cut it.

## Email HTML constraints — read before writing markup

Gmail is not a browser.

- Use `htmlBody` on `create_draft`, and **always** supply `body` too as the plain-text
  fallback. Some clients and the mobile preview use it.
- **Inline styles only.** Gmail strips `<style>` blocks.
- **Tables for layout.** Flexbox and CSS grid are unreliable across clients.
- **Literal hex colours.** CSS variables do not exist in email.
- **Background fills need the `bgcolor` ATTRIBUTE, not just CSS.** Confirmed 13 Aug 2026:
  Gmail stripped every `style="background:…"` on table cells, so the tinted metric cards
  and the one-thing banner rendered as flat coloured text on white. Always write both:
  `<td bgcolor="#FCEBEB" style="background:#FCEBEB;…">`. Same for badge spans — wrap them
  in a single-cell table with `bgcolor` if the pill fill matters.
- **Tint only the urgent block.** Metric strip, the one-thing banner, and status badges.
  Everything from "Waiting on you" down stays flat. Colour then means "act on this"
  instead of decoration, and there is less to break across clients and dark mode.
- No web fonts, no icon fonts, no external images. System font stack only.
- Keep total HTML under roughly 40KB or Gmail clips the message and hides the end.
- Every link is a plain `<a href>`. No JavaScript, ever.

### Palette (light-mode assumption; acceptable if dark mode inverts imperfectly)

| Role | Background | Text | Accent line |
|---|---|---|---|
| Danger / overdue | `#FCEBEB` | `#A32D2D` | `#E24B4A` |
| Warning / due today | `#FAEEDA` | `#854F0B` | `#EF9F27` |
| Success / cleared | `#E1F5EE` | `#0F6E56` | `#1D9E75` |
| Accent / neutral info | `#E6F1FB` | `#185FA5` | `#378ADD` |
| Surface | `#F1EFE8` | — | — |
| Text primary | — | `#2C2C2A` | — |
| Text secondary | — | `#5F5E5A` | — |
| Text muted | — | `#888780` | — |
| Border | — | — | `#D3D1C7` |

Font stack: `-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif`

---

## Section order

1. **Metric strip** — four cells: Due today, Waiting on you, Oldest item (days), Commitments delivered (n/m).
2. **Today's one thing** — a tinted banner with a left accent bar. One sentence on what matters and why.
3. **Due today** — table. Item, why it's due, status badge, link. Nothing else belongs here.
4. **Narrative** — 3 to 4 sentences of honest assessment. Serif is unavailable in email; use italic instead.
5. **Do this, in order** — numbered actions, 3 to 5, each with an effort hint ("two minutes", "before 17:00").
6. **Priority balance** — two bars, Infra AI Agent vs Dashboard, with a call-out if one is starving.
7. **Changed since yesterday** — new / moved / cleared.
8. **Waiting on you** — table with age, verified badge, link.
9. **Slipping** — table with cause, not just status.
10. **Today's meetings** — one line each; fuller block for external or client meetings.
11. **Tomorrow** — meeting load and anything needing prep tonight.
12. **Not checked** — every source that failed, timed out, or isn't connected.

## How to compute the four extras

**Priority balance.** Count signals of activity per project over the last 24h (or since
the last run): Jira issues updated, GitLab commits and pipelines, meetings held, chat
messages sent. Express as a percentage split between the {{PROJECT_A}} and the
{{PROJECT_B}}. They carry **equal weight** — if the split is
worse than roughly 70/30 for two or more consecutive runs, say so plainly in the narrative.
This is the imbalance the user explicitly asked to catch.

**Commitment delivery score.** From `radar.md` "Commitments I made": delivered ÷ total
still-tracked. Show as `n/m`. Do not soften it and do not editorialise — the number is
the point. Exclude items in Dismissed.

**Changed since yesterday.** Diff against the previous `radar.md`:
- **New** — flagged for the first time this run.
- **Moved** — age increased, status changed, or a reply arrived.
- **Cleared** — verified resolved since the last run. Always show these; progress is
  what makes the brief worth opening.
If nothing changed, say "no movement since yesterday" — that is itself a finding.

**Tomorrow preview.** Read tomorrow's calendar. Report meeting count, total booked hours,
anything where the user is the organiser, and anything needing prep tonight. Flag a meeting
with no agenda where he is the organiser.

---

## HTML skeleton

Adapt freely — this is a starting point, not a straitjacket. Omit any section with
nothing in it rather than printing an empty heading.

```html
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;color:#2C2C2A;max-width:640px">

<table width="100%" cellpadding="0" cellspacing="6" style="border-collapse:separate;margin-bottom:16px">
<tr>
<td width="25%" style="background:#FCEBEB;border-radius:8px;padding:10px">
<div style="font-size:11px;color:#A32D2D">Due today</div>
<div style="font-size:22px;color:#A32D2D">2</div></td>
<td width="25%" style="background:#F1EFE8;border-radius:8px;padding:10px">
<div style="font-size:11px;color:#5F5E5A">Waiting on you</div>
<div style="font-size:22px">7</div></td>
<td width="25%" style="background:#F1EFE8;border-radius:8px;padding:10px">
<div style="font-size:11px;color:#5F5E5A">Oldest</div>
<div style="font-size:22px">16d</div></td>
<td width="25%" style="background:#F1EFE8;border-radius:8px;padding:10px">
<div style="font-size:11px;color:#5F5E5A">Delivered</div>
<div style="font-size:22px">2/8</div></td>
</tr>
</table>

<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px">
<tr>
<td width="4" style="background:#E24B4A"></td>
<td style="background:#FCEBEB;padding:10px 14px">
<div style="font-size:12px;color:#A32D2D;margin-bottom:2px">Today's one thing</div>
<div style="color:#A32D2D">Six days of {{MONITORING_SYSTEM_2}} silence ends today with a date or a real escalation. {{MANAGER}} is in the {{TIME}}.</div>
</td>
</tr>
</table>

<div style="font-size:12px;color:#5F5E5A;border-bottom:1px solid #D3D1C7;padding-bottom:4px;margin-bottom:8px">DUE TODAY</div>
<table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px">
<tr>
<td style="padding:8px 0;border-bottom:1px solid #D3D1C7">
<a href="URL" style="color:#185FA5;text-decoration:none">Post action items in the AI-Infra-Agent chat</a>
<div style="color:#888780;font-size:12px">Promised to {{COLLEAGUE}} on {{DATE}}, chased once. Before their {{TIME}} standup.</div>
</td>
<td width="80" align="right" style="padding:8px 0;border-bottom:1px solid #D3D1C7">
<span style="background:#FCEBEB;color:#A32D2D;font-size:11px;padding:2px 7px;border-radius:6px">overdue</span>
</td>
</tr>
</table>

<div style="font-style:italic;color:#2C2C2A;margin:18px 0;padding-left:12px;border-left:2px solid #D3D1C7">
Yesterday was entirely dashboard work. The Infra AI Agent surfaced once, blocked on someone else, and its Jira has not moved in sixteen days.
</div>

<div style="font-size:12px;color:#5F5E5A;border-bottom:1px solid #D3D1C7;padding-bottom:4px;margin-bottom:8px">DO THIS, IN ORDER</div>
<table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;margin-bottom:18px">
<tr><td width="24" valign="top" style="padding:4px 0;color:#A32D2D">1.</td>
<td style="padding:4px 0">Post the action items you promised {{COLLEAGUE}}. <span style="color:#888780">Before 17:00.</span></td></tr>
</table>

<div style="font-size:12px;color:#5F5E5A;border-bottom:1px solid #D3D1C7;padding-bottom:4px;margin-bottom:8px">PRIORITY BALANCE</div>
<table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;margin-bottom:18px">
<tr><td width="90" style="color:#5F5E5A;padding:3px 0">Infra agent</td>
<td style="padding:3px 0"><table width="18%" cellpadding="0" cellspacing="0"><tr><td style="background:#E24B4A;height:6px;border-radius:3px">&nbsp;</td></tr></table></td>
<td width="40" align="right" style="padding:3px 0">18%</td></tr>
<tr><td width="90" style="color:#5F5E5A;padding:3px 0">Dashboard</td>
<td style="padding:3px 0"><table width="82%" cellpadding="0" cellspacing="0"><tr><td style="background:#378ADD;height:6px;border-radius:3px">&nbsp;</td></tr></table></td>
<td width="40" align="right" style="padding:3px 0">82%</td></tr>
</table>

<div style="font-size:12px;color:#888780;border-top:1px solid #D3D1C7;padding-top:10px;margin-top:18px">
Not checked: {{MONITORING_SYSTEM}} and {{MONITORING_SYSTEM_2}} (no access). GitLab state (email only, permanently unconfirmed). {{DATA_PLATFORM}} alerts (not parsed).
</div>

</div>
```

## Badges

| Meaning | Markup |
|---|---|
| Overdue | `<span style="background:#FCEBEB;color:#A32D2D;font-size:11px;padding:2px 7px;border-radius:6px">overdue</span>` |
| Due today | `<span style="background:#FAEEDA;color:#854F0B;font-size:11px;padding:2px 7px;border-radius:6px">today</span>` |
| Verified live | `<span style="background:#E1F5EE;color:#0F6E56;font-size:11px;padding:2px 7px;border-radius:6px">verified</span>` |
| Unconfirmed | `<span style="background:#F1EFE8;color:#5F5E5A;font-size:11px;padding:2px 7px;border-radius:6px">unconfirmed</span>` |

## Tone

Plain and direct. No filler, no hedging, no corporate register. Never "successfully",
"leverage", "seamless", or "just". If nothing is slipping, one line saying so beats a
padded section. If the picture is bad, say it plainly — a brief that flatters is worthless.
