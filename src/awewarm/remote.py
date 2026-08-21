"""Client half of remote delegation: talk to an `awewarm serve` process.

The local machine owns every secret: the server token and the delegated API
keys live in local secrets.json and are pushed over the wire (TLS via a
cloudflared tunnel) whenever the server needs them. The server keeps them in
RAM only, so after it restarts this module re-claims it and re-sends keys.
"""
import json
import secrets as _secrets
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, keystore

# Namespaced so no connection id can collide with it (slugify never emits ":").
TOKEN_SECRET_ID = "remote:token"
LEGACY_TOKEN_SECRET_ID = "remote-token"
TIMEOUT_SECONDS = 5
# The server fires the real request while holding its lock before answering a
# run (its own cap: ACTIVATION_TIMEOUT_SECONDS per activation) — the client
# must outwait it, or a slow endpoint reports failure here while the request
# actually went out, inviting a duplicate retry.
RUN_TIMEOUT_SECONDS = 30


class RemoteError(Exception):
    """One failed round-trip; str(exc) is user-ready (never contains secrets)."""


def generate_token():
    return "awt_" + _secrets.token_urlsafe(32)


def remote_url(config):
    return (config.get("remote") or {}).get("url")


def load_token():
    token = keystore.load_api_key(f"file:{TOKEN_SECRET_ID}")
    if token:
        return token
    legacy = keystore.load_api_key(f"file:{LEGACY_TOKEN_SECRET_ID}")
    if legacy:
        store_token(legacy)
    return legacy


def store_token(token):
    keystore.store_api_key(TOKEN_SECRET_ID, token)
    keystore.delete_api_key(LEGACY_TOKEN_SECRET_ID)


def _request(url, method, path, body=None, token=None, timeout=TIMEOUT_SECONDS):
    target = url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Content-Type": "application/json",
        # urllib's default "Python-urllib/3.x" UA is on Cloudflare Bot Fight
        # Mode's signature blocklist (error 1010) — name ourselves instead.
        "User-Agent": f"awewarm/{__version__}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(target, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("error", "")
        except Exception:
            detail = ""
        message = f"{method} {path} failed (HTTP {exc.code}){': ' + detail if detail else ''}"
        if exc.code == 403 and not detail:
            # Cloudflare Bot Fight Mode answers with a bare "error code: 1010"
            # plain-text body — point at the proxy, not the awewarm server.
            message += " — a proxy or WAF (e.g. Cloudflare bot protection) in front of the server may be blocking this client"
        raise RemoteError(message)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise RemoteError(f"cannot reach the awewarm server at {url}: {reason}")
    try:
        return json.loads(raw or "{}")
    except ValueError:
        raise RemoteError(f"{url} answered, but not like an awewarm server (invalid JSON)")


def healthz(url):
    return _request(url, "GET", "/healthz")


def claim(url, token):
    return _request(url, "POST", "/v1/claim", {"token": token})


def join(url, invite):
    """Exchange a one-time hub invite for a personal pairing token."""
    return _request(url, "POST", "/v1/join", {"invite": invite})


def release(url, token):
    return _request(url, "POST", "/v1/release", {}, token)


def push_connection(url, token, conn_id, conn, api_key, timezone):
    payload = {"connection": conn, "apiKey": api_key, "timezone": timezone}
    return _request(url, "PUT", f"/v1/connections/{urllib.parse.quote(conn_id, safe='')}", payload, token)


def push_keys(url, token, mapping):
    return _request(url, "PUT", "/v1/keys", mapping, token)


def fetch_state(url, token):
    return _request(url, "GET", "/v1/state", token=token)


def delete_connection(url, token, conn_id):
    return _request(url, "DELETE", f"/v1/connections/{urllib.parse.quote(conn_id, safe='')}", token=token)


def run_connection(url, token, conn_id, reset_due=False, allow_auto_disabled=False):
    """Fire one connection on the server. allow_auto_disabled mirrors the local
    split: an explicit `run <id>` fires (and on success clears) an auto-disabled
    ladder, `run` (no id) skips those connections."""
    return _request(
        url, "POST", f"/v1/connections/{urllib.parse.quote(conn_id, safe='')}/run",
        {"resetDue": reset_due, "allowAutoDisabled": allow_auto_disabled},
        token, timeout=RUN_TIMEOUT_SECONDS,
    )


def ensure_session(config):
    """Server reachable, claimed, and accepting our token → its state view.

    Re-claims automatically after a server restart (its token lived in RAM);
    raises RemoteError when no server is configured or it stays unreachable.
    """
    url = remote_url(config)
    token = load_token()
    if not url or not token:
        raise RemoteError("no remote server connected\nfix: awewarm remote connect <url>")
    if not healthz(url).get("claimed"):
        claim(url, token)
    return fetch_state(url, token)
