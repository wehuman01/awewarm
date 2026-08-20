# awewarm: I Let My Agent Keep My Subscription Window Warm

![awewarm](../../logo/logo.png)

Here is the uncomfortable math of AI coding subscriptions: every plan has a usage window. Claude Max gives you 5 hours. Codex plans have their own. Third-party token subscriptions work the same way. The window opens when your first request of the day hits the provider, and it closes 5 hours later — whether you are coding or not. You start at 9 AM, the window closes at 2 PM. You take a lunch break, come back at 3 PM, and your first request opens a brand new window. You just burned quota on a window you only used for 90 minutes.

awewarm's answer is predictable warmth. It sends one minimal request — literally `"Reply with exactly: ok"` — at the right time so the window is always already open when you sit down to code. You never arrive at a cold start. You never waste a window on a partial session. The tool is invisible: it runs as a system scheduler tick, once a minute, with no daemon, no persistent process, and no token burn beyond the single warm-up request.

awewarm was born out of this exact frustration. And it makes a second bet on top of the first: a scheduler is only useful if someone configures and maintains it — times to set, modes to choose, providers to add, failures to recover from. That someone does not have to be you. awewarm is built as a tool for the AI age: operable end-to-end by an AI agent. It ships with a README the agent reads, a skill the agent uses, and a CLI the agent runs. The learning cost of the tool moves from you to the agent. You say what you want; the agent figures out how.

Then I let my agent set the whole thing up.

I told it: "Read https://github.com/mugpeng/awewarm/blob/main/README.ai.md and follow it." Then I went to get a coffee.

When I came back, awewarm was installed, the scheduler was registered with launchd, two connections sat in `~/.config/awewarm/config.json`, and the keys were in `~/.config/awewarm/secrets.json` with 0600 permissions. It had discovered my Claude Code login from the macOS Keychain, detected my Codex auth from `~/.codex/auth.json`, and set up a fixed-time grid for Claude Code — five slots per day, spaced 5 hours and 5 minutes apart, covering the full day from my stated start time. It also added a subscription connection for my GLM coding plan with the token plan URL I had in my env.

Then it said: "The scheduler is installed. Ticks run once a minute. You can check status with `awewarm status`."

That is the new shape of installing an agent tool. The install is a task. The agent does tasks. So I gave the task to the agent.

GitHub: [github.com/mugpeng/awewarm](https://github.com/mugpeng/awewarm)

## Two Modes: Fixed and Interval

awewarm has two scheduling modes, and the choice between them is the most important decision you make for each connection.

### Fixed Mode: Absolute Times, Always Safe

Fixed mode is the default. You give it a list of local times (`--times 06:35,11:40,16:45,21:50`) and days (`weekday` or `every-day`). At each slot, awewarm sends one request. Each hit opens a fresh window.

If the machine was asleep at the slot time, the slot fires late within a catch-up window (default 30 minutes, configurable). Past that, it is recorded as skipped — no wasted requests, no false warmth. A slot landing within 30 minutes of a previous success is also skipped, preventing double-fires when the machine wakes and catches up.

During setup, when the window duration is known (Claude Code is a verified 5 hours), awewarm asks for the plan's daily quota reset time and offers a full-day grid: one slot per window, spaced `window + 5 minutes` apart. For a 5-hour window starting at 01:14, you get: `01:14, 06:19, 11:24, 16:29, 21:34`. Five slots, five windows, full day coverage. You never think about it again.

Fixed mode is the only mode available while window semantics are unknown. Unverified plans — like Codex, whose window duration is not publicly documented — start here. You can switch to interval mode once the window is verified or user-confirmed.

### Interval Mode: Rolling Renewal

Interval mode is for when you know the window duration and want the tool to chain itself. After each successful warm-up request, the next request is scheduled `window + grace + jitter` later (default: 300 minutes + 75 seconds + up to 30 seconds of jitter).

The grace period is the key design decision. It runs **after** the old window has closed — firing early would land inside the old window and start nothing. The 75-second grace plus jitter ensures the old window is fully expired before the new one opens. No overlap. No wasted windows.

With no success recorded yet, one request fires immediately as the first anchor. `--start HH:MM` defers that first anchor to a specific time — useful for aligning the chain with your actual work schedule. A manual `run <id>` never shifts the renewal chain unless `--reset-due` is explicitly used.

Interval mode is **locked** until the window is verified or user-confirmed with `--window`. This is intentional: interval mode with a wrong window duration is worse than fixed mode. It would chain windows at the wrong interval, burning quota on useless warm-ups. The tool refuses to guess.

## The Health Ladder: Four States, One Recovery Path

This is the part of awewarm that gets the most design attention. Most cron-based tools have two states: working or broken. awewarm has four, and the transitions between them are deliberate.

```
connected ──首次节点失败──▶ failing ──连续N个节点丢失──▶ degraded ──再连续N个节点丢失──▶ auto-disabled
   ▲                          │                              │                              │
   └────────── 任一次成功（节点尝试/catch-up重试/手动run）──────┘                              │
                                                              └──── 只有 --on 或手动 run 成功 ──┘
```

### Connected

Normal operation. Fixed slots fire on time. Interval chains renew on schedule. The scheduler ticks once a minute, checks state, and sends requests when due.

### Failing

A node fails. A node is a scheduled event — a fixed slot time or an interval renewal moment. One failure is not a crisis. Networks flicker. APIs return transient 429s. The machine was briefly asleep.

In the failing state, catch-up retries are allowed: by default, 5 attempts within 30 minutes, spaced roughly 5 minutes apart. awewarm keeps trying, and any single success resets the entire ladder back to **connected**. The failure counter resets to zero. The schedule continues as if nothing happened.

### Degraded

After `degradeAfterNodes` (default 3) consecutive nodes are lost, the connection enters degraded state. This is the signal that something is genuinely wrong — the API key expired, the CLI binary moved, the network is down for real.

In degraded state, catch-up retries stop. Each node gets exactly one attempt. Interval mode probes once per window. Fixed mode fires each slot exactly once. The tool is still trying, but it has accepted that the problem is not transient. It conserves effort and avoids flooding a broken endpoint.

Any success in degraded state — a node attempt that finally works, or a manual `run <id>` — resets the ladder back to **connected**. The counters clear. The schedule resumes.

### Auto-Disabled

After another `degradeAfterNodes` consecutive lost nodes while already degraded, the connection auto-disables. This is the final rung. The tool has concluded that the problem is persistent and continuing to retry is wasteful. It goes silent.

The only way back from auto-disabled is:
- `awewarm config set <id> --on` — resets the failure counters, keeps schedule memory
- A manual `run <id>` that succeeds — same effect, but proven by an actual successful request

The ladder is not a punishment. It is a graduated response to uncertainty. One failure is a flicker. Three failures is a pattern. Six failures is a fact. The tool responds proportionally at each stage, and any success at any stage is a full reset.

### What Does Not Count as a Node

- **Manual `run <id>`** — never advances the ladder. Manual runs are exploratory, not scheduled.
- **Slots the machine slept through** — zero attempts = skip, not a lost node. The machine was off.
- **Catch-up retries within the failing state** — they are attempts to recover, not new failures.

The `status` command shows the current rung with full detail: `Health: failing -- 1/3 nodes lost, catch-up attempt 2/5`. The last failure is printed right under the last activation, with the error message intact.

## Account Connections: Claude Code and Codex, Zero Config

awewarm has built-in knowledge of two coding CLI tools.

### Claude Code

Claude Code's 5-hour session window is verified — it is documented provider behavior. awewarm detects it automatically:
- Checks macOS Keychain for `Claude Code-credentials`
- Falls back to `~/.claude/.credentials.json`
- No credentials are stored by awewarm. It reuses the existing login state.
- The CLI is resolved to an absolute path at send time (launchd has a minimal PATH).
- The warm-up request: `claude -p --model haiku "Reply with exactly: ok"` — one token in, one token out.

### Codex

Codex is detected from `~/.codex/auth.json`. Its window duration is **unknown** — not publicly documented — so it starts in fixed mode as unverified. The user must verify the window manually before switching to interval mode.

The warm-up request: `codex exec -m <model> "Reply with exactly: ok"`. The model defaults to the CLI default; you can override it with `--model`.

## Subscription Connections: Any Token Plan URL

Not everyone uses a CLI login. Many users are on third-party coding plans — GLM, DeepSeek, StepFun — that issue API keys and base URLs. awewarm supports these as subscription connections.

Three protocols are supported:
- **`openai-chat`** — OpenAI Chat Completions API (`/v1/chat/completions`)
- **`openai-responses`** — OpenAI Responses API (`/v1/responses`)
- **`anthropic-messages`** — Anthropic Messages API (`/v1/messages`)

You provide the base URL, the API key, and the model name. The key is stored in `~/.config/awewarm/secrets.json` (chmod 600) so the background scheduler can always read it — the scheduler runs as a system process with no access to your shell environment.

The `plan.url` field is stored in the connection config. For subscription connections, it defaults to the base URL. The `plan.label` is set to the connection label. This means you can have multiple plans — Claude Code account, Codex account, GLM token plan, DeepSeek token plan — all managed from the same config file, all running on the same scheduler.

The transport layer handles endpoint URL construction correctly for versioned bases (`/v1`, `/v4`, `/paas/v4`). It builds the request with `urllib` from the stdlib — no external HTTP dependencies. Timeout is 60 seconds. All secret-looking fields are redacted in output.

## The Architecture: Tick, Not Daemon

awewarm has no daemon. No persistent process. No memory footprint between ticks.

The system scheduler (launchd on macOS, Task Scheduler on Windows, systemd user timer on Linux) invokes `awewarm tick` once a minute. The tick process:
1. Loads config and state from disk
2. Runs pure schedule logic — no I/O in the scheduler module
3. Sends requests if due
4. Records outcomes
5. Saves state back to disk
6. Exits

This is the right architecture for a tool that runs once every few hours. A daemon would be a waste of memory. A tick is a stateless function from `(config, state, now) → (actions, new_state)`. It is testable, inspectable, and impossible to drift.

The scheduler module is pure — no I/O, no clock access except an injected `now` parameter. The transport module is pure builders. The CLI module is thin orchestration. The separation is clean: schedule logic, transport logic, and user interface are three independent layers.

### Wake-from-Sleep

On macOS, launchd `StartCalendarInterval` entries wake the machine from sleep — lid closed, deep sleep included — at the exact slot time. No sudo needed. One extra launchd job per fixed slot.

On Windows, one extra Task Scheduler task per fixed slot with *Wake to run* enabled, registered via PowerShell's `Register-ScheduledTask`. The per-minute tick itself never wakes the machine.

On Linux, the machine cannot be woken from suspend. Connections default to `wakeWhenAsleep: false`. Missed slots catch up within their catch-up windows once the machine wakes.

Per-connection `--no-wake` opts out of wake behavior entirely.

## The Stack: What the Skill Can Reach

The `awewarm` skill is intentionally small. It is a thin procedural layer over the `awewarm` CLI, with an intent router that maps natural language to commands.

| You say | The skill runs |
|---|---|
| "Show me awewarm status." | `awewarm status` |
| "Set Claude Code to warm at 06:35, 11:40, 16:45, 21:50 on weekdays." | `awewarm config set claude-code --times 06:35,11:40,16:45,21:50 --days weekday` |
| "Switch Claude Code to interval mode." | `awewarm config set claude-code --mode interval` (after verifying `--window 300`) |
| "Add my GLM coding plan." | interactive `awewarm config add` with protocol, base URL, API key, model |
| "Pause Codex warm-ups for the week." | `awewarm config set codex --off` |
| "Resume Codex and reset the health ladder." | `awewarm config set codex --on` |
| "Why did Claude Code stop warming?" | `awewarm status claude-code` — shows the health rung and last failure |
| "Fire Claude Code now, I want to test it." | `awewarm run claude-code` |
| "Change the catch-up window to 45 minutes." | `awewarm config settings --catchup-minutes 45` |

The `status` command is the one that saves the most time. Instead of digging through logs, you get one line per connection with the current health rung, the last activation time, and the next due time. If something is wrong, the failure message is right there. No grep. No journalctl. No wondering.

## Why It Matters

The first wave of subscription tools assumed you would manage your own schedule. Warm meant "remember to open Claude Code at 9 AM." Window meant "try not to take a long lunch." Most users tolerated it because they only had one subscription to manage.

The second wave assumes an agent operator. Warm is a schedule. Window is a configuration. The agent sets the times, installs the scheduler, verifies the setup, and monitors the health ladder. You never think about it again until `status` shows something unexpected.

awewarm has a second design constraint that makes it different from most cron tools: the health ladder is **graduated, not binary**. A single failure is not a crisis. Three failures is a pattern. Six failures is a fact. The tool responds proportionally at each stage, and **any success at any stage is a full reset**. This is the right behavior for a tool that operates unattended for weeks at a time. It is forgiving of transient failures and assertive about persistent ones.

The tick architecture is the third design decision. No daemon. No persistent process. No memory. The scheduler invokes a stateless tick once a minute. The tick loads state, computes actions, sends requests, saves state, and exits. This is simpler, more reliable, and more inspectable than any daemon could be. The state file is JSON. The config is JSON. The secrets are in a separate 0600 file. Everything is transparent.

This is the test I now apply to every warming tool I evaluate:

1. **Can an agent install it from a single prompt?**
2. **Does it handle failure gracefully across days of unattended operation?**
3. **Does it support both CLI logins and API key subscriptions?**

awewarm passes all three. The first prompt is the README. The second is the health ladder — four states, one recovery path, proportional response. The third is the transport layer: `claude-cli`, `codex-cli`, `openai-chat`, `openai-responses`, `anthropic-messages` — five transports, one config format.

The future of agent tooling is not "tools that work well with agents." It is "tools that the agent itself can install, configure, and operate on your behalf." awewarm is one of the first subscription tools to ship with that as the primary install path, not a workaround.

## Try It

Tell your agent:

> "Read https://github.com/mugpeng/awewarm/blob/main/README.ai.md and follow it."

Then check the status:

```bash
awewarm status
```

From there, the questions become ordinary:

- "Set Claude Code to warm every 5 hours starting at 06:35 on weekdays."
- "Add my Codex account."
- "Why did my GLM plan stop warming?"
- "Switch Claude Code to interval mode."
- "Pause all warm-ups for the weekend."

The agent already knows the commands. You just had not given it the README yet.

## More from mugpeng

awewarm is part of the aweteam ecosystem:

- **[aweskill](https://aweskill.webioinfo.top/)** — CLI-first skill package manager for 47+ AI coding agents
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — Agent profile switcher for Claude Code, Codex, and OpenCode; launches sessions with the right provider config
- **[awerouter](https://github.com/mugpeng/awerouter)** — A smart LLM router that automatically directs agent requests to fast, low-cost Flash models or more capable Pro providers using structural signals
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — AI coding session manager with profile-aware restoration
- **[aweshare](https://github.com/wehuman01/aweshare)** — An open-source, local-first AI capability relay: share your GPU and API keys without exposing the keys
- **[awewarm](https://github.com/mugpeng/awewarm)** — A subscription window warmer that keeps AI coding-plan windows predictably open with minimal scheduled requests