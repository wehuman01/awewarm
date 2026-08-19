"""Send one minimal activation request through a connection's transport.

Builders (activation_argv / http_request_parts) are pure and unit-tested;
senders do the I/O. Results never contain API keys or auth headers.
"""
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from .config import die

HTTP_TIMEOUT_SECONDS = 60
CLI_TIMEOUT_SECONDS = 120
DETAIL_LIMIT = 200
SECRET_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|AUTH)", re.IGNORECASE)


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
        argv = [transport.get("cliCommand") or "codex", "exec"]
        if activation.get("model"):
            argv += ["-m", activation["model"]]
        return argv + [activation["prompt"]]
    return None


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
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
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
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
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
        url = base + ("/responses" if base.endswith("/v1") else "/v1/responses")
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


def _send_cli(connection):
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
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"{command} timed out after {CLI_TIMEOUT_SECONDS}s"}
    except OSError as exc:
        return {"ok": False, "detail": f"{command} failed to start: {exc}"}
    if proc.returncode == 0:
        return {"ok": True, "detail": _detail(proc.stdout.splitlines()[0] if proc.stdout else "")}
    return {"ok": False, "detail": _detail(proc.stderr or proc.stdout) or f"{command} exited {proc.returncode}"}


def _send_http(connection, api_key):
    parts = http_request_parts(connection, api_key)
    url, headers, body = parts
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
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


def send_activation(connection, api_key=None):
    """Send one minimal activation request. Returns {"ok": bool, "detail": str}."""
    if connection["transport"]["kind"] in ("claude-cli", "codex-cli"):
        return _send_cli(connection)
    if not api_key:
        die("no API key available for this subscription connection\nfix: re-add the plan with: awewarm add plan")
    return _send_http(connection, api_key)
