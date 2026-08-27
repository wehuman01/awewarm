# awewarm Community Hub: 10 Beta Spots, First Come First Served

![awewarm](../../../logo/hero2.webp)

awewarm keeps AI coding-plan subscription windows open by firing one minimal request at the right moment. On your own machine it even wakes the laptop from sleep at slot times — so far so good. What it can't survive is the machine being off: shut down for the night, shipped in a bag, left at home while you travel. The community hub closes that last gap: an always-on server fires the warm-ups instead, so your subscription windows stay warm around the clock — even when your computer is powered off.

Starting today, the hub at `https://awewarm.wehuman.top` is accepting its first test users: **10 spots, first come first served.**

## How to Apply

One email:

- **To**: [peng@wehuman.top](mailto:peng@wehuman.top)
- **Mention**: who you are, and which plan you want to keep warm (GLM token plan, Doubao, an Anthropic-compatible endpoint — any base URL + API key subscription)

That's the whole application. I'll reply with a one-time invite code (`awi_...`). When the 10 spots are gone, they're gone — later emails go on the waitlist for the next round.

## What a Tester Gets

- **Around-the-clock warming.** Fixed times run in your machine's timezone and travel with the push. Your laptop needs to be online only when you edit the schedule — edits re-push automatically.
- **The same awewarm you already know.** Five-minute setup: `awewarm config add` for the connection, `awewarm remote connect` to pair, `awewarm config set <id> --remote` to delegate. From then on the hub ticks for you.
- **An escape hatch that actually works.** Your config, schedules, and the master copy of your keys never leave your machine. `awewarm config set <id> --local` takes any connection back at any moment — local scheduling resumes where the server left off.

New to awewarm entirely? Let your agent do the setup:

> "Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm."

## The Trust Rule, Stated Up Front

The hub sends its warm-up requests with **your API key**, so the key's plaintext passes through the server's RAM. Using this service means trusting the operator (me, the project's developer) and the machine's root. What keeps the blast radius small:

- Your key **never touches the server's disk**. It lives in RAM only, is pushed by your machine over TLS, and is re-pushed automatically after any server restart.
- Everything else — config, schedules, the master copy of your keys — stays **on your machine**. Nothing is locked in.

If that trade doesn't suit you, run your own: `awewarm serve` on any box you control, or a private [awewarm-hub](https://github.com/wehuman01/awewarm-hub).

## What the Hub Can Warm

| Connection | Warmed by the hub? |
|---|---|
| Subscription endpoint (OpenAI Chat / OpenAI Responses / Anthropic-compatible, base URL + API key) | **yes** — this is the hub's job |
| Local CLI account (`claude` / `codex` login) | no — the login lives on your machine; awewarm warms it locally |

Local CLI account only? You don't need the hub — `awewarm init` handles local warming in one go.

## Honest FAQ

**No uptime guarantees.** The hub is run personally by me. If it stops, take your connections back with `awewarm config set <id> --local` — your config and keys never left your machine.

**Server restarted / "key missing" in status.** Harmless by design: your next local command re-claims the server and re-pushes your keys; a slot that came due meanwhile fires late inside its catch-up window, exactly like a laptop waking from sleep.

**Lost your token.** The server keeps tokens recoverable for exactly this case — ask, and reconnect with `awewarm remote connect https://awewarm.wehuman.top --token <it>`.

**Caps.** Each tester may keep a small number of delegated connections and pair a small number of machines. Hit a cap? Ask.

**Stop using it.** `awewarm config set <id> --local` for each delegated connection, then `awewarm remote disconnect`. Clean exit, no residue.

## Apply Now

10 spots. First come first served.

Email [peng@wehuman.top](mailto:peng@wehuman.top) — who you are, which plan to keep warm. Bugs go to [GitHub issues](https://github.com/wehuman01/awewarm/issues).

## More from mugpeng

awewarm is part of the aweteam ecosystem:

- **[aweskill](https://aweskill.webioinfo.top/)** — CLI-first skill package manager for 47+ AI coding agents
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — Agent profile switcher for Claude Code, Codex, and OpenCode
- **[awerouter](https://github.com/mugpeng/awerouter)** — Smart LLM router that directs requests to Flash or Pro models using structural signals
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — AI coding session manager with profile-aware restoration
- **[aweshare](https://github.com/wehuman01/aweshare)** — Local-first AI capability relay: share your GPU and API keys without exposing the keys
- **[awewarm](https://github.com/wehuman01/awewarm)** — Subscription window warmer that keeps AI coding-plan windows predictably open
