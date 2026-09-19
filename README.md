# nbrain

Your second brain, standalone. nbrain watches your work surface (mail, calendar, chat, meeting
notes, tickets, code hosting, and any MCP server), keeps a durable ledger of what you promised
and what is slipping in an **Obsidian-compatible markdown vault**, and writes a short daily brief.
It runs without Claude Cowork, on the model provider you choose: Anthropic, AWS Bedrock, OpenAI,
OpenRouter, Ollama or any OpenAI-compatible local endpoint.

It is read-only towards every source by construction. It observes and reports; you decide and act.

## What you get

- **A daily brief** under 550 words: what is due, who is waiting on you, what is slipping and why,
  meetings needing action, tomorrow, and an honest list of what could not be checked. Markdown and
  email-safe HTML, plus optional Gmail draft, Gmail send or Slack DM to yourself.
- **Durable memory** as an Obsidian vault. One note per tracked item with YAML properties (due,
  promised to, verified, unconfirmed runs, dismissed reason), notes for people, projects and
  meetings, daily briefs, weekly reviews and sweep logs. `radar.md` and `memory.md` are rendered
  from the vault every run. Open the folder in Obsidian and everything links.
- **Verification before repetition.** Every open item is re-checked at its source each run. An
  item unconfirmed for three runs moves to the watch list and leaves the brief. Dismissed items stay
  dismissed.
- **A local web UI** with settings, brief viewer, ledger, source health, run-now, and an
  interactive relationship graph with computed patterns ("6 open commitments converge on Ada",
  "Infra Agent holds the oldest open items").
- **A weekly review** on Friday: what slipped and why, delivery rate, one process change.

## Install

Requires Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
cd nbrain-agent
uv sync
uv run nbrain setup
```

`uv run nbrain …` works from the repo. To get a plain `nbrain` command on your PATH:

```bash
uv tool install --editable .
```

## Setup

`nbrain setup` walks through: vault location, model provider (with a test call), sources, MCP
servers, delivery channels, schedule, then **discovery** (reads two weeks of calendar, a month of
mail, your tickets and merge requests to propose your role track, tier-1 people, standing meetings,
projects and noise senders) and finally the five questions only you can answer: priorities and
their weights, whose replies never wait, confusable names, timing, and delivery confirmation. It
ends with a dry-run sweep so you can tune thresholds before anything is scheduled.

`nbrain setup --defaults` writes a default config with no questions, for editing by hand or in
the web UI.

Everything lives in the vault:

```
~/nbrain-vault/
  nbrain/config.yaml      settings (no secrets)
  nbrain/.env             tokens, chmod 600   (or the OS keyring: `nbrain secret NAME --keyring`)
  nbrain/rules.md         the operating rules
  Items/  People/  Projects/  Meetings/
  Briefs/YYYY-MM-DD.md (+ .html, latest.md)   Reviews/   Sweeps/
  radar.md  memory.md
```

## Model providers

Set per task in `config.yaml` under `llm:` (`default`, and optional `extract`, `write`, `mcp`
overrides). `nbrain llm test --task extract` round-trips one.

| provider | model example | credentials |
|---|---|---|
| `anthropic` | `claude-opus-5` | `ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` / an `ant auth login` profile |
| `bedrock` | `us.anthropic.claude-opus-5` (inference-profile id) | standard AWS chain: env vars, `AWS_PROFILE`, SSO, a profile written by gimme-aws-creds |
| `openai` | `gpt-5` | `OPENAI_API_KEY` |
| `openrouter` | `anthropic/claude-sonnet-5`, `meta-llama/…` | `OPENROUTER_API_KEY` |
| `ollama` | `qwen3:14b` | none; `base_url` defaults to `http://localhost:11434/v1` |
| `openai_compatible` | anything | `base_url` required (LM Studio, vLLM, llama.cpp, LiteLLM) |

On Bedrock, current Claude models are served through **inference profiles**, so use the
`us.` or `global.` prefixed id (`us.anthropic.claude-opus-5`), not the bare one. If a call fails
with `ValidationException: Operation not allowed`, the account has not been granted model access:
open the Bedrock console, go to **Model access**, and enable the Anthropic models. That error does
not indicate a missing IAM permission.

Bedrock with short-lived credentials:

```yaml
llm:
  default:
    provider: bedrock
    model: us.anthropic.claude-opus-5
    region: us-east-1
    profile: nbrain
    refresh_command: "gimme-aws-creds --profile nbrain"
    refresh_when_expiring_within_minutes: 30
```

Before each sweep nbrain reads the profile's expiry, runs `refresh_command` when the credentials
are missing or about to expire, and skips the run with a clear log line if the refresh needs an
interactive MFA prompt. The daemon retries at the next tick.

Local models without tool calling: set `output_mode: prompted` (the wizard does this for Ollama).

Anthropic Opus 5 / Fable 5.1 requests include server-side refusal fallbacks by default; set
`anthropic_fallbacks: false` to turn that off.

## Sources

| source | needs | read scopes / token |
|---|---|---|
| Google Workspace | an OAuth **Desktop app** client from the Google Cloud console with Gmail, Calendar, Chat, Drive and Docs APIs enabled; save it as `nbrain/credentials.json`; run `nbrain auth google` | `gmail.readonly`, `calendar.readonly`, `chat.messages.readonly`, `chat.spaces.readonly`, `drive.readonly`, `documents.readonly`. `gmail.compose` / `gmail.send` are added **only** if you enable those delivery channels |
| GitLab | `GITLAB_TOKEN` | create it with the `read_api` scope |
| Slack | `SLACK_TOKEN` (user token) | `channels:history`, `groups:history`, `im:history`, `mpim:history`, `users:read`, `search:read`; add `chat:write` only for Slack DM delivery |
| Jira | `JIRA_TOKEN` (+ account email for Cloud; bearer for Data Center) | API token |

Store tokens with `nbrain secret GITLAB_TOKEN` (prompts, writes `nbrain/.env`) or
`nbrain secret GITLAB_TOKEN --keyring`.

### Google Workspace, click by click

This is the fiddliest part of setup, and it is a one-off. You are creating an OAuth client that
belongs to you, so nbrain talks to Google as you, with read-only scopes.

**1. Create or pick a project.** Open
[console.cloud.google.com](https://console.cloud.google.com/), then the project dropdown in the
top bar, then **New project**. Name it `nbrain` and click **Create**. Make sure it is selected in
the top bar before continuing.

**2. Enable the five APIs.** For each of Gmail API, Google Calendar API, Google Chat API, Google
Drive API and Google Docs API: open **APIs & Services → Library**, search the name, click the
result, click **Enable**. Direct links, with your project selected:

- [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
- [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
- [Google Chat API](https://console.cloud.google.com/apis/library/chat.googleapis.com)
- [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
- [Google Docs API](https://console.cloud.google.com/apis/library/docs.googleapis.com)

**3. Configure the consent screen.** Go to **APIs & Services → OAuth consent screen** (newer
consoles call this **Google Auth Platform → Branding**). Choose the user type:

- **Internal** if your account is on a Google Workspace domain and you can select it. Choose this.
  It skips app verification, skips the test-user list, and your sign-in never expires.
- **External** if Internal is greyed out, for example on a personal Gmail account. See the warning
  below, because this one bites.

Fill in an app name such as `nbrain`, your own email as the support email, your own email again
as the developer contact, then **Save and continue** through the remaining steps.

**4. If, and only if, you had to choose External:** go to **Audience → Test users**, click
**Add users**, add your own email address, and save. Without this, sign-in fails with
`access_denied`.

> **External apps left in Testing have refresh tokens that expire after seven days.** nbrain will
> then stop mid-week with an auth error until you run `nbrain auth google` again. Use Internal if
> your account allows it. If it does not, either accept the weekly re-auth or click
> **Publish app** on the consent screen to move out of Testing.

**5. Create the client.** Go to **APIs & Services → Credentials**, click **+ Create credentials**,
then **OAuth client ID**. Set **Application type** to **Desktop app** — not Web application, which
will fail with a redirect-URI error. Name it `nbrain desktop` and click **Create**.

**6. Download the JSON.** In the dialog, click **Download JSON**, or use the download icon next to
the client in the credentials list afterwards. It saves as something like
`client_secret_1234-abcd.apps.googleusercontent.com.json`, usually in `~/Downloads`.

**7. Give the path to nbrain.** Paste the full path at the wizard's
`Path to the downloaded credentials.json` prompt. nbrain copies it into your vault at
`nbrain/credentials.json` with owner-only permissions; the original in Downloads can be deleted.
Outside the wizard, copy it yourself and run `nbrain auth google`.

**8. Authorise in the browser.** A browser tab opens. Pick your work account and approve the
access. On an External app you will see "Google hasn't verified this app": click **Advanced**, then
**Go to nbrain (unsafe)**. It is your own client, talking only to your own account. Internal apps
show no such warning. The tab then says the flow is complete, and the token is written to
`nbrain/google-token.json`.

Check it worked:

```bash
nbrain doctor
```

**If something goes wrong**

| Symptom | Cause |
|---|---|
| `access_denied` | External app and your address is not in **Test users**, or you picked the wrong account at sign-in |
| `redirect_uri_mismatch` | The client was created as **Web application**. Delete it and create a **Desktop app** one |
| `Google hasn't verified this app` | Expected on External. Advanced, then Go to nbrain (unsafe) |
| `insufficient authentication scopes` / `has not been used in project` | One of the five APIs is not enabled. Enable it, then delete `nbrain/google-token.json` and run `nbrain auth google` |
| Auth dies every seventh day | External app still in Testing. Switch to Internal or publish the app |
| Chat returns nothing | Google Chat is the fussiest of the five. Your admin may restrict API access to it. Turn it off with `nbrain config set sources.google.chat false` and carry on without it |

Changing which sub-sources are enabled changes the scopes nbrain asks for. After such a change,
delete `nbrain/google-token.json` and run `nbrain auth google` again.

### Slack, click by click

nbrain reads Slack as **you**, not as a bot, because the commitments worth tracking are in your own
messages and your own unanswered mentions. That needs a user token.

**1. Create the app from a manifest.** Go to [api.slack.com/apps](https://api.slack.com/apps),
click **Create New App**, then **From a manifest** — not AI agent, not Starter app, not Blank app.
Pick your workspace, choose **YAML**, and paste
[`docs/slack-app-manifest.yml`](docs/slack-app-manifest.yml) from this repo. Review, then
**Create**.

The manifest asks for six read-only user scopes and nothing else:

```
search:read
channels:history  groups:history  im:history  mpim:history
channels:read     groups:read     im:read     mpim:read
users:read
```

`search:read` is the fast path. The `*:read` scopes only matter when a workspace restricts search,
in which case nbrain falls back to scanning conversation histories and needs to list them first.

**2. Install it.** On the app page, open **OAuth & Permissions** and click **Install to
Workspace**, then **Allow**. If your workspace requires admin approval, the button says *Request to
Install* instead and you wait for an admin.

**3. Copy the right token.** Still on **OAuth & Permissions**, copy the **User OAuth Token**. It
starts with `xoxp-`. Do not copy the Bot User OAuth Token (`xoxb-`); it cannot read your own history
or use search, and nbrain will report empty results.

**4. Hand it to nbrain.**

```bash
nbrain secret SLACK_TOKEN
nbrain config set sources.slack.enabled true
nbrain doctor
```

`doctor` prints the account the token resolves to. If that is you, it is wired up.

**Optional: the brief in your Slack DM.** Uncomment `chat:write` in the manifest, reinstall the app,
then `nbrain config set delivery.slack_dm.enabled true`. It only ever messages you.

**If something goes wrong**

| Symptom | Cause |
|---|---|
| `Installation was not completed` on the review screen | The authorisation popup was blocked or closed before you clicked Allow. Allow popups for `api.slack.com` and retry, or skip the popup: open the app from [api.slack.com/apps](https://api.slack.com/apps) → **OAuth & Permissions** → **Install to Workspace**, which is a full-page redirect. If that button reads *Request to Install*, your workspace needs admin approval |
| `not_authed` / `invalid_auth` | The token is wrong or was revoked on reinstall. Copy the User OAuth Token again |
| `missing_scope` | A `xoxb-` bot token was used, or the app was installed before a scope was added. Reinstall after changing scopes |
| Search silently finds nothing | Your workspace restricts `search:read`. nbrain falls back to scanning channel histories, covers fewer channels, and says so under "Not checked" |
| Only public channels appear | The token has `channels:history` but not `groups:history`, `im:history` or `mpim:history` |
| `missing_scope` from `conversations.list` | The `*:read` scopes are absent. Only bites when search is restricted and the history fallback runs |

Narrow the scan with `nbrain config set sources.slack.channels '["C123","C456"]'` when the workspace
is large; leaving it empty scans your DMs and the channels you belong to, capped.

### Any MCP server

Add servers under `mcp_servers:` (stdio or HTTP), give each the roles it covers, and nbrain runs a
bounded tool-using collector against it every sweep (capped tool calls, structured output). Tools
whose names look like writes (`create_*`, `send*`, `update*`, `delete*`, …) are filtered out before
the model sees them unless you set `allow_write: true`.

```yaml
mcp_servers:
  - name: linear
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-linear"]
    env: { LINEAR_API_KEY: "${LINEAR_API_KEY}" }
    roles: [tickets]
    max_tool_calls: 12
    instructions: "Only the PLAT team's issues matter."
  - name: notion
    transport: http
    url: https://mcp.notion.com/mcp
    headers_env: { Authorization: NOTION_TOKEN }
    roles: [notes, knowledge]
```

#### Glean

Glean speaks MCP, so it plugs into the layer above with no special support. It is a good fit for
the `knowledge` role: search, chat, document read, code search and people lookup.

Find your server URL by opening [app.glean.com/admin/about-glean](https://app.glean.com/admin/about-glean),
taking the backend domain and appending `/mcp/default`. Transport is streamable HTTP; SSE is
deprecated.

**Use a token, not OAuth.** Glean recommends OAuth for interactive hosts like Cursor, and nbrain
supports it with `auth: oauth`, but the token is held in memory only: every process re-runs the
browser flow, which the scheduled daemon cannot do. A user-scoped Client API token works
unattended.

```bash
nbrain secret GLEAN_TOKEN
```

```yaml
mcp_servers:
  - name: glean
    transport: http
    url: https://<your-backend-domain>/mcp/default
    headers_env: { Authorization: GLEAN_TOKEN }
    roles: [knowledge]
    max_tool_calls: 8
    instructions: "Prefer search over chat; cite the document each answer came from."
```

The token is sent as `Authorization: Bearer <token>`; the `Bearer ` prefix is added for you if your
value does not already carry it. As with every source, the token name lives in `config.yaml` and the
value does not.

Check what came through:

```bash
nbrain mcp tools glean
```

**Glean agents need an explicit allow.** Agents surface as tools, and they are frequently named
`run_agent_*`, which the write denylist blocks on sight. If you have a read-only agent worth
calling, such as a profiler, open it deliberately and narrowly:

```yaml
    allow_write: true
    tool_allow: ["^search$", "^read_document$", "^run_agent_profiler$"]
```

`allow_write` lifts the name-based gate, so pair it with a `tool_allow` list rather than leaving it
open. `nbrain mcp tools glean` shows exactly which tools each combination permits.

`nbrain mcp tools linear` shows every tool and whether the read-only gate allows it;
`nbrain mcp call linear list_issues '{"limit": 5}'` calls one directly.

## Running

```bash
nbrain sweep --dry-run        # full run against a throwaway copy of the vault, nothing delivered
nbrain sweep                  # the real thing
nbrain sweep --no-llm         # numbers-only brief, no model calls
nbrain sweep --source gmail   # one source, for debugging
nbrain brief                  # print the latest brief
nbrain weekly                 # write this week's review now
nbrain items list --status open
nbrain items dismiss 20260917-post-action-items --reason "handled in person"
nbrain items resolve 20260917-post-action-items
nbrain doctor                 # vault, config, every model, every source, every MCP server
nbrain ui                     # http://127.0.0.1:8765
nbrain graph export           # graph.json + graph.graphml
```

### On a schedule

```bash
nbrain daemon                 # foreground: cron jobs in your timezone + the web UI
nbrain service install        # macOS launchd agent that runs `nbrain daemon` at login
nbrain service status
nbrain service uninstall
```

The daemon reloads `config.yaml` at every run, never overlaps two sweeps (lock file), and if the
machine was asleep at the scheduled time it catches up when it wakes, within
`sweep.missed_run_grace_hours`.

Docker:

```bash
docker build -t nbrain .
docker run -v ~/nbrain-vault:/vault -p 8765:8765 nbrain
```

## How a sweep works

1. Health-check every enabled source and MCP server. Failures go straight to the "Not checked"
   line; one dead source never kills the run.
2. Re-verify every open item at its source: replied, merged, closed, cancelled. Resolved items move
   to "Resolved"; unverifiable ones count an unconfirmed run.
3. Collect deterministic signals: stale or past-due tickets, MRs awaiting review, unanswered
   threads from tier-1 people, agenda-less meetings you organise, mentions with no reply.
4. Ask the model, in bounded structured calls, to extract commitments from your own chat messages
   and meeting notes ("next steps" sections) and to classify email threads. Content is wrapped in
   `<data>` blocks and treated as data, never instruction.
5. Merge into the vault by `(source, source_id)`. Low-confidence findings go to the watch list.
6. Compute metrics in code: due today, waiting on you, oldest age, commitment delivery score,
   priority balance with the 70/30-for-two-runs alarm, changed since yesterday.
7. Ask the model for the judgement parts only: today's one thing, a four-sentence assessment,
   three to five ordered actions. Everything else is rendered from data.
8. Enforce the caps (550 words, 3 due today, top 5 waiting, top 3 slipping, one line of caveats).
9. Write `Briefs/`, `radar.md`, `memory.md`, `Sweeps/`, then deliver.

Role tracks change the emphasis: `ic` leads with your own delivery, `lead` adds a "you are the
bottleneck" block above it, `manager` puts decisions and team blockers first and never reports
per-person metrics.

## Privacy and the read-only guarantee

- Source classes have no write methods. Google and Slack scopes are read-only unless you turn on a
  delivery channel, and then only the compose/send scope is added.
- MCP tools that look like writes are filtered out by name unless a server is marked
  `allow_write: true`.
- The only email nbrain will ever create is addressed to you. Reply drafts are never created.
- The vault is yours and local. Nothing in this repo contains personal data; the vault must never be
  committed to a shared repository (the wizard warns if you pick a path inside one).

Behavioural rules the model is given are in `nbrain/rules.md` in your vault. Where a rule can be
enforced in code, it is.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
```

The original Cowork plugin this was ported from lives in `../plugin/`.
