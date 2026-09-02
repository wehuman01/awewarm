# awewarm update: Your CLI Login Accounts Can Delegate Too

![awewarm](../../../logo/hero.png)

The community-hub announcement had a table with one blunt row: subscription endpoints (base URL + API key) — the hub can keep them warm; local CLI accounts (`claude` / `codex` logins) — **no**. The reason was just as blunt: the login lives on your machine, and the server can't have it.

This update takes that row back. `awewarm config set <id> --remote` now works on Claude Code and Codex login accounts: your machine reads its own credentials (the macOS Keychain entry for Claude, `~/.codex/auth.json` for Codex), pushes them like an API key, and the server runs the CLI on your behalf. Your local login stays the single source of truth — every push and the background sync (at most twice an hour) re-read it, and a fingerprint mismatch re-pushes automatically. `status` shows the fingerprint too.

The trust model is unchanged: credentials live in the server's RAM only, never on disk. One addition — an account-wide login covers every subscription under it, so delegating one asks for an explicit confirmation naming the target server URL (`--yes` skips it in scripts).

## No CLI on the Server? Also Fine

Delegation used to require the matching CLI installed on the server box. That prerequisite is gone too: pushing to a machine without `claude` / `codex` no longer fails. The server records the connection as native mode and, at fire time, sends a plain HTTPS request — for Codex, a ChatGPT backend SSE request built from its OAuth tokens; for Claude, a POST carrying the accessToken. The push validates the credential offline first, so a delegation that is guaranteed to fail is rejected on the spot with the exact fix (run `claude /login` or `codex login` locally, then push again). Install the CLI on that box later and one re-push flips the connection back to CLI mode.

## New Machine: One Backup, One Restore

Pairing is machine-bound, and switching machines always meant re-pairing and asking the operator to free a slot. There is now a migration path:

```bash
awewarm config backup        # writes the archive, prints the path
# carry the file to the new machine (it holds plaintext keys — encrypt in transit)
awewarm config restore awewarm-backup.tar.gz
```

One tar.gz carries config, secrets, state, and the machine-id. Restore it on the new machine and the hub treats it as the same one — no new pairing slot, no operator involvement, schedule and keys pick up where they left off. Restore refuses to overwrite existing files without `--force`, and rejects archives holding unexpected members.

## Laptop Rarely Online: Opt-in Server-side Keys

By design, delegated keys live in the server's RAM only, and your machine re-pushes whenever it is online. The cost: if the server restarts while your machine is off, that machine's warm-ups wait until you come back. For machines that go online rarely, a connection can now opt into server-side storage (`keys.json`, plaintext, 0600) that survives restarts:

```bash
awewarm config set glm-sub --persist-key on
```

The option ships off by default, and we discourage it — a plaintext key on the server's disk widens the trust boundary from "the server's memory" to "the server's disk." So every step confirms: turning it on defaults to No, and turning it off tells you both consequences (the server deletes the key at once; a restart while you're offline holds warm-ups again). On a hub, the operator must also allow it first (`awewarm-hub config --persist-keys on`); a hub that hasn't rejects persisted pushes with a 403. `status` labels where each key lives, and taking a connection back purges its disk copy.

## You Say, awewarm Runs

| You say | awewarm runs |
|---|---|
| "My Claude Code login should stay warm on the server too." | `awewarm config set <id> --remote` |
| "I just re-logged in on my laptop." | `awewarm remote push` (or wait — the background sync re-pushes) |
| "I don't want a CLI on the server." | Just push — the connection runs over native HTTPS |
| "I'm switching to a new machine." | `awewarm config backup` → `awewarm config restore` on the new one |
| "This machine is offline for weeks at a time." | `awewarm config set <id> --persist-key on` |

One-line summary: delegation no longer picks connection types — from API keys to CLI logins, a whole machine's warm-ups can move to the server.

## Try It

### Let the agent install it

In Claude Code, Codex, or any coding agent, say:

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

### Or do it yourself

```bash
pip install awewarm

# CLI login accounts delegate the same way
awewarm config set claude-main --remote

# Machine migration
awewarm config backup

# Laptop rarely online? Optional
awewarm config set glm-sub --persist-key on
```

## Apply Now

Don't want to run your own server? The community hub at [awewarm.wehuman.top](https://awewarm.wehuman.top) is still taking test users: 10 spots, first come first served.

Email [peng@wehuman.top](mailto:peng@wehuman.top) — who you are, which plan to keep warm. Bugs go to [GitHub issues](https://github.com/wehuman01/awewarm/issues).

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
