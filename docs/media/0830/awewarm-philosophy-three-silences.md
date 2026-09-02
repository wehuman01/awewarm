# awewarm: Not Every Missed Warm-up Is a Failure

![awewarm](../../../logo/hero.png)

Every subscription-warming tool answers the same question all day: *is this window still alive?*

Most answer by brute force: a resident process that forbids sleep, a timer that fires on schedule, retries until the heat death of the universe — or a bare error and move on. These answers share one flaw: they treat "didn't go out" as one thing. But a warm-up that didn't fire can mean wildly different things: the machine slept through the slot; the server restarted and lost the key; the endpoint is genuinely unreachable. The right next step differs in each case, and if you collapse them into one, your only options are mindless retrying or alarming the user.

awewarm makes the opposite bet: **name the silence.** A request that didn't go out is one of three things — a skip, a hold, or a failure — and only the third one counts as failure.

GitHub: [github.com/wehuman01/awewarm](https://github.com/wehuman01/awewarm)

## Three Kinds of Silence

**Skip: it wasn't needed.** The machine slept through the slot and zero attempts happened — that is not a malfunction, it's a missed opportunity. You closed the lid overnight, the 3 a.m. slot slipped by, and the health ladder doesn't move an inch: silence with zero attempts is no evidence against the connection's quality. The other skip is deliberate: you used the plan naturally a few minutes ago, the window is warm, and this heartbeat would be redundant — the goal is a live window, not a request count.

**Hold: the moment hasn't come.** The server restarted and lost the key while your machine happened to be offline — the slot is *held*, not failed, and the ladder doesn't move. When your machine comes back and the key re-pushes automatically, the slot fires inside its catch-up window exactly like a slept-through one does. Only past the window does it settle into a skip. A hold is the system saying: the problem is timing, not this connection.

**Fail: it went out and didn't come back.** Only this — genuinely sent, genuinely failed — enters the health ladder.

Most tools collapse the three. Collapsing fails in both directions: mistaking sleep for a dead endpoint makes the tool furiously retry a slot that stopped mattering the moment the machine woke; mistaking a dead endpoint for sleep lets it slumber on the ladder, never discovered. Name them separately and neither mistake can happen.

## The Catch-up Window: Wait Whenever Waiting Works

A slot was missed — what then? awewarm's default answer is not to give up but to wait, with a boundary.

The catch-up window defaults to thirty minutes. Inside it, a failed slot retries on a throttled cadence; if you come back, the key returns, the endpoint revives — the slot completes like any punctual one, indistinguishable in the record. Past the boundary, the activation settles into a skip and the book closes cleanly. There is a deliberate kind of waiting too: `--start 16:05` pushes today's 16:00 slot back wholesale — no request fires before the gate, and the moment it lifts, the held slot completes.

The asymmetry here deserves saying out loud. Reporting success as failure is *loud* — the user sees red, asks, and it clears up quickly. Sitting on a failure is *bad* — the window quietly goes cold, and by discovery time it has expired. awewarm prefers the loud one. But preferring loud is not the same as being eager: before the evidence is in, the default action is to wait, not to alarm.

## The Health Ladder: Failures Escalate Slowly, One Success Clears It All

Failures that do enter the ladder escalate with proportion, never in one step. Here is the whole ladder:

```text
connected (normal)
   │
   │  lose 1 node — genuinely sent, genuinely failed
   ▼
failing (catching up)
   │      retries inside the catch-up window: 30 min default, 5 attempts max
   │      any single success → straight back to connected
   │
   │  3 consecutive lost nodes
   ▼
degraded (degraded)
   │      no more catch-up: one shot per node
   │      interval probes once per window, fixed slots fire exactly once
   │
   │  3 more consecutive lost nodes
   ▼
auto-disabled
          fully silent, until you turn it back on with --on,
          or a manual run succeeds
          —— ladder resets, schedule memory stays, the original plan resumes

From any rung, at any moment, one success: back to connected.
Two things that never count:
  · a slot slept through (zero attempts) is a skip — the ladder doesn't move
  · manual runs and verify requests are never counted as nodes
```

One lost node puts a connection in `failing`: catch-up retries within the window, and any single success returns it to `connected`. Three consecutive lost nodes downgrade it to `degraded`: no more catch-up, one shot per node — the retry budget is traded for quiet. Three more while degraded reach `auto-disabled`: fully silent until you turn it back on, or a manual `run` succeeds — the ladder resets, the schedule memory stays, and recovery day one runs the original plan.

Three design decisions here run against convention. Escalation counts *nodes*, not attempts — one failed attempt is noise; a node lost from catch-up start to window end is evidence. Manual runs and verify requests never count as nodes — your own knock on the door is not evidence against the system. And any single success clears everything — failure accumulates but holds no grudge: a connection that has failed a hundred times is `connected` the moment this one succeeds.

## A Heartbeat, Not a Burn

The warm-up request itself obeys minimal intervention. A heartbeat is tiny, `maxTokens` strips the reply to the bone — its purpose is to sustain the window, not to consume the plan. The opposite extreme is a machine that never sleeps: the old approach pins a laptop awake with a prevent-sleep assertion, losing a whole night's sleep for two midnight heartbeats. awewarm lets the machine sleep and, at the moment it's needed, pulls the Mac out of sleep with a one-shot RTC wake — a screen-off dark wake, heartbeat delivered in seconds, back to sleep. It doesn't prevent sleep; it passes through it. Windows gets the same coverage with one-shot scheduled tasks.

## Where the Keys Live, Said Out Loud

Honesty doesn't stop at failure bookkeeping — it extends to where the keys live.

A delegated connection's key lives in the server's RAM only, never on disk — lost on restart, re-pushed automatically when your machine returns. That is the default, and the recommended one. For machines that come online a few times a month there is an option to write the key onto the server's disk — shipped off by default, confirming at every step, gated by the hub operator, with `status` always labeling where the key currently lives. The backup command's archive holds plaintext keys, and the command itself says so. When the server is offline, `status` shows you the last snapshot *labeled stale*, not one pretending to be fresh.

The tool does not pretend to know more than it can see. It can't see your machine's power state, so it calls that "missed"; it holds stale data, so it prints "stale" in the heading.

## One Sentence to Sum It Up

**A warm-up that didn't fire is, the vast majority of the time, not a failure — it's a skip or a hold. Name them apart from real failure, and waiting gains its justification, escalation keeps its proportion, and one single success wipes the entire slate clean.**

Three kinds of silence. Thirty minutes of patience. A ladder that escalates slowly. A heartbeat kept minimal. And the whereabouts of your keys, always stated. No resident insomniac process, no mindless retries, no silent deaths. Just one ledger, kept honestly.

## Try It

### Let the agent install it

In Claude Code, Codex, or any coding agent, say:

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

### Or do it yourself

```bash
pip install awewarm

# Two commands to start warming
awewarm init

# Check each connection's state and health ladder
awewarm status
```

## Apply Now

Don't want to run your own server? The community hub at [awewarm.wehuman.top](https://awewarm.wehuman.top) is still taking test users: 10 spots, first come first served.

Email [peng@wehuman.top](mailto:peng@wehuman.top) — who you are, which plan to keep warm. Bugs go to [GitHub issues](https://github.com/wehuman01/awewarm/issues).

## More from the awewarm Series

- [awewarm：牛来，让你的ai订阅时刻热起来](https://mp.weixin.qq.com/s/HYAzfUPF_PUEfio4nZs1KA)

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
