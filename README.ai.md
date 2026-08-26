# awewarm Bootstrap Protocol

This document is for AI coding agents. Help the user install and configure `awewarm`, a scheduler that keeps AI coding-plan subscription windows warm with one minimal request per window.

## Quota Is Money

Every `awewarm run <id>` and bare `awewarm run` sends REAL requests against the user's coding-plan quota. Never run them unless the user explicitly asks. To preview what would fire, `awewarm status` shows each connection's next due moment.

`awewarm init` and `awewarm config add` are interactive (they prompt for choices and API keys, and send one test request per added connection). Tell the user to run them in their own terminal.

## Language Behavior

- Reply in the user's language when possible.
- If the user asks in Chinese, continue in Chinese.
- If the user asks in English, continue in English.

## Step 1: Install awewarm CLI

### Prerequisites

- Python >= 3.9 (`python3 --version`)
- pip available (`pip --version`)

If Python is missing, tell the user to install it from https://www.python.org/.

### Steps

```bash
pip3 install awewarm
awewarm -v
```

Expected output: `awewarm X.Y.Z`

---

## Step 2: Install the awewarm skill

Install the skill so the agent can manage warm-up schedules in this and future sessions. Choose one of the following options.

### Option A: Via aweskill (recommended if aweskill is available)

Use this option if the user already has aweskill installed, or is willing to install it. This gives full skill management — install, update, projection, backup.

#### Prerequisites

- Node.js >= 20 (`node --version`) — required by aweskill
- npm available (`npm --version`)

If Node.js is missing, tell the user to install it from https://nodejs.org/.

#### Steps

##### A1. Install aweskill (if not already installed)

```bash
npm install -g aweskill
```

##### A2. Initialize the aweskill central store (if not already done)

```bash
aweskill store init
```

##### A3. Install the awewarm skill from GitHub

```bash
aweskill install wehuman01/awewarm
```

##### A4. Identify the current agent

```bash
aweskill agent supported
```

Look for lines marked with `✓`. Common agent ids: `claude-code`, `cursor`, `codex`, `gemini-cli`, `windsurf`, `opencode`, `qwen-code`.

If you cannot determine the agent id, ask the user.

##### A5. Project the awewarm skill to this agent

```bash
aweskill agent add skill awewarm --global --agent <agent-id>
```

##### A6. Verify

```bash
aweskill agent list --global --agent <agent-id>
```

Expected: `awewarm` shows as `linked`.

---

### Option B: Direct copy (no aweskill needed)

Use this option if the user does not have aweskill and does not want to install Node.js. This copies the SKILL.md file directly into the agent's skill directory.

#### Prerequisites

- `curl` or `wget` available

#### Steps

##### B1. Identify the current agent's skill directory

Determine which agent is running and its global skill directory:

| Agent | Skill directory |
|---|---|
| Claude Code | `~/.claude/skills/awewarm/` |
| Codex | `~/.codex/skills/awewarm/` |
| Cursor | `.cursor/skills/awewarm/` (project-level) |
| Gemini CLI | `~/.gemini/skills/awewarm/` |
| Windsurf | `~/.windsurf/skills/awewarm/` |
| OpenCode | `~/.opencode/skills/awewarm/` |
| Qwen Code | `~/.qwen/skills/awewarm/` |

If the agent is not in this list, ask the user where to place the skill file.

##### B2. Download and place SKILL.md

```bash
mkdir -p <skill-directory>
curl -fsSL https://raw.githubusercontent.com/wehuman01/awewarm/main/resources/skills/awewarm/SKILL.md -o <skill-directory>/SKILL.md
```

Replace `<skill-directory>` with the path from step B1.

---

## Step 3: Scan local accounts (read-only, safe to run)

```bash
awewarm discover
```

This scans for local `claude` / `codex` CLIs and their login state. No network requests, no credentials read. Report the findings to the user:

- `✓ ... CLI found` — the CLI is installed
- `✓ ... authentication found` — already logged in, awewarm can reuse it
- `? ... authentication not found` — the user must run `claude auth login` or `codex login` first
- `✓ Subscription session window detected: 5 hours` — the window semantics are known, interval renewal is unlocked

---

## Step 4: Tell the user to run `awewarm init`

`awewarm init` is interactive — it confirms each discovered account, picks a warm-up mode, sets fixed times, and installs the background scheduler. **Do not run it yourself.** Tell the user:

> Run `awewarm init` in your terminal. It will list your local accounts, ask which ones to manage, and install the scheduler.

For a subscription endpoint (OpenAI Chat / OpenAI Responses / Anthropic-compatible base URL + API key), the command is `awewarm config add` — also interactive (API key prompt), so it also belongs in the user's terminal. The same command re-adds a local `claude` / `codex` account the user removed earlier.

Platform note: the scheduler installs on macOS (launchd), Windows (Task Scheduler), and Linux (systemd user timer). API keys live in `secrets.json` on every platform — env-var references were removed because background schedulers cannot read shell variables. On headless Linux/SSH accounts, the user may need `loginctl enable-linger $USER` first; without systemd, cron the tick: `* * * * * awewarm tick`.

---

## Step 5: Verify and tune (safe to run)

After the user's `init`:

```bash
awewarm status                      # per-connection mode, window, next due moment
awewarm config path                 # config / state / log locations
awewarm status <id> --json          # redacted machine-readable dump
```

On the user's request you may also:

```bash
awewarm config set <id> --times 06:35,11:40,16:45   # set fixed warm-up times
awewarm config set <id> --mode interval              # switch mode (interval needs a verified window)
awewarm config set <id> --off                       # pause while on vacation
awewarm scheduler install                           # (re)install the background scheduler
awewarm scheduler install --wake                    # also wake a lid-closed sleeping machine at slot/renewal
                                                     # moments (macOS: one sudo; Windows: no grant needed)
```

`interval` mode stays locked until the window is verified or user-confirmed. If the user wants it, guide the three-step flow in the skill (`awewarm run <id>`, observe the quota reset, `awewarm config set <id> --window <minutes>`) — but that request itself consumes quota, so only run it when the user asks.

## Useful commands

Read-only commands (safe to run in agent):

```bash
awewarm discover                    # scan local CLIs and logins
awewarm status                      # summary + next due moment
awewarm status <id>                 # one connection in detail
awewarm status --remote / --local   # delegated connections only (with the server health line) / locally scheduled only
awewarm config set <id>             # show current schedule settings
awewarm status --json               # redacted machine-readable dump
awewarm config path                 # file locations
awewarm self-update --check          # show current/latest version
```

Local-only changes (run on user request):

```bash
awewarm config set <id> --times HH:MM,...  # set fixed times
awewarm config set <id> --mode ...         # switch mode
awewarm config set <id> --on / --off       # resume / pause scheduling
awewarm config set <id> --anchor HH:MM     # anchor past an already-open window (no request)
awewarm config remove <id>                 # delete connection + stored API key (confirm first)
awewarm scheduler install [--wake]         # background scheduler (launchd / Task Scheduler / systemd);
                                           # --wake arms RTC wake-from-sleep for slot/renewal moments
awewarm self-update                      # upgrade awewarm
```

User-only commands (interactive or quota-consuming):

```bash
awewarm init                        # interactive onboarding
awewarm config add                  # interactive, prompts for API key
awewarm run <id> [--reset-due]       # sends a real request now (prompts; schedule untouched unless --reset-due)
awewarm run [--force]               # fires every enabled connection now (prompts; --force skips, required when non-interactive)
```

The background scheduler's own command is `awewarm tick` (hidden, invoked once a minute by the installed agent) — never call it manually; use `awewarm run` for manual activations and `awewarm status` to preview what would fire.

Commands from pre-0.3 releases (`add plan`, `times`, `enable`, `verify`, `anchor`, `activate`, `inspect`, ...) still work as hidden aliases that print their new spelling. `remote status` folded into `status --remote` (hidden alias likewise). `awewarm update` was removed outright in v0.5.0 — use `awewarm self-update`.

## Safety Rules

- Never run `run <id>` or bare `run` unless the user explicitly asks — they consume plan quota.
- Do not run `init` or `config add` inside the agent — they are interactive.
- API keys live in `secrets.json` (0600), never in config files. Never ask the user to paste an API key into chat; all awewarm output is redacted.
- `config set <id> --persist-key on` (store the key on the server's disk, plaintext) asks for confirmation and is discouraged by design — if the user asks for it, state the trade-off (readable by whoever reads the server box) and let them run it in their own terminal; the same applies to `config restore` of a backup containing persisted-key connections.
- `config backup` writes API keys and the pairing token in plaintext into one archive — never create, move, or transmit one for the user without saying so.
- Read config through `status`; never hand-edit config.json or state.json.
- If any command fails, report the exact command and error message. Do not silently retry.

## Final Step

After setup, tell the user to invoke skills (`/` in Claude Code, `$` in Codex, or the equivalent in other agents) and check if `awewarm` appears in the list. If it does, the skill is ready to use immediately. If not, the user should restart the agent.

> awewarm is installed and configured. Invoke skills (type `/` or `$` depending on your agent) and look for `awewarm` — if it appears, you're good to go. If not, restart the agent. Then you can ask me things like:
>
> - "When is the next warm-up for claude-code?"
> - "Set my GLM plan to warm at 06:35 and 12:35."
> - "Pause warm-ups while I'm on vacation."

If the user is speaking Chinese, use this version instead:

> awewarm 已安装并配置完成。请调用 skills（输入 `/` 或 `$`，取决于你的 agent），看看列表中是否出现了 `awewarm`。如果出现了，说明已就绪可以直接使用。如果没有，请重启 agent 后再试。然后你可以继续问我，例如：
>
> - "claude-code 下一次保温是什么时候？"
> - "把我的 GLM 套餐保温时间改成 06:35 和 12:35。"
> - "我要休假，暂停保温。"

---

## Next Steps

### awewarm-hub — one server, many users

If the user wants to share one always-on server with a team or family (paired through one-time invites), that is the separate **[awewarm-hub](https://github.com/wehuman01/awewarm-hub)** package (`pip install awewarm-hub`). The *operator* sets it up on the 24/7 box by following its agent doc: https://github.com/wehuman01/awewarm-hub/blob/main/README.ai.md — hub *users* stay right here with plain awewarm.

### Community hub — no server of your own

The project's developer runs an invite-based community hub at https://awewarm.wehuman.top. If the user has no always-on box of their own, guide them through the tutorial: https://github.com/wehuman01/awewarm/blob/main/docs/community-hub/README.md — installing awewarm, setting up the first connection (`awewarm config add`, interactive — their terminal), requesting an invite at peng@wehuman.top if they don't have one, then pairing (`remote connect --invite awi_...`) and delegating (`config set <id> --remote`). State the trust rule before they delegate: their API key's — or, for an account connection, their login credential's — plaintext passes through the hub's RAM. Account delegation (Claude Code / Codex logins) is supported since v0.6.3 and asks for an explicit confirmation naming the server; the hub box must have the matching CLI installed.

### aweswitch — agent profile switching

If the user launches coding sessions against different providers, point them to [aweswitch](https://github.com/Webioinfo01/aweswitch), an agent profile switcher for Claude Code, Codex, and OpenCode. aweswitch manages which provider a session launches with; awewarm keeps that provider's subscription window open underneath.
