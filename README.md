<div align="center">
  <img src="logo/hero2.webp" alt="awewarm" width="860">
  <h1>awewarm: Subscription Window Warmer</h1>
  <p><strong>Keep AI coding-plan windows warm with one minimal request.</strong></p>
  <p>Connect once; awewarm detects what your Claude Code / Codex account or subscription endpoint can do, then makes sure the next usage window is always already open.</p>
  <p>
    <strong>English</strong> ·
    <a href="./README_cn.md">简体中文</a>
  </p>
  <p>
    <a href="https://ko-fi.com/mugpeng"><img src="https://img.shields.io/badge/Ko--fi-Buy%20me%20a%20coffee-FF5E5B?style=flat-square&logo=ko-fi&logoColor=white" alt="Ko-fi"></a>
  </p>
  <p>
    <img src="https://img.shields.io/pypi/v/awewarm?style=flat-square&color=7C3AED" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A5%203.9-0EA5E9?style=flat-square" alt="Python">
    <img src="https://img.shields.io/badge/license-MPL--2.0-22C55E?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip install">
    <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-334155?style=flat-square" alt="Platform">
    <img src="https://img.shields.io/pepy/dt/awewarm?style=flat-square" alt="PyPI downloads">
    <img src="https://img.shields.io/github/stars/wehuman01/awewarm?style=flat-square" alt="GitHub stars">
  </p>
</div>

> Real case: you start work at 9:00 and normally get only one 5-hour quota for the morning. With awewarm warming at 05:30, the window refreshes again at 10:30 — twice the morning quota, without you touching anything.

awewarm manages two kinds of connections:

- **Account** — your local `claude` / `codex` CLI logins. awewarm reuses their login state and sends one minimal headless request (`Reply with exactly: ok`). No credentials are stored.
- **Subscription plan** — any OpenAI Chat / OpenAI Responses / Anthropic-compatible endpoint with a base URL + API key. The key is stored in `~/.config/awewarm/secrets.json` (chmod 600) so the background scheduler can always read it.

It schedules those requests in two modes — `fixed` and `interval` — explained in [Scheduling Modes](#scheduling-modes) below. Interval-style renewal stays locked until the window semantics are verified or user-confirmed; `fixed` is always safe.

## Install

Requires Python ≥ 3.9:

```bash
pip3 install awewarm
```

The background scheduler installs on macOS (launchd), Windows (Task Scheduler), and Linux (systemd user timer — `loginctl enable-linger $USER` first on headless/SSH accounts). Where systemd is unavailable, cron the tick: `* * * * * awewarm tick`.

All keys live in `secrets.json` — env-var references were removed because the background scheduler (launchd/systemd/Task Scheduler) cannot read shell variables and would silently fail with "API key unavailable". Writes are atomic; a malformed or unreadable secrets file is refused rather than silently replaced.

## Quick Start

### Let an AI agent set it up

Working in Claude Code, Codex, or another coding agent? Tell it:

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

The agent installs the CLI, scans your local accounts (read-only), and tunes schedules on request. Onboarding itself (`awewarm init`, `awewarm config add`) stays in your terminal — it prompts for choices and API keys. After setup you can ask things like "when is the next warm-up?" or "set claude-code to 06:35 and 12:35".

### Manual setup

```bash
awewarm init        # scan local accounts, pick a schedule, install the scheduler
awewarm status      # see what will happen next
```

Every added connection — account or endpoint — gets one test request during setup, so a broken model or bad key surfaces immediately instead of at 6 a.m.

For a subscription endpoint instead:

```bash
awewarm config add
```

You will be asked for the protocol, API base URL, API key, and model; awewarm tests the endpoint with one minimal request, then stores the key in `secrets.json`. The same command also re-adds a local `claude` / `codex` account you removed earlier — it lists whatever is detected on this machine.

## Companion Tools

awewarm is part of a small tool family for AI coding agents:

- **[awewarm-hub](https://github.com/wehuman01/awewarm-hub)** — the multi-tenant companion server: one always-on box keeps a whole team's windows warm through one-time invites. Same org, same MPL-2.0; its engine is this package, pinned to its minor version.
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — agent profile switcher for Claude Code, Codex, and OpenCode. aweswitch manages which provider a session launches with; awewarm keeps that provider's subscription window open underneath. If you launch coding-plan profiles with aweswitch, awewarm is the piece that keeps those 5-hour windows from going cold overnight.
- **[aweskill](https://github.com/Webioinfo01/aweskill)** — CLI skill package manager for AI agents (47+ agents).
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — session bookmark manager for Claude Code and Codex.
- **[awerouter](https://github.com/mugpeng/awerouter)** — smart LLM router: flash/pro split by structural signals.

## Scheduling Modes

Both modes send the same one minimal request — what differs is *when* it fires. Switch with `awewarm config set <id> --mode fixed|interval`; see the current mode and next due moment with `awewarm status`. The old `hybrid` mode was removed — a fixed grid spaced one window apart already keeps windows chained all day, with calendar wake coverage interval cannot offer.

| Mode | Fires when | Needs a verified window | Best for |
| --- | --- | --- | --- |
| `fixed` | fixed times each day | no | predictable hours; unverified plans; sleeping Macs |
| `interval` | window + grace after each success | yes | 24/7 warmth on an always-on machine |

### `fixed` — absolute times, always safe

One request at each fixed local time (`weekday` or `every-day`); each hit opens a fresh window.

- If the machine was asleep at the slot time, the slot still fires late within the catch-up window (default 30 min); past that it is recorded as skipped.
- A slot landing within 30 min of a previous success is skipped — never pay for two windows at once.
- `--start HH:MM` is a one-time gate that shifts today's schedule: no slot fires before that moment, and a held slot fires right after the gate lifts while still inside its catch-up window (`--start 16:05` turns today's 16:00 slot into 16:05) — the times list itself is untouched. A gate past a slot's catch-up end skips that slot; the gate clears on the first success.
- The only mode that works while window semantics are unknown, which is why unverified plans start here.
- During setup, when the window duration is known, awewarm asks for the plan's daily quota reset time and offers a full-day grid anchored on it — one slot per window, spaced window + 5 min apart (e.g. reset 01:14 + a 5 h window → 01:14, 06:19, 11:24, 16:29, 21:34). Declining keeps just the time you entered. Plans added in fixed mode are asked for the window duration first (default 300) — it spaces the grid and is recorded as a user-confirmed window that unlocks interval mode.

```bash
awewarm config set claude-code --times 06:35,11:40,16:45   # 5 h + 5 min apart: windows chain across a workday
awewarm config set claude-code --mode fixed
```

**Example** — a laptop that sleeps at night: slots at 06:35 / 11:40 / 16:45 keep a window open from 06:35 to ~21:45 every weekday. The machine only needs to be awake within 30 min of each slot.

### `interval` — rolling renewal

After each success the next request is scheduled `window + grace` later (default 300 min + 75 s, plus up to 30 s jitter). The grace runs *after* the old window has closed — firing earlier would land inside the old window and start nothing. With no success recorded yet, one request fires immediately as the first anchor — unless you defer that start with `--start HH:MM`: no request fires before that moment (today, or tomorrow if it has passed), the first tick after it opens the chain, and the gate clears on the first success. The same gate also works in fixed mode (see above).

```bash
awewarm run my-plan                        # 1. one minimal request, timestamped
# ...watch when the plan's quota resets, note the elapsed minutes...
awewarm config set my-plan --window 300    # 2. record the window (unlocks interval)
awewarm config set my-plan --mode interval # 3. rolling renewal
```

A manual `run <id>` never shifts the renewal chain — the next due moment stays as scheduled. Add `--reset-due` to restart the chain from this run instead.

**Example** — an always-on machine you want warm around the clock, nights and weekends included: no wake machinery needed, renewal just keeps rolling.

### Quick Templates

Common scheduling patterns to get started:

```bash
# Standard workday (morning + afternoon)
awewarm config set <id> --times 06:00,11:05,16:10

# With evening overtime
awewarm config set <id> --times 06:00,11:05,16:10,21:15

# Weekday only
awewarm config set <id> --times 08:00,13:05 --days weekday

# Interval (verified 5h window)
awewarm config set <id> --mode interval --window 300 --anchor 11:05
```

All fixed-time slots above are 5 h 5 min apart — the subscription window (5 h) plus a 5 min buffer for scheduling imprecision. Each slot fires once and opens a fresh window. With four slots (`06:00, 11:05, 16:10, 21:15`) you get coverage across the whole day: morning, afternoon, evening, and late night for overtime. Three slots (`06:00, 11:05, 16:10`) cover a standard workday. `--days weekday` limits fires to weekdays. Two-slot chains work well for half-day or intermittent use.

### When requests fail — the health ladder

Both modes share one ladder: `connected → failing → degraded → auto-disabled`.

```

connected ──first failure──▶ failing ──N consecutive lost──▶ degraded ──N more lost──▶ auto-disabled
   ▲                          │                              │                              │
   └──────── any success (node/catch-up/manual run) ──────┘                              │
                                                                                             │
                                                                          └────── --on / run ──┘
```

- A failed node (a fixed slot, or an interval renewal moment) enters **failing** and gets catch-up retries — by default 5 attempts within 30 minutes, spaced ~5 minutes apart (defaults via `awewarm config settings`; one connection via `--catchup-attempts` / `--catchup-minutes`).
- 3 consecutive lost nodes (default 3, via `awewarm config settings --degrade-after-nodes` or a per-connection `--degrade-after-nodes`) drop the connection to **degraded**: single shot per node, no more catch-up. interval probes once per window; fixed fires each slot exactly once.
- The same count again while degraded stops it entirely: **auto-disabled**, silent until you resume with `awewarm config set <id> --on` (or a manual `run <id>` that succeeds).
- Any success — node attempt, catch-up retry, manual run — resets the whole ladder. Manual attempts never count as nodes, and a slot the machine slept through (zero attempts) is not a lost node.
- `status` shows the rung plus details (`Health: failing — 1/3 nodes lost, catch-up attempt 2/5`), and prints the last failure with its error right under the last activation.

### Sleeping Macs — calendar fire + RTC wakes (macOS)

Two layers, honestly split by what each can do:

- **Calendar fire (default, no sudo).** `scheduler install` writes one `StartCalendarInterval` entry per fixed slot into the launchd agent. Those entries run the tick at the exact slot time *whenever the machine is awake* — launchd does not wake a sleeping Mac; a job whose slot passes during sleep fires, coalesced, at whatever wake happens next (system maintenance dark wakes make this minutes, not hours). Entries fire every day regardless of the slot's day rule: the tick itself decides whether today is an active day, so a weekend entry for a weekday-only slot is a harmless no-op. Editing times/mode updates the entries immediately; the first tick after any edit heals drift automatically.

- **RTC wakes (opt-in, one sudo).** `awewarm scheduler install --wake` arms real wake-from-sleep events (`pmset schedule wakeorpoweron`) for *every* moment the schedules need the machine — all fixed slots for today and tomorrow, plus each interval connection's next renewal, including renewals that drift: every minute, the tail of the tick recomputes the needed moments and converges the armed events to them, so a renewal chain that shifts (late wake, manual `--reset-due`) is followed automatically. A one-line sudoers grant (`/etc/sudoers.d/awewarm`, scoped to arming/cancelling wake events only — nothing else) lets the unattended tick do this without prompts; the machine wakes into a screen-off dark wake, the tick fires within seconds, and it sleeps again. Non-activation sleep is untouched — there is no prevent-sleep assertion anywhere.

  Coverage boundary: RTC wakes are reliable while the Mac sleeps normally (lid closed on power, and lid-closed on battery before standby kicks in). After hours on battery the Mac enters standby (RAM powered off) and Apple will not wake it on schedule — for that regime, delegate to an always-on server (below). `awewarm status` shows the layer's state, and `scheduler uninstall` cancels every armed event and removes the grant.

Per connection, `wakeWhenAsleep: false` opts out of both layers (asked during setup; change later with `awewarm config set <id> --no-wake`). Missed slots still fire late within the catch-up window once the machine wakes. A fully *shut down* Mac stays off — power it on and the first tick catches up anything still inside the catch-up window.

### Sleeping PCs — wake tasks (Windows)

The calendar-fire/wake split, mirrored: `scheduler install` registers one extra Task Scheduler task per fixed slot — a daily trigger at the slot time with *Wake to run* enabled, running `awewarm tick`. Interval renewals get their wake coverage through one-shot `-Once` tasks armed by the same tick-tail convergence as on macOS (no grant needed — users may register wake tasks). The per-minute tick task itself never wakes the machine (a waking tick would keep it from ever staying asleep); only slot and renewal moments do. `schtasks.exe` cannot set *Wake to run*, so the tasks are registered through PowerShell's `Register-ScheduledTask`. The setup flow asks whether fixed slots may wake the machine (same prompt as macOS), `awewarm config set <id> --no-wake` opts a connection out, and install/uninstall/refresh/self-heal keep the task set in sync with the config.

### Always-on servers (Linux)

No wake machinery exists or is needed on a machine that never sleeps — `awewarm scheduler install` sets up the systemd user timer directly (tick every minute; `Persistent=true` fires a missed tick at boot). Copy `config.json` and `secrets.json` over (or re-run `init`), and note that CLI-based connections need their CLI installed on the server. `loginctl enable-linger $USER` first on headless/SSH accounts. Linux simply cannot wake a suspended machine: the setup flow never asks, connections default to `wakeWhenAsleep: false`, and missed slots catch up within their catch-up windows once the machine wakes.

## Remote Server — your own box, or a shared hub

A lid-closed laptop on battery eventually enters standby, where no scheduled wake can reach it — and an off machine fires nothing at all. For around-the-clock warmth regardless of power state, delegate subscription connections to an always-on machine (VPS, NAS, Raspberry Pi) — `awewarm serve` for a box of your own, or `awewarm-hub serve` shared with a team, a family, or a community. CLI-account connections cannot be delegated — their login lives on your machine and keeps ticking locally.

The two flavors at a glance:

| | Solo — `awewarm serve` | Hub — `awewarm-hub serve` |
| --- | --- | --- |
| Who runs the box | you | one operator; everyone else pairs as a user |
| Who may pair | just you — the **first** token to reach an unclaimed server claims it | many users, one-time invites (`awi_...`) the operator mints |
| Software on the box | this package (`pip install awewarm`) | the separate **[awewarm-hub](https://github.com/wehuman01/awewarm-hub)** package (`pip install awewarm-hub`; same MPL-2.0) |
| Pair from your machine | `awewarm remote connect <url>` | `awewarm remote connect <url> --invite awi_...` |
| Trust | your keys, your box | every user's API keys pass through the operator's RAM — hub users must trust the operator and root |
| The right pick when… | you have, or can cheaply rent, any always-on box and want it fully yours | you have no always-on box of your own, or want to share one with several people |

Once paired, the two flavors are identical from your laptop: the same delegation commands, the same `status --remote` view, the same takeback with `--local`.

Both hold **no secrets on disk**. The pairing token and your API keys stay in the local `secrets.json` and are pushed over the wire; the server keeps them in RAM only. Restart it and the local machine re-claims and re-pushes automatically the next time it is online. A slot that came due while its key was missing is *held*, not failed — it still fires inside the catch-up window once the key returns, exactly like a machine that was asleep; past the window it is recorded as skipped.

### Set up the server (once)

**Solo** — install this package on the box and run it:

```bash
ssh my-server
pip3 install awewarm
awewarm serve                                 # listens on 127.0.0.1:8790, data at ~/.awewarm-server
awewarm serve --data-dir /data/awewarm        # ...or keep config/state/log somewhere else
```

Keep it running with a systemd user unit (`~/.config/systemd/user/awewarm.service`):

```ini
[Unit]
Description=awewarm serve
After=network-online.target

[Service]
ExecStart=awewarm serve --data-dir %h/awewarm-server
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now awewarm` (with `loginctl enable-linger $USER` on headless boxes). Expose it through a cloudflared tunnel — free TLS, no open inbound ports, your origin IP stays hidden:

```bash
cloudflared tunnel create awewarm
cloudflared tunnel route dns awewarm warm.example.com
cloudflared tunnel run --url http://127.0.0.1:8790 awewarm
```

Solo pairing safety: an unclaimed server trusts the **first** token that reaches it — anyone who finds the URL before you connect could claim it instead (your own connect then fails loudly with 403). Keep the URL private, connect promptly after starting `serve`, or pin the token ahead of time with `awewarm serve --token awt_...`.

**Hub** — the operator installs the separate package on the box and hands out invite codes:

```bash
pip3 install awewarm-hub
awewarm-hub serve                              # same ~/.awewarm-server data dir, now multi-tenant
awewarm-hub invite --name alice                # prints awi_... (one use, 48 h)
```

Admin lives there too (`awewarm-hub status / list / revoke / restore / config`); an existing `~/.awewarm-server` data dir carries over unchanged. The old spellings (`awewarm serve --hub`, `awewarm hub ...`) now die with a tombstone naming their replacement. No always-on box of your own? The project's developer runs a community hub at **https://awewarm.wehuman.top** (invite-based — request a code at peng@wehuman.top); [docs/community-hub/](./docs/community-hub/README.md) is a step-by-step user tutorial that starts from installing awewarm and setting up your first connection (中文版).

### Delegate from the laptop

Both flavors pair over **https** (e.g. via the cloudflared tunnel): `remote connect` asks for confirmation before sending the token and any API keys over plain `http://` to a non-local host.

```bash
awewarm remote connect https://warm.example.com              # solo: token generated + stored locally, server claimed
awewarm remote connect https://warm.example.com --invite awi_...   # hub: burn an invite for a personal token
awewarm config set glm --remote                   # the server takes over this connection
awewarm config set glm --duplicate --remote       # ...or keep glm local and delegate a copy of it
awewarm status                                    # merged view: local + delegated truth
awewarm status --remote                           # delegated only, plus the server health line (version/uptime/last tick)
awewarm status --local                            # locally scheduled connections only
```

`--remote` only lands after the server accepted the push, so a connection is never left with nobody ticking it. `--duplicate` copies a connection under a fresh id (`glm-copy`) — the API key is re-stored under the new id, runtime state starts blank — and with `--remote` the copy is delegated and the original disabled, so one subscription is never ticked twice. Everything keeps working on delegated connections: `config set` pushes schedule edits automatically (offline edits stay local and pending; `awewarm remote push` reconciles later), `awewarm run glm` fires on the server and reports back — and, same as locally, a successful manual run clears an auto-disabled ladder — and `awewarm config set glm --local` takes a connection back — server state is pulled first so local scheduling resumes where the server left off. `awewarm remote disconnect` refuses while anything is still delegated, then forgets the server and releases its claim (another machine can pair immediately); the pairing token stays in `secrets.json`, so reconnecting later is instant even against a server that kept the old claim. Fixed times run in the delegating machine's timezone (it travels with the push; machines whose zone has no IANA name, e.g. Windows, push a fixed `UTC±HH:MM` offset instead); wake-from-sleep does not apply on a server that never sleeps.

## Security

Local mode: your API keys never leave your machine (`secrets.json`, 0600). Delegating hands a key to that server's RAM — solo is your own box; a hub means trusting its operator (and root). Every other ordinary path is closed by design: no secrets on the server's disk, none in its logs, none readable back over the API, tenants invisible to one another. Delegate a dedicated, revocable key — and see the [hub README → Security](https://github.com/wehuman01/awewarm-hub#security) for the full picture.

## Config

Users never hand-edit config; `init` / `config add` generate it at `~/.config/awewarm/config.json` (state at `~/.local/state/awewarm/state.json`). The shape, for reference:

```json
{
  "version": 3,
  "settings": {
    "catchupMinutes": 30,
    "catchupAttempts": 5,
    "degradeAfterNodes": 3,
    "wakeWhenAsleep": false,
    "prompt": "Reply with exactly: ok",
    "maxTokens": 4,
    "schedule": {
      "mode": "fixed",
      "times": ["06:35"],
      "days": "weekday",
      "skipIfActivatedMinutes": 30,
      "windowMinutes": 300,
      "graceSeconds": 75,
      "jitterSeconds": 30
    }
  },
  "connections": {
    "local": {
      "settings": {
        "wakeWhenAsleep": true,
        "schedule": {"times": ["06:35"], "days": "weekday"}
      },
      "claude-code": {
        "label": "Claude Code",
        "cli": "/usr/local/bin/claude",
        "model": "haiku",
        "schedule": {"times": ["06:35"], "mode": "fixed"}
      }
    },
    "remote": {
      "settings": {
        "schedule": {"times": ["08:00"], "days": "every-day"}
      },
      "glm": {
        "label": "glm",
        "url": "https://open.bigmodel.cn/api/coding/paas/v4",
        "protocol": "openai-chat",
        "apiKey": "file:glm",
        "model": "GLM-5-Turbo"
      }
    }
  },
  "remote": {
    "url": "https://warm.example.com",
    "tokenRef": "file:remote:token"
  }
}
```

A connection with `url` + `apiKey` is a subscription; one with `cli` is a local account. `apiKey` is `file:<id>` — the pasted key lives in `~/.config/awewarm/secrets.json` (chmod 600), readable by the background scheduler. A connection nested under `connections.remote` is ticked by the paired `awewarm serve` server (whose URL and token ref live in the top-level `remote` block); the group alone says so — no per-connection location field. The window duration (`windowMinutes`) is a schedule field inherited through the layers (below); a confirmed window unlocks interval renewal — it only takes effect while the schedule mode is interval, fixed connections merely record it. `"hide": true` keeps a connection out of `status` listings — it still warms on its schedule, and `status <id>` still shows it.

Settings are layered three deep — every level carries the same knobs and a `schedule` block, and each field resolves through them. The split is semantic: the `schedule` block answers when a connection fires (`mode`, `times`, `days`, `skipIfActivatedMinutes`, `windowMinutes`, `graceSeconds`, `jitterSeconds`); the knobs answer how an activation behaves — `catchupMinutes`/`catchupAttempts`/`degradeAfterNodes` (catch-up and the degrade ladder), `wakeWhenAsleep` (may fixed slots wake a sleeping machine), and `prompt`/`maxTokens` (the warm-up request's prompt and token cap). Setting `windowMinutes` on a layer vouches for that duration for every connection under it without its own record, unlocking interval; a CLI account's builtin window is never overridden by a layer:

1. **global** — the top-level `settings`: knobs every connection inherits, plus default schedule fields (the saved block always names its `mode`, so the file shows whether the default is `fixed` or `interval`).
2. **connections.local / connections.remote** — per-location overrides nested under each location group.
3. **profile** — a connection's own overrides, written directly on it (`schedule` plus any knob, no `settings` wrapper — the layers need one to share a dict with the connection ids, a connection does not); they always win, and `--inherit-schedule` drops them back to the layers above. One field is always present here: `mode`. Every saved connection names its mode (`fixed` or `interval`) even when it matches the layers, so the file shows it without running `status` — and, deliberately, changing a layer's mode never re-modes an existing connection; switch each one explicitly.

One deliberate asymmetry: a delegated (`remote`) connection never follows the global schedule — it describes this machine's day. Remote connections resolve their schedule from their own settings and `connections.remote.settings` only (knobs still inherit globally) — with one exception: `windowMinutes` is a fact about the plan, not about any machine's day, so the global block's window duration reaches delegated connections too. An inherited interval mode never breaks a connection whose window is unverified — such connections stay on fixed until their window is recorded. Delegating a connection freezes its then-effective schedule as its own settings, so handover never changes what fires. Configs saved by slightly older builds (a knob-position `windowMinutes`, a schedule-position `wakeWhenAsleep`) fold into the current positions on first load and are never written back.

## Commands

```bash
awewarm init                          # interactive onboarding: scan accounts, pick schedules, install scheduler
awewarm discover                      # read-only scan of local CLIs and logins
awewarm config add                    # add a connection: a detected account or a subscription endpoint
awewarm config set <id> [flags]       # show or change settings: --times, --days, --mode, --on/--off, --hide/--show,
                                       #   --anchor, --start, --window, --api-key, --wake/--no-wake, --remote/--local,
                                       #   --catchup-minutes, --catchup-attempts, --degrade-after-nodes,
                                       #   --inherit-schedule (drop own schedule overrides, follow the layers)
awewarm config settings [scope] [flags]  # show or change the settings layers: scope is global (default), local,
                                       #   or remote; flags: --catchup-*, --degrade-after-nodes, --window-minutes,
                                       #   --prompt, --max-tokens, --times, --days, --mode, --wake/--no-wake, --reset
awewarm config remove <id>            # delete a connection, its state, and its stored API key
awewarm config show / edit            # print the on-disk config / open it in $EDITOR (validated on exit)
awewarm config template               # print the reference config shape (what hand-edits must match)
awewarm config path                   # config / state / log locations
awewarm status [<id>] [--json]        # summary; one connection in detail; redacted machine-readable dump
awewarm status --remote / --local     # delegated connections only (with the server health line) / locally scheduled only
awewarm run [--force]                 # fire every enabled connection now, ignoring the schedule (prompts; --force skips)
awewarm run <id> [--reset-due]        # fire one connection now (schedule untouched unless --reset-due)
awewarm scheduler install [--wake] / uninstall # background scheduler (launchd / Task Scheduler / systemd); --wake also arms RTC wake-from-sleep
awewarm serve [--data-dir /data] [--token awt_...]  # run the always-on server that ticks delegated connections
                                       #   one server, many invited users: the separate awewarm-hub package
awewarm remote connect <url> [--invite awi_...|--token awt_...]
                                       #   pair with a server: solo serve generates + stores a token;
                                       #   a hub takes --invite awi_... (or a saved --token)
awewarm remote push [<id>]             # re-sync delegated connections to the server (config + keys)
awewarm remote disconnect              # forget the server + release its claim (refuses while delegations exist)
awewarm self-update [--check]         # upgrade to the latest PyPI release
```

Commands from pre-0.3 releases (`add plan`, `times`, `enable`, `disable`, `verify`, `anchor`, `activate`, `remove`, `install`, `uninstall`, `inspect`) still work as hidden aliases; they print their new spelling and will be removed in v1.0. `remote status` folded into `status --remote` (kept as a hidden alias the same way). `awewarm update` was removed outright in v0.5.0 — use `awewarm self-update`.

## Self-Update

awewarm checks PyPI in the background — at most once a day, and never during scheduler ticks. When a newer release exists, interactive commands print a reminder to stderr.

```bash
awewarm self-update            # upgrade to the latest release
awewarm self-update --check    # show versions only
```

To disable the background check:

```bash
export AWEWARM_NO_UPDATE_CHECK=1
```

## Development

```bash
pip install -e .
python3 -m unittest discover -s tests
```

`awewarm -v` says `editable` (with the git state) when running from this checkout; pip's recorded metadata freezes at `pip install -e .` time, so re-run it after a version bump to keep `pip show` in sync. `awewarm self-update` refuses on a checkout — pull and re-install instead.

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for the engineering doctrine and [docs/CHANGELOG.md](docs/CHANGELOG.md) for release history.

## Support

If awewarm saves your quota, consider supporting it:

- ⭐ Star the repo — it helps others find it.
- ☕ [Ko-fi](https://ko-fi.com/mugpeng) — buy me a coffee.
- 💬 WeChat — scan the QR code below.

<p align="center">
  <img src="assets/images/wechat-pay.jpg" alt="WeChat Pay" width="240">
</p>

> awewarm is free and open source. Sponsors keep it maintained — thank you.
