# Community Hub — subscription warmth without your own server

<strong>English</strong> · <a href="./README_cn.md">简体中文</a>

`https://awewarm.wehuman.top` is a community [awewarm-hub](https://github.com/wehuman01/awewarm-hub) instance run by awewarm's developer. awewarm keeps AI coding-plan subscription windows open by firing one minimal request at the right moment — normally on a schedule from your own machine, which then has to be awake for it. This hub fires them from an always-on server instead: your laptop can be closed, off, or across the world, and your subscription windows stay warm around the clock.

Pairing is invite-based — see [Contact](#contact) for how to get a code.

## Read this first — the trust rule

The hub sends its warm-up requests with **your API key**, so the key's plaintext passes through the server's RAM. Using this service means trusting the operator (the project's developer) and the machine's root. A server run by someone you don't trust is not a place for your keys — in that case run your own: [`awewarm serve`](../../README.md#remote-server--your-own-box-or-a-shared-hub) on any box you control, or your own private awewarm-hub.

What keeps the blast radius small, stated honestly:

- Your key **never touches the server's disk**. It lives in RAM only, is pushed by your machine over TLS, and is re-pushed automatically after any server restart.
- Everything else — your config, your schedules, the master copy of your keys — stays **on your machine**. You can take any connection back with `awewarm config set <id> --local` at any moment; nothing is locked in.

## What the hub can warm

| Connection | Example | Warmed by the hub? |
| --- | --- | --- |
| Subscription endpoint | any OpenAI Chat / OpenAI Responses / Anthropic-compatible API (base URL + API key) | **yes** — this is the hub's job |
| Local CLI account | your local `claude` / `codex` login | **no** — the login lives on your machine, so awewarm warms it locally with its background scheduler |

If a local CLI account is all you have, you don't need the hub: `awewarm init` sets up local warming in one go. The rest of this guide is about the first kind.

## Quick start

The interactive steps (`config add`) belong in your own terminal; everything else your AI agent can run for you (see the repo's [README.ai.md](https://github.com/wehuman01/awewarm/blob/main/README.ai.md)).

### 1. Install

Python ≥ 3.9:

```bash
pip3 install awewarm
```

Working in Claude Code, Codex, or another coding agent? Let it do the setup — tell it:

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

That installs the CLI **and the `awewarm` skill** — afterwards you operate awewarm by just asking ("when is the next warm-up?", "delegate glm to the community hub"). The agent also runs every read-only step of this guide for you; only the interactive `config add` below stays in your terminal.

### 2. Add your first connection

`awewarm config add` walks you through it:

```bash
awewarm config add
```

- **A subscription endpoint** — pick "Subscription endpoint", choose the protocol (OpenAI Chat / OpenAI Responses / Anthropic Messages), then enter the API base URL, API key, and model. One minimal test request fires immediately, so a bad key or model surfaces right there instead of at 6 a.m. The key is stored in `~/.config/awewarm/secrets.json` (chmod 600) — never in config.json, and never on the hub's disk.
- **A local account** — pick a detected `claude` / `codex` login (the same command re-adds one you removed earlier). It warms locally and cannot be delegated.

`awewarm status` then lists your connections by id (derived from the name you entered — e.g. `glm`); that id is what the commands below refer to.

### 3. Pair with the hub

```bash
awewarm remote connect https://awewarm.wehuman.top --invite awi_...
```

The invite code is one-time and time-limited (typically 7 d). Pairing prints your personal token once and saves it to secrets.json — **keep a copy**: it is the only way back into your account without asking for a fresh invite.

### 4. Delegate the subscription

```bash
awewarm config set glm --remote     # your connection id
awewarm status --remote             # server health line + delegated connections
```

From now on the hub fires the warm-ups on schedule, around the clock. Fixed times run in **your machine's timezone** — it travels with the push. Your laptop needs to be online only when you edit the schedule: edits re-push automatically, and edits made while offline sync on the next `awewarm remote push`.

### 5. No local scheduler needed — unless you keep local connections

With everything delegated, the hub ticks for you; `awewarm scheduler install` is unnecessary (it even asks before installing on such a machine). Keeping a local `claude` / `codex` account warm as well? Install the scheduler for that one.

## FAQ

**I lost my token.** Ask the operator — the server keeps tokens recoverable for exactly this case. Reconnect with:

```bash
awewarm remote connect https://awewarm.wehuman.top --token <it>
```

— same account, same connections.

**The server restarted / status shows "key missing".** Harmless by design: the first local command (or `awewarm remote push`) re-claims the server and re-pushes your keys; a slot that came due meanwhile fires late inside its catch-up window, exactly like a laptop waking from sleep.

**Caps.** Each user may keep a small number of delegated connections and pair a small number of machines (often just one). If you hit a cap, the 403 error names the exact number — ask the operator to raise it or to mint a new invite.

**What if the service goes away?** It is run personally by the developer with no uptime guarantees. If it stops, take your connections back (`awewarm config set <id> --local`) — your config and keys never left your machine, so local scheduling resumes where the server left off.

**Stop using it.**

```bash
awewarm config set glm --local      # take each delegated connection back first
awewarm remote disconnect           # then forget the server
```

## Contact

- **Invite code**: email **peng@wehuman.top** — mention who you are and which plan you want to keep warm.
- **awewarm bugs**: [GitHub issues](https://github.com/wehuman01/awewarm/issues).
