"""Egress policy for awewarm's own HTTPS requests.

Whether traffic needs a proxy is a property of the destination and the local
network — which machine-wide environment variables (http_proxy/https_proxy/
all_proxy, typically set for git/npm or a desktop Clash) cannot express. All
of awewarm's own requests therefore go DIRECT and ignore those variables:
the hub control channel must not inherit a proxy's failure domain, and the
interactive CLI behaves exactly like the background tick, which runs without
a shell environment. A network that genuinely requires a proxy opts in
explicitly with `awewarm config proxy <url>`.

CLI subprocesses (claude/codex) are outside this policy: they inherit the
ambient environment and make their own proxy decisions.
"""
import urllib.request

from .config import load_config


def client_proxy(config=None):
    """The proxyUrl the client config names, or None for direct egress.

    Loads the client config when not handed one. The client file refuses an
    invalid value at load time; anything malformed that still slips through
    falls back to direct — a bad proxy URL must never take a fire down."""
    raw = (config if config is not None else load_config()).get("proxyUrl")
    if isinstance(raw, str):
        stripped = raw.strip()
        for scheme in ("http://", "https://"):
            if stripped.lower().startswith(scheme) and len(stripped) > len(scheme):
                return stripped
    return None


def opener(proxy=None):
    """A urllib opener: through the explicit proxy, or direct (env ignored).

    Direct egress must pass an explicit empty ProxyHandler: build_opener()
    with no handler installs its default one, which reads http_proxy and
    friends from the environment. The empty handler suppresses that default
    and is then itself dropped by OpenerDirector.add_handler — leaving an
    opener with no proxy logic at all, so requests connect straight to their
    target host no matter what the environment says."""
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(request, timeout, proxy=None):
    """urlopen through the egress policy. Callers decide the proxy: the client
    passes client_proxy(), the delegation server passes nothing (direct)."""
    return opener(proxy).open(request, timeout=timeout)


def egress_hint(proxy=None):
    """One-line suffix for a network failure, naming the egress it used.
    Never contains the proxy URL itself — one can carry credentials."""
    if proxy:
        return " (egress: the proxyUrl set with `awewarm config proxy`; check that the proxy itself is reachable)"
    return (" (egress: direct — environment proxy variables are ignored; "
            "if this network needs a proxy: awewarm config proxy <url>)")
