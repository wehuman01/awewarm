# awewarm Tutorial: Turn a Spare Machine into Your Warming Server

![awewarm](../../../logo/hero.png)

Earlier posts covered how delegation works: find a machine that never powers off, and let it fire the heartbeats for your laptop. So where does that machine come from? The community hub is one answer, but spots are limited. Truth is, any box that stays on will do: a cheap VPS, a dusty NAS, a Raspberry Pi, or an old laptop plugged in and tucked into a closet. This post walks the whole path from zero — about twenty minutes if nothing goes sideways.

One prerequisite: awewarm installed on your own machine with at least one connection running (if not, `awewarm init` is a two-command affair).

GitHub: [github.com/wehuman01/awewarm](https://github.com/wehuman01/awewarm)

## Decide First: Solo or Hub?

A machine just for yourself — plain awewarm is enough (solo). Want family and friends to share one box, each managing their own connections without seeing each other's — install awewarm-hub (hub). Every step below shows both variants side by side; follow your line. Switching later costs nothing either — the data directory is shared, so you just move over.

## Step 1: Install the Package

```bash
ssh my-server

pip3 install awewarm        # solo: just for you
pip3 install awewarm-hub    # hub: for a group
```

## Step 2: Start the Server

```bash
# solo
awewarm serve               # listens on 127.0.0.1:8790, data at ~/.awewarm-server

# hub
awewarm-hub serve           # same data directory, now multi-tenant
awewarm-hub invite --name alice   # mints an invite code (awi_..., one use, 7-day expiry)
```

Both servers bind to loopback only — nothing exposed yet; that's Step 4.

Hub admin lives under the `awewarm-hub` command: `invite` mints codes, `list` shows users and invites, `revoke` suspends, `restore` undoes.

## Step 3: Keep It Running

Not with nohup. Write a systemd user unit (`~/.config/systemd/user/awewarm.service`):

```ini
[Unit]
Description=awewarm serve
After=network-online.target

[Service]
ExecStart=awewarm serve --data-dir %h/awewarm-server
# for hub mode, use this line instead:
# ExecStart=awewarm-hub serve --data-dir %h/awewarm-server
Restart=on-failure

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user enable --now awewarm
loginctl enable-linger $USER   # keeps it running without a login session; required on a server
```

## Step 4 (Optional): Make the Server Reachable

Your laptop needs to reach the box. Pick one of these.

**Option A: direct LAN connection.** For a NAS or Raspberry Pi on the same router as your computer, this is the simplest — listen on the LAN and connect by internal address:

```bash
awewarm serve --bind 0.0.0.0          # on the server: change the listen address
awewarm remote connect http://192.168.1.20:8790   # on the laptop: connect over the LAN
```

Connecting over plain http to a non-local address makes awewarm confirm once before sending the token — a deliberate heads-up, just confirm it. If the box never leaves your home Wi-Fi, this is all you need.

**Option B: a cloudflared tunnel (recommended for VPS).** A machine on the public internet shouldn't run a naked port. The tunnel brings free TLS for nothing, and your origin IP hides behind Cloudflare:

```bash
cloudflared tunnel create awewarm
cloudflared tunnel route dns awewarm warm.example.com
cloudflared tunnel run --url http://127.0.0.1:8790 awewarm
```

Connect to `https://warm.example.com` from the laptop. cloudflared itself should also run as a service — its own docs cover that well.

**Option C: your own reverse proxy.** Already running nginx or caddy? Reverse-proxy 8790 behind a certificate — same result as the tunnel.

## The Critical Step: Pair Promptly

One solo rule to remember: **an unclaimed server belongs to whoever pairs first** — the first token to arrive is the owner. If someone beats you to it, your own connect fails loudly with a 403.

So pick one: keep the URL private; pair right after starting `serve`; or pin a token ahead of time:

```bash
awewarm serve --token awt_your-own-token
```

Hub has no such worry: pairing burns a one-time invite code, the codes are in your hands, and the server stays yours.

## Step 5: Pair, and Hand Over the Connections

Back on your machine:

```bash
# solo: a token is generated locally into secrets.json, claiming the server
awewarm remote connect https://warm.example.com

# hub: burn an invite code for your personal token
awewarm remote connect https://warm.example.com --invite awi_xxx

# then delegate — both modes, the exact same command
awewarm config set glm --remote
```

From this step on, solo and hub are identical. Over https, no plaintext-confirmation prompt appears — that one is reserved for bare http to a public address.

CLI login accounts (Claude Code / Codex) delegate the same way, same command. An account credential covers every subscription under the login, so there's one extra confirmation; scripts pass `--yes`. The server needs no CLI installed at all: it uses the server's copy if one exists, and otherwise fires native HTTPS requests straight at the vendor backend.

## Step 6: Day-to-Day

After delegation, it's all the commands you already know:

```bash
awewarm status               # delegated connections show the server's live state
awewarm status --remote      # delegated only, plus the server health line (version/uptime/last tick)
awewarm run glm              # a manual make-up fire, executed on the server, reported back
awewarm config set glm --local   # changed your mind — take it back, local scheduling resumes
```

Schedule edits need no manual push: `config set` syncs to the server automatically. If the server happens to be unreachable when you edit, the change stays local under a pending marker and pushes itself once the network returns.

## Where the Key Lives

By default: your key lives in the server's **RAM** only, never on disk. A server restart loses it — no matter, your machine re-claims and re-pushes the moment it's online; slots that come due in the meantime still complete inside the catch-up window.

For machines that are genuinely offline for weeks, the key can be written onto the server's disk (`--persist-key`). But it ships off by default, confirms at every step, and we don't recommend it — a plaintext key on disk means trusting the server's whole disk. The full accounting is in the philosophy post.

## Common Questions

**The server restarted.** Do nothing — the key re-pushes automatically. A brief key-missing warning shows in `status` and clears once you're online.

**Switching to a different server.** `config set <id> --local` to take connections back one by one, `remote disconnect` to forget the old one, then run the pairing again against the new address. Your local config and schedule were never touched.

**The tunnel dropped / network hiccup.** `status` shows the last snapshot labeled stale — not a blank — and catches up automatically once things recover.

**Started solo, want a hub now?** Hand the same data directory to `awewarm-hub serve` — it migrates in place, and back again too.

## Apply Now

No box that can stay on? The community hub at [awewarm.wehuman.top](https://awewarm.wehuman.top) is still taking test users: 10 spots, first come first served.

Email [peng@wehuman.top](mailto:peng@wehuman.top) — who you are, which plan to keep warm. Bugs go to [GitHub issues](https://github.com/wehuman01/awewarm/issues).

## Try It

### Let the agent install it

In Claude Code, Codex, or any coding agent, say:

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

### Or do it yourself

```bash
pip install awewarm

# Start locally first
awewarm init

# Have a box? Twenty minutes to self-host (follow the steps above)
ssh my-server
pip3 install awewarm && awewarm serve
```

## More from the awewarm Series

- [awewarm：牛来，让你的ai订阅时刻热起来](https://mp.weixin.qq.com/s/HYAzfUPF_PUEfio4nZs1KA)

## More from mugpeng

awewarm is part of the aweteam ecosystem:

- **[aweskill](https://aweskill.webioinfo.top/)** — CLI-first skill package manager for 47+ AI coding agents
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — profile switcher for Claude Code, Codex, and OpenCode
- **[awerouter](https://github.com/mugpeng/awerouter)** — smart LLM router: splits requests between Flash (cheap) and Pro (capable) providers based on structural signals, cutting unnecessary model spend
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — collect, organize, and restore AI coding sessions; pair it with aweswitch to save configurations and launch with one command
- **[aweshare](https://github.com/wehuman01/aweshare)** — share your local Ollama/vLLM or authorized OpenAI/Anthropic backends through a self-hosted hub — a sharing economy for tokens
- **[awewarm](https://github.com/wehuman01/awewarm)** — subscription-window keeper: keeps your AI coding plan's window continuously active, whether on a local machine or a remote server over the wire
