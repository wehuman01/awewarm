# awewarm: I Let My Agent Keep My Subscription Window Warm

![awewarm](../../../logo/hero2.webp)

AI coding plans all have usage windows. Claude Max gives you 5 hours from the first request. Codex and third-party token plans have windows of their own. You start at 9 AM, the window closes at 2 PM. You take a lunch break, come back at 3 PM, and your first request opens a brand new window — burning quota on a partial session. awewarm fixes this by sending one minimal request at the right time so the window is always already open when you sit down to code.

The install is a single prompt: "Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it." The agent installs the package and the awewarm skill, then walks you through `awewarm init` in your terminal: it discovers your Claude Code login (macOS Keychain or `~/.claude/.credentials.json`) and your Codex login (`~/.codex/auth.json`), offers a full-day fixed-time grid when the window is known, adds subscription endpoints through `awewarm config add`, and registers the background scheduler (launchd on macOS, Task Scheduler on Windows, systemd on Linux). Two minutes later, `awewarm status` shows every connection `connected`. The learning cost of the tool moves from you to the agent.

GitHub: [github.com/wehuman01/awewarm](https://github.com/wehuman01/awewarm)

## Two Modes: Fixed and Interval

### Fixed Mode: Absolute Times, Always Safe

Fixed mode is the default. You give it a list of local times (`--times 06:35,11:40,16:45,21:50`) and days (`weekday` or `every-day`). Each slot opens a fresh window. If the machine was asleep, the slot fires late within a catch-up window (default 30 min); past that, it is skipped. A slot within 30 min of a previous success is also skipped to prevent double-fires.

When the window duration is known (Claude Code is a verified 5 hours), awewarm asks for the daily quota reset time and computes a full-day grid: one slot per window, spaced `window + 5 min` apart. For 5 hours starting at 01:14, you get `01:14, 06:19, 11:24, 16:29, 21:34`. Unverified plans — like Codex — start in fixed mode and switch to interval once the window is confirmed.

### Interval Mode: Rolling Renewal

Interval mode chains windows. After each success, the next request is scheduled `window + grace + jitter` later (default: 300 min + 75 s + up to 30 s). The grace runs **after** the old window closes — firing early lands inside the old window and starts nothing. With no success yet, one request fires immediately as the first anchor (`--start HH:MM` defers it). Manual `run` never shifts the chain unless `--reset-due` is used. Interval mode is **locked** until the window is verified with `--window` — wrong-duration chaining is worse than fixed mode.

## The Health Ladder: Four States, One Recovery Path

Most cron tools have two states: working or broken. awewarm has four:

![health-ladder](../../media/images/health-ladder.png)

- **Connected** — normal operation. Slots fire on time, chains renew on schedule.
- **Failing** — one node failed. Catch-up retries: 5 attempts within 30 minutes, spaced 5 min apart. Any success resets the ladder.
- **Degraded** — `degradeAfterNodes` (default 3) consecutive nodes lost. Catch-up stops. Each node gets one attempt. Success resets the ladder.
- **Auto-disabled** — another 3 consecutive nodes lost while degraded. Goes silent. Only `--on` or a successful manual `run` restores it.

What does not count as a node: manual runs, slots the machine slept through, catch-up retries within failing. `awewarm status` shows the current rung: `Health: failing — 1/3 nodes lost, catch-up attempt 2/5`.

## Account and Subscription Connections

awewarm supports both CLI logins and API key subscriptions — five transports, one config.

**Claude Code** — detected from macOS Keychain or `~/.claude/.credentials.json`. 5-hour window is verified. No credentials stored; awewarm reuses existing login state. Warm-up: `claude -p --model haiku "Reply with exactly: ok"`.

**Codex** — detected from `~/.codex/auth.json`. Window duration is unknown, starts in fixed mode. Warm-up: `codex exec --skip-git-repo-check "Reply with exactly: ok"` (plus `-m <model>` when one is configured).

**Subscription plans** — any OpenAI Chat / Responses / Anthropic-compatible endpoint with a base URL + API key. Protocols: `openai-chat`, `openai-responses`, `anthropic-messages`. The key is stored in `secrets.json` (0600) so the background scheduler can read it. The base URL is stored in the connection's `url` field. Claude Code account, Codex account, GLM token plan, Doubao token plan — all managed from the same config, all running on the same scheduler.

## The Architecture: Tick, Not Daemon

No daemon. No persistent process. The system scheduler (launchd / Task Scheduler / systemd timer) invokes `awewarm tick` once a minute. The tick loads config and state, computes actions, sends requests if due, records outcomes, saves state, and exits. Pure function from `(config, state, now) → (actions, new_state)` — testable, inspectable, impossible to drift.

On macOS, launchd fires the tick at the exact slot time while the machine is awake (no sudo); waking a lid-closed sleeping Mac takes `awewarm scheduler install --wake` — one sudo for a wake-only grant. On Windows, extra Task Scheduler tasks with *Wake to run*. On Linux, a suspended machine cannot be woken; missed slots catch up within the catch-up window after wake. Wake is opt-in per connection (`--wake`; off by default).

## The Stack: What the Skill Can Reach

| You say | The skill runs |
|---|---|
| "Show me awewarm status." | `awewarm status` |
| "Set Claude Code to warm at 06:35, 11:40, 16:45, 21:50 on weekdays." | `awewarm config set claude-code --times 06:35,11:40,16:45,21:50 --days weekday` |
| "Switch Claude Code to interval mode." | `awewarm config set claude-code --mode interval` — its 5-hour window is already verified |
| "Add my GLM coding plan." | interactive `awewarm config add` |
| "Pause Codex warm-ups for the week." | `awewarm config set codex --off` |
| "Resume Codex and reset the health ladder." | `awewarm config set codex --on` |
| "Why did Claude Code stop warming?" | `awewarm status claude-code` — shows the health rung and last failure |
| "Fire Claude Code now, I want to test it." | `awewarm run claude-code` |
| "Change the catch-up window to 45 minutes." | `awewarm config settings --catchup-minutes 45` |

## Why It Matters

Every subscription is getting worse. Token plans shrink limits while raising costs. The math is simple: if you pay for a 5-hour window and only use 3 hours of it, you are leaving 40% of your money on the table. A cold start at 3 PM when you already opened a window at 9 AM is not just inconvenient — it is a direct loss of paid quota.

awewarm is a yield optimizer for your subscription. It ensures every window you pay for is fully available when you need it. No cold starts. No partial windows. No wasted quota. The marginal cost of one warm-up request per window is negligible. The marginal gain of a full 5-hour window versus a 90-minute fragment is the entire subscription.

Three design decisions make it durable. The health ladder is **graduated, not binary** — one failure is a flicker, three is a pattern, six is a fact. Any success resets the ladder. The tick architecture has **no daemon** — stateless, transparent, JSON state on disk. The transport layer is **unified** — five transports, one config format. The agent installs it, the scheduler runs it, and your subscription works at full capacity.

## Try It

Tell your agent:

> "Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it."

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

## Awesome Ecosystem

aweshare is part of a growing family of "awesome" tools — CLI-first, local-first, and operable by AI agents.

### CLI Tools

- **[aweskill](https://aweskill.webioinfo.top/)** — CLI-first skill package manager supporting 47+ AI coding agents.
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — Agent profile switcher for Claude Code, Codex, and OpenCode.
- **[awerouter](https://github.com/mugpeng/awerouter)** — Smart router that splits requests between Flash and Pro models using structural signals, cutting unnecessary model spend.
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — Bookmark, categorize, and restore AI coding sessions; pairs with aweswitch to save profiles and launch with one command.
- **[aweshare](https://github.com/wehuman01/aweshare)** — Share local Ollama/vLLM backends, domestic coding plans, or authorized OpenAI/Anthropic subscriptions through a self-hosted hub — a sharing economy for tokens.
- **[awewarm](https://github.com/wehuman01/awewarm)** — Subscription window warmer that keeps AI coding-plan windows active, for local setups and through a remote hub server.
- **[awescholar](https://github.com/Webioinfo01/awescholar)** — AI-agent-operable scientific literature discovery and curation.

### Desktop Apps

- **[awedot](https://awedot.wehuman.top/)** — A floating orb at your screen edge keeps track of the current AI session: bookmark it in one click, resume anytime, and pair with aweswitch to pin the agent's config (e.g., relaunch with the GLM model).

### Project Collections

- **[Awesome AI Meets Biology](https://github.com/Webioinfo01/Awesome-AI-Meets-Biology)** — A curated survey of AI applications in biology, bioinformatics, and biomedical research. Powered by awescholar.
- **[Awesome AI Virtual Tumor](https://github.com/Webioinfo01/Awesome-AI-Virtual-Tumor)** — A curated collection of state-of-the-art AI systems for virtual tumor modeling and simulation: static models, dynamic models, agents, benchmarks, and reviews.