# awewarm: I Let My Agent Keep My Subscription Window Warm

![awewarm](../../../logo/hero2.webp)

AI coding plans all have usage windows. Claude Max gives you 5 hours from the first request. Codex and third-party token plans work the same way. You start at 9 AM, the window closes at 2 PM. You take a lunch break, come back at 3 PM, and your first request opens a brand new window — burning quota on a partial session. awewarm fixes this by sending one minimal request at the right time so the window is always already open when you sit down to code.

The install is a single prompt: "Read https://github.com/mugpeng/awewarm/blob/main/README.ai.md and follow it." The agent installs the package, registers the scheduler with launchd, discovers your Claude Code login from Keychain and Codex from `~/.codex/auth.json`, sets up a fixed-time grid, and adds any subscription connections. Two minutes later, `awewarm status` shows everything green. The learning cost of the tool moves from you to the agent.

GitHub: [github.com/mugpeng/awewarm](https://github.com/mugpeng/awewarm)

## Two Modes: Fixed and Interval

### Fixed Mode: Absolute Times, Always Safe

Fixed mode is the default. You give it a list of local times (`--times 06:35,11:40,16:45,21:50`) and days (`weekday` or `every-day`). Each slot opens a fresh window. If the machine was asleep, the slot fires late within a catch-up window (default 30 min); past that, it is skipped. A slot within 30 min of a previous success is also skipped to prevent double-fires.

When the window duration is known (Claude Code is a verified 5 hours), awewarm asks for the daily quota reset time and computes a full-day grid: one slot per window, spaced `window + 5 min` apart. For 5 hours starting at 01:14, you get `01:14, 06:19, 11:24, 16:29, 21:34`. Unverified plans — like Codex — start in fixed mode and switch to interval once the window is confirmed.

### Interval Mode: Rolling Renewal

Interval mode chains windows. After each success, the next request is scheduled `window + grace + jitter` later (default: 300 min + 75 s + up to 30 s). The grace runs **after** the old window closes — firing early lands inside the old window and starts nothing. With no success yet, one request fires immediately as the first anchor (`--start HH:MM` defers it). Manual `run` never shifts the chain unless `--reset-due` is used. Interval mode is **locked** until the window is verified with `--window` — wrong-duration chaining is worse than fixed mode.

## The Health Ladder: Four States, One Recovery Path

Most cron tools have two states: working or broken. awewarm has four:

```
connected ──首次节点失败──▶ failing ──连续N个节点丢失──▶ degraded ──再连续N个节点丢失──▶ auto-disabled
   ▲                          │                              │                              │
   └────────── 任一次成功（节点尝试/catch-up重试/手动run）──────┘                              │
                                                               └──── 只有 --on 或手动 run 成功 ──┘
```

- **Connected** — normal operation. Slots fire on time, chains renew on schedule.
- **Failing** — one node failed. Catch-up retries: 5 attempts within 30 minutes, spaced 5 min apart. Any success resets the ladder.
- **Degraded** — `degradeAfterNodes` (default 3) consecutive nodes lost. Catch-up stops. Each node gets one attempt. Success resets the ladder.
- **Auto-disabled** — another 3 consecutive nodes lost while degraded. Goes silent. Only `--on` or a successful manual `run` restores it.

What does not count as a node: manual runs, slots the machine slept through, catch-up retries within failing. `awewarm status` shows the current rung: `Health: failing -- 1/3 nodes lost, catch-up attempt 2/5`.

## Account and Subscription Connections

awewarm supports both CLI logins and API key subscriptions — five transports, one config.

**Claude Code** — detected from macOS Keychain or `~/.claude/.credentials.json`. 5-hour window is verified. No credentials stored; awewarm reuses existing login state. Warm-up: `claude -p --model haiku "Reply with exactly: ok"`.

**Codex** — detected from `~/.codex/auth.json`. Window duration is unknown, starts in fixed mode. Warm-up: `codex exec -m <model> "Reply with exactly: ok"`.

**Subscription plans** — any OpenAI Chat / Responses / Anthropic-compatible endpoint with a base URL + API key. Protocols: `openai-chat`, `openai-responses`, `anthropic-messages`. The key is stored in `secrets.json` (0600) so the background scheduler can read it. The `plan.url` field is stored in the connection config. Claude Code account, Codex account, GLM token plan, DeepSeek token plan — all managed from the same config, all running on the same scheduler.

## The Architecture: Tick, Not Daemon

No daemon. No persistent process. The system scheduler (launchd / Task Scheduler / systemd timer) invokes `awewarm tick` once a minute. The tick loads config and state, computes actions, sends requests if due, records outcomes, saves state, and exits. Pure function from `(config, state, now) → (actions, new_state)` — testable, inspectable, impossible to drift.

On macOS, launchd wakes the machine from sleep at slot times (no sudo). On Windows, extra Task Scheduler tasks with *Wake to run*. On Linux, missed slots catch up within the catch-up window after wake. `--no-wake` opts out per connection.

## The Stack: What the Skill Can Reach

| You say | The skill runs |
|---|---|
| "Show me awewarm status." | `awewarm status` |
| "Set Claude Code to warm at 06:35, 11:40, 16:45, 21:50 on weekdays." | `awewarm config set claude-code --times 06:35,11:40,16:45,21:50 --days weekday` |
| "Switch Claude Code to interval mode." | `awewarm config set claude-code --mode interval` (after `--window 300`) |
| "Add my GLM coding plan." | interactive `awewarm config add` |
| "Pause Codex warm-ups for the week." | `awewarm config set codex --off` |
| "Resume Codex and reset the health ladder." | `awewarm config set codex --on` |
| "Why did Claude Code stop warming?" | `awewarm status claude-code` — shows the health rung and last failure |
| "Fire Claude Code now, I want to test it." | `awewarm run claude-code` |
| "Change the catch-up window to 45 minutes." | `awewarm config settings --catchup-minutes 45` |

## Why It Matters

The first wave of subscription tools assumed you would manage your own schedule. The second wave assumes an agent operator. The agent sets the times, installs the scheduler, and monitors the health ladder. You never think about it until `status` shows something unexpected.

Three design decisions set awewarm apart. The health ladder is **graduated, not binary** — one failure is a flicker, three is a pattern, six is a fact. Any success resets the ladder. The tick architecture has **no daemon** — stateless, transparent, JSON state on disk. The transport layer is **unified** — five transports (`claude-cli`, `codex-cli`, `openai-chat`, `openai-responses`, `anthropic-messages`), one config format.

The future of agent tooling is not "tools that work well with agents." It is "tools that the agent itself can install, configure, and operate on your behalf."

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
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — Agent profile switcher for Claude Code, Codex, and OpenCode
- **[awerouter](https://github.com/mugpeng/awerouter)** — Smart LLM router that directs requests to Flash or Pro models using structural signals
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — AI coding session manager with profile-aware restoration
- **[aweshare](https://github.com/wehuman01/aweshare)** — Local-first AI capability relay: share your GPU and API keys without exposing the keys
- **[awewarm](https://github.com/mugpeng/awewarm)** — Subscription window warmer that keeps AI coding-plan windows predictably open