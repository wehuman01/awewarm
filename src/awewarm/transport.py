"""Send one minimal activation request through a connection's transport.

Builders (activation_argv / http_request_parts / activation_env /
native_request_parts) are pure and unit-tested; senders do the I/O. Results
never contain API keys, credentials, or auth headers.
"""
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .config import die
from .credstore import claude_access_token, codex_auth

HTTP_TIMEOUT_SECONDS = 60
CLI_TIMEOUT_SECONDS = 120
CLI_TRANSPORT_KINDS = ("claude-cli", "codex-cli")
DETAIL_LIMIT = 200
SECRET_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|AUTH)", re.IGNORECASE)

# Native account transport: when a server has no provider CLI installed, it
# warms a delegated account by speaking the CLI's own backend protocol over
# HTTPS. Defaults mirror what each CLI itself uses (verified against the live
# backends 2026-08-26); transport.baseUrl overrides the endpoint for relays.
CODEX_BACKEND_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_NATIVE_MODEL = "gpt-5.6-luna"
CLAUDE_API_BASE = "https://api.anthropic.com"
CLAUDE_NATIVE_MODEL = "claude-sonnet-5"
# The codex backend streams SSE; after a 200 the warm-up already happened, so
# the stream is drained best-effort under this cap purely to close cleanly.
NATIVE_SSE_CAP_BYTES = 128 * 1024


def redact(value):
    """Deep copy with secret-looking string values replaced, for display paths."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if SECRET_RE.search(k) and isinstance(v, str) and v else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def activation_argv(connection):
    """argv for CLI transports, or None for HTTP transports."""
    transport = connection["transport"]
    kind = transport["kind"]
    activation = connection["activation"]
    if kind == "claude-cli":
        argv = [transport.get("cliCommand") or "claude", "-p"]
        if activation.get("model"):
            argv += ["--model", activation["model"]]
        return argv + [activation["prompt"]]
    if kind == "codex-cli":
        # Scheduled ticks run outside any git repo (launchd's cwd is /, systemd's
        # the home dir); codex exec refuses those without this flag.
        argv = [transport.get("cliCommand") or "codex", "exec", "--skip-git-repo-check"]
        if activation.get("model"):
            argv += ["-m", activation["model"]]
        return argv + [activation["prompt"]]
    return None


VERSIONED_BASE_RE = re.compile(r"/v\d+$")


def endpoint_url(base, path):
    """Append an endpoint path to a base URL.

    Bases already ending in a version segment (/v1, /v4, ...) are complete:
    the endpoint hangs off them directly. Only a bare host gets /v1 added.
    """
    return base + path if VERSIONED_BASE_RE.search(base) else base + "/v1" + path


def http_request_parts(connection, api_key):
    """(url, headers, body) for HTTP transports; None for CLI transports."""
    transport = connection["transport"]
    kind = transport["kind"]
    activation = connection["activation"]
    base = (transport.get("baseUrl") or "").rstrip("/")
    model = activation.get("model")
    if not model:
        die(f"activation.model is required for {kind} connections\nfix: re-add the plan or edit activation.model")
    prompt = activation["prompt"]
    max_tokens = activation.get("maxTokens", 4)
    if kind == "anthropic-messages":
        url = endpoint_url(base, "/messages")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif kind == "openai-chat":
        url = endpoint_url(base, "/chat/completions")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif kind == "openai-responses":
        url = endpoint_url(base, "/responses")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {"model": model, "input": prompt, "max_output_tokens": max_tokens}
    else:
        return None
    return url, headers, body


def _detail(text):
    text = (text or "").strip()
    return text[:DETAIL_LIMIT] if text else ""


def _extract_error(body_bytes):
    """Pull a provider error message out of an HTTP error body, if any."""
    try:
        payload = json.loads(body_bytes.decode("utf-8", "replace") or "{}")
    except ValueError:
        return _detail(body_bytes.decode("utf-8", "replace"))
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return _detail(str(error.get("message") or error))
    if error:
        return _detail(str(error))
    return _detail(json.dumps(payload))


def _powershell():
    return (
        shutil.which("powershell")
        or shutil.which("powershell.exe")
        or "powershell.exe"
    )


def _send_cli(connection, env=None):
    transport = connection["transport"]
    command = transport.get("cliCommand") or ("claude" if transport["kind"] == "claude-cli" else "codex")
    # Resolve to an absolute path: launchd runs with a minimal PATH where
    # user-local install dirs (~/.local/bin and friends) are missing.
    resolved = shutil.which(command)
    if resolved is None:
        return {"ok": False, "detail": f"{command} not found in PATH — install the CLI or set transport.cliCommand"}
    argv = activation_argv(connection)
    if resolved.lower().endswith(".ps1"):
        # CreateProcess cannot execute .ps1 scripts directly (PowerShell
        # installs on Windows); route them through powershell -File.
        argv = [_powershell(), "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", resolved, *argv[1:]]
    else:
        argv[0] = resolved
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            # Both CLIs append piped stdin to the prompt; an open pipe would
            # block the headless tick until the timeout. Never read our stdin.
            stdin=subprocess.DEVNULL,
            # A delegated login rides in as env vars layered over ours; a
            # locally-fired CLI sees its own login and gets no overlay.
            env={**os.environ, **env} if env else None,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"{command} timed out after {CLI_TIMEOUT_SECONDS}s"}
    except OSError as exc:
        return {"ok": False, "detail": f"{command} failed to start: {exc}"}
    if proc.returncode == 0:
        return {"ok": True, "detail": _detail(proc.stdout.splitlines()[0] if proc.stdout else "")}
    return {"ok": False, "detail": _detail(proc.stderr or proc.stdout) or f"{command} exited {proc.returncode}"}


def _send_http(connection, api_key, timeout_seconds=None):
    parts = http_request_parts(connection, api_key)
    url, headers, body = parts
    if timeout_seconds is None:
        timeout_seconds = HTTP_TIMEOUT_SECONDS
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response.read()
            return {"ok": True, "detail": ""}
    except urllib.error.HTTPError as exc:
        try:
            body_bytes = exc.read()
        except Exception:
            body_bytes = b""
        message = _extract_error(body_bytes)
        detail = f"HTTP {exc.code}"
        if message:
            detail += f": {message}"
        return {"ok": False, "detail": detail}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "detail": f"request failed: {reason}"}


def activation_env(connection, credential, sandbox_root=None, conn_id=None):
    """Env overlay that injects a delegated login credential into the CLI
    subprocess; {} when the connection fires locally with its own login.

    claude: CLAUDE_CODE_OAUTH_TOKEN carries the login's access token. codex:
    CODEX_HOME points at a per-connection sandbox whose auth.json is rewritten
    from the pushed credential before every fire, so any token refresh the
    server-side CLI performs is discarded on the next pass (the local login is
    the source of truth). Raises ValueError when the credential's JSON shape
    is not recognized.
    """
    if credential is None:
        return {}
    kind = connection["transport"]["kind"]
    if kind == "claude-cli":
        return {"CLAUDE_CODE_OAUTH_TOKEN": claude_access_token(credential)}
    if kind == "codex-cli":
        if sandbox_root is None:
            raise ValueError("a delegated codex connection needs its sandbox dir (internal error)")
        home = Path(sandbox_root).expanduser() / (conn_id or "codex")
        home.mkdir(parents=True, exist_ok=True)
        auth = home / "auth.json"
        fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(credential)
        return {"CODEX_HOME": str(home)}
    raise ValueError(f"a {kind} connection carries no login credential")


def native_request_parts(connection, credential):
    """(url, headers, body) to warm an account natively over HTTPS — the
    protocol each CLI itself speaks, so a server without the CLI installed
    can still fire the connection. Raises ValueError when the credential's
    JSON shape is not recognized."""
    kind = connection["transport"]["kind"]
    activation = connection["activation"]
    base = (connection["transport"].get("baseUrl") or "").rstrip("/")
    prompt = activation["prompt"]
    if kind == "codex-cli":
        token, account = codex_auth(credential)
        url = (base or CODEX_BACKEND_BASE) + "/responses"
        headers = {
            "Authorization": f"Bearer {token}",
            "chatgpt-account-id": account,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "Accept": "text/event-stream",
            "content-type": "application/json",
        }
        body = {
            "model": activation.get("model") or CODEX_NATIVE_MODEL,
            "input": [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }],
            "stream": True,
            "store": False,
        }
        return url, headers, body
    if kind == "claude-cli":
        url = endpoint_url(base or CLAUDE_API_BASE, "/messages")
        headers = {
            "Authorization": f"Bearer {claude_access_token(credential)}",
            "anthropic-version": "2023-06-01",
            # Tells the API this bearer is a Claude subscription login,
            # not a scoped API key — the header Claude Code itself sends.
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        }
        body = {
            "model": activation.get("model") or CLAUDE_NATIVE_MODEL,
            "max_tokens": activation.get("maxTokens", 4),
            "messages": [{"role": "user", "content": prompt}],
        }
        return url, headers, body
    raise ValueError(f"a {kind} connection cannot fire natively")


def _sse_failure(text):
    """The error message in one `response.failed` SSE data line, if any."""
    marker = '"response.failed"'
    if marker not in text:
        return None
    try:
        payload = json.loads(text[6:]) if text.startswith("data: ") else None
    except ValueError:
        payload = None
    error = ((payload or {}).get("response") or {}).get("error")
    if isinstance(error, dict) and error.get("message"):
        return _detail(str(error["message"]))
    return "the provider reported response.failed"


def _send_native(connection, credential, timeout_seconds=None):
    """Fire one native account activation. A 2xx means the provider accepted
    the request — the warm-up already happened — so the codex SSE stream is
    drained best-effort and read errors after a 200 never count as failure
    (a phantom failure would invite duplicate retries)."""
    url, headers, body = native_request_parts(connection, credential)
    if timeout_seconds is None:
        timeout_seconds = HTTP_TIMEOUT_SECONDS
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        try:
            body_bytes = exc.read()
        except Exception:
            body_bytes = b""
        message = _extract_error(body_bytes)
        if exc.code in (401, 403):
            return {
                "ok": False,
                "detail": f"credential rejected (HTTP {exc.code})"
                          + (f": {message}" if message else "")
                          + " — log in again on the local machine, then: awewarm remote push",
            }
        detail = f"HTTP {exc.code}"
        if message:
            detail += f": {message}"
        return {"ok": False, "detail": detail}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "detail": f"request failed: {reason}"}
    with response:
        if connection["transport"]["kind"] == "codex-cli":
            try:
                drained = 0
                while drained < NATIVE_SSE_CAP_BYTES:
                    line = response.readline()
                    if not line:
                        break
                    drained += len(line)
                    failure = _sse_failure(line.decode("utf-8", "replace"))
                    if failure:
                        return {"ok": False, "detail": failure}
            except (OSError, TimeoutError):
                pass
            return {"ok": True, "detail": ""}
        try:
            response.read(NATIVE_SSE_CAP_BYTES)
        except (OSError, TimeoutError):
            pass
        return {"ok": True, "detail": ""}


def send_native(connection, credential, timeout_seconds=None):
    """Send one minimal activation request natively (no provider CLI needed).
    Returns {"ok": bool, "detail": str}; ValueError from credential parsing
    becomes an activation failure pointing at a re-push."""
    try:
        return _send_native(connection, credential, timeout_seconds)
    except ValueError as exc:
        return {"ok": False, "detail": _detail(str(exc))}


def remove_sandbox(sandbox_root, conn_id):
    """Drop one connection's codex sandbox (takeback / delete on the server)."""
    shutil.rmtree(Path(sandbox_root) / conn_id, ignore_errors=True)


def send_activation(connection, api_key=None, timeout_seconds=None, credential=None, sandbox_root=None, conn_id=None):
    """Send one minimal activation request. Returns {"ok": bool, "detail": str}.

    timeout_seconds caps an HTTP request (default 60); the delegation server
    passes a tighter one so a dead endpoint cannot stall its tick loop. CLI
    transports always run at their own cap (CLI_TIMEOUT_SECONDS) instead.
    credential injects a delegated login into the CLI subprocess (with the
    codex sandbox under sandbox_root); a locally-fired CLI gets none.
    """
    if connection["transport"]["kind"] in CLI_TRANSPORT_KINDS:
        try:
            env = activation_env(connection, credential, sandbox_root=sandbox_root, conn_id=conn_id)
        except ValueError as exc:
            return {"ok": False, "detail": _detail(str(exc))}
        return _send_cli(connection, env or None)
    if not api_key:
        die("no API key available for this subscription connection\nfix: re-add the plan with: awewarm config add")
    return _send_http(connection, api_key, timeout_seconds)
