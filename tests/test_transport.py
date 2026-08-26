import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import account_connection, plan_connection

from awewarm import transport

CLAUDE_CREDENTIALS = json.dumps(
    {"claudeOAuthAccessToken": {"accessToken": "sk-ant-oat01-test"}}
)
CODEX_AUTH = json.dumps({"tokens": {"access_token": "at", "refresh_token": "rt"}})


class ArgvBuilderTests(unittest.TestCase):
    def test_claude_argv_with_model(self):
        argv = transport.activation_argv(account_connection())
        self.assertEqual(argv, ["claude", "-p", "--model", "haiku", "Reply with exactly: ok"])

    def test_claude_argv_without_model(self):
        conn = account_connection()
        conn["activation"]["model"] = None
        self.assertEqual(
            transport.activation_argv(conn),
            ["claude", "-p", "Reply with exactly: ok"],
        )

    def test_codex_argv(self):
        conn = account_connection()
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        conn["activation"]["model"] = "gpt-5-codex"
        self.assertEqual(
            transport.activation_argv(conn),
            # Scheduled ticks run outside any git repo; exec refuses those
            # without --skip-git-repo-check.
            ["codex", "exec", "--skip-git-repo-check", "-m", "gpt-5-codex", "Reply with exactly: ok"],
        )

    def test_codex_argv_without_model(self):
        conn = account_connection()
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        conn["activation"]["model"] = None
        self.assertEqual(
            transport.activation_argv(conn),
            ["codex", "exec", "--skip-git-repo-check", "Reply with exactly: ok"],
        )

    def test_http_transport_returns_none(self):
        self.assertIsNone(transport.activation_argv(plan_connection()))


class HttpPartsTests(unittest.TestCase):
    def test_anthropic_messages(self):
        url, headers, body = transport.http_request_parts(plan_connection(), "tok")
        self.assertEqual(url, "https://open.bigmodel.cn/api/anthropic/v1/messages")
        self.assertEqual(headers["x-api-key"], "tok")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["max_tokens"], 4)
        self.assertEqual(body["messages"], [{"role": "user", "content": "Reply with exactly: ok"}])

    def test_anthropic_base_with_trailing_slash_and_v1(self):
        conn = plan_connection()
        conn["transport"]["baseUrl"] = "https://api.example.com/anthropic/v1/"
        url, _, _ = transport.http_request_parts(conn, "tok")
        self.assertEqual(url, "https://api.example.com/anthropic/v1/messages")

    def test_openai_chat(self):
        conn = plan_connection()
        conn["transport"]["kind"] = "openai-chat"
        conn["transport"]["baseUrl"] = "https://api.openai.com/v1"
        url, headers, body = transport.http_request_parts(conn, "tok")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertIn("messages", body)

    def test_openai_chat_base_without_v1(self):
        conn = plan_connection()
        conn["transport"]["kind"] = "openai-chat"
        conn["transport"]["baseUrl"] = "https://proxy.example.com/openai"
        url, _, _ = transport.http_request_parts(conn, "tok")
        self.assertEqual(url, "https://proxy.example.com/openai/v1/chat/completions")

    def test_openai_responses(self):
        conn = plan_connection()
        conn["transport"]["kind"] = "openai-responses"
        conn["transport"]["baseUrl"] = "https://api.openai.com/v1"
        url, headers, body = transport.http_request_parts(conn, "tok")
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(body["input"], "Reply with exactly: ok")
        self.assertEqual(body["max_output_tokens"], 4)
        self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_missing_model_dies(self):
        conn = plan_connection()
        conn["activation"]["model"] = None
        with self.assertRaises(SystemExit):
            transport.http_request_parts(conn, "tok")


class RedactTests(unittest.TestCase):
    def test_masks_secret_keys_recursively(self):
        view = {
            "auth": {"type": "api-key", "apiKeyRef": "keychain:awewarm/x"},
            "nested": [{"apiKey": "abc", "name": "glm"}],
        }
        out = transport.redact(view)
        self.assertEqual(out["auth"]["apiKeyRef"], "<redacted>")
        self.assertEqual(out["nested"][0]["apiKey"], "<redacted>")
        self.assertEqual(out["nested"][0]["name"], "glm")

    def test_empty_secret_left_as_is(self):
        out = transport.redact({"apiKeyRef": None})
        self.assertEqual(out["apiKeyRef"], None)


class ExtractErrorTests(unittest.TestCase):
    def test_json_error_message(self):
        body = b'{"error": {"message": "invalid api key"}}'
        self.assertEqual(transport._extract_error(body), "invalid api key")

    def test_plain_text_truncated(self):
        self.assertEqual(transport._extract_error(b"x" * 500), "x" * 200)

    def test_non_json_falls_back_to_text(self):
        self.assertEqual(transport._extract_error(b"gateway timeout"), "gateway timeout")


class ActivationEnvTests(unittest.TestCase):
    def test_no_credential_means_local_firing(self):
        # A locally-fired CLI reads its own login; no overlay is injected.
        self.assertEqual(transport.activation_env(account_connection(), None), {})

    def test_claude_token_extracted_into_the_env(self):
        env = transport.activation_env(account_connection(), CLAUDE_CREDENTIALS)
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"})

    def test_claude_unrecognized_shape_raises(self):
        with self.assertRaises(ValueError) as ctx:
            transport.activation_env(account_connection(), json.dumps({"no": "token"}))
        self.assertIn("not recognized", str(ctx.exception))

    def _codex_conn(self):
        conn = account_connection()
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        return conn

    def test_codex_materializes_auth_json_into_the_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = transport.activation_env(self._codex_conn(), CODEX_AUTH, sandbox_root=tmp, conn_id="codex-1")
            self.assertEqual(env, {"CODEX_HOME": str(Path(tmp) / "codex-1")})
            auth = Path(tmp) / "codex-1" / "auth.json"
            self.assertEqual(auth.read_text(), CODEX_AUTH)
            if sys.platform != "win32":  # Windows has no POSIX modes; st_mode always reads 0o666
                self.assertEqual(auth.stat().st_mode & 0o777, 0o600)

    def test_codex_rematerialization_discards_server_side_refresh(self):
        # The CLI may refresh tokens into its sandbox; the next fire rewrites
        # auth.json from the pushed credential, so the local login wins.
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._codex_conn()
            transport.activation_env(conn, CODEX_AUTH, sandbox_root=tmp, conn_id="codex-1")
            (Path(tmp) / "codex-1" / "auth.json").write_text('{"tokens": {"rotated": true}}')
            transport.activation_env(conn, CODEX_AUTH, sandbox_root=tmp, conn_id="codex-1")
            self.assertEqual((Path(tmp) / "codex-1" / "auth.json").read_text(), CODEX_AUTH)

    def test_codex_without_a_sandbox_is_an_internal_error(self):
        with self.assertRaises(ValueError):
            transport.activation_env(self._codex_conn(), CODEX_AUTH)

    def test_remove_sandbox_deletes_only_that_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            for conn_id in ("codex-1", "codex-2"):
                transport.activation_env(self._codex_conn(), CODEX_AUTH, sandbox_root=tmp, conn_id=conn_id)
            transport.remove_sandbox(tmp, "codex-1")
            self.assertFalse((Path(tmp) / "codex-1").exists())
            self.assertTrue((Path(tmp) / "codex-2").exists())


class SendCliEnvTests(unittest.TestCase):
    @mock.patch("awewarm.transport.subprocess.run")
    @mock.patch("awewarm.transport.shutil.which", return_value="/usr/local/bin/claude")
    def test_delegated_credential_layers_over_our_environment(self, which, run):
        run.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        result = transport.send_activation(
            account_connection(), credential=CLAUDE_CREDENTIALS, conn_id="claude"
        )
        self.assertTrue(result["ok"])
        env = run.call_args[1]["env"]
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-test")
        # an overlay, not a replacement: the rest of the environment survives
        self.assertEqual(env["PATH"], os.environ["PATH"])

    @mock.patch("awewarm.transport.subprocess.run")
    @mock.patch("awewarm.transport.shutil.which", return_value="/usr/local/bin/claude")
    def test_local_firing_passes_no_env(self, which, run):
        run.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        transport.send_activation(account_connection())
        self.assertIsNone(run.call_args[1]["env"])

    def test_unrecognized_credential_is_a_failure_not_a_crash(self):
        result = transport.send_activation(
            account_connection(), credential=json.dumps({"no": "token"}), conn_id="claude"
        )
        self.assertFalse(result["ok"])
        self.assertIn("not recognized", result["detail"])
        self.assertIn("awewarm remote push", result["detail"])


class SendCliTests(unittest.TestCase):
    def _conn(self):
        return account_connection()

    @mock.patch("awewarm.transport.subprocess.run")
    @mock.patch("awewarm.transport.shutil.which", return_value="/usr/local/bin/claude")
    def test_success(self, which, run):
        run.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        result = transport.send_activation(self._conn())
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "ok")
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "/usr/local/bin/claude")
        # The CLIs append piped stdin to the prompt; a headless tick must not
        # let them read (and block on) awewarm's own stdin.
        self.assertEqual(run.call_args[1]["stdin"], subprocess.DEVNULL)

    @mock.patch("awewarm.transport.subprocess.run")
    @mock.patch("awewarm.transport.shutil.which", return_value="/usr/local/bin/claude")
    def test_failure_reports_stderr(self, which, run):
        run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom\n")
        result = transport.send_activation(self._conn())
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "boom")

    @mock.patch("awewarm.transport.shutil.which", return_value=None)
    def test_missing_cli(self, which):
        result = transport.send_activation(self._conn())
        self.assertFalse(result["ok"])
        self.assertIn("not found in PATH", result["detail"])

    @mock.patch("awewarm.transport.subprocess.run")
    @mock.patch("awewarm.transport.shutil.which")
    def test_ps1_cli_routed_through_powershell(self, which, run):
        # CreateProcess cannot run .ps1 scripts directly (PowerShell installs
        # on Windows); they must go through powershell -File.
        def fake_which(cmd):
            if cmd == "claude":
                return "C:\\bin\\claude.ps1"
            return "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"

        which.side_effect = fake_which
        run.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        result = transport.send_activation(self._conn())
        self.assertTrue(result["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(
            argv,
            ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
             "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", "C:\\bin\\claude.ps1",
             "-p", "--model", "haiku", "Reply with exactly: ok"],
        )


class SendHttpTests(unittest.TestCase):
    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_success(self, urlopen):
        urlopen.return_value.__enter__ = lambda self: io.BytesIO(b"{}")
        urlopen.return_value.__exit__ = mock.Mock(return_value=False)
        result = transport.send_activation(plan_connection(), api_key="tok")
        self.assertTrue(result["ok"])
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://open.bigmodel.cn/api/anthropic/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "tok")

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_timeout_seconds_passed_through(self, urlopen):
        urlopen.return_value.__enter__ = lambda self: io.BytesIO(b"{}")
        urlopen.return_value.__exit__ = mock.Mock(return_value=False)
        transport.send_activation(plan_connection(), api_key="tok", timeout_seconds=15)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15)

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_timeout_defaults_to_sixty_seconds(self, urlopen):
        urlopen.return_value.__enter__ = lambda self: io.BytesIO(b"{}")
        urlopen.return_value.__exit__ = mock.Mock(return_value=False)
        transport.send_activation(plan_connection(), api_key="tok")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], transport.HTTP_TIMEOUT_SECONDS)

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_http_error_extracted(self, urlopen):
        error = transport.urllib.error.HTTPError(
            "url", 401, "Unauthorized", None, io.BytesIO(b'{"error":{"message":"bad key"}}')
        )
        urlopen.side_effect = error
        result = transport.send_activation(plan_connection(), api_key="tok")
        self.assertFalse(result["ok"])
        self.assertIn("401", result["detail"])
        self.assertIn("bad key", result["detail"])

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_url_error(self, urlopen):
        urlopen.side_effect = transport.urllib.error.URLError("connection refused")
        result = transport.send_activation(plan_connection(), api_key="tok")
        self.assertFalse(result["ok"])
        self.assertIn("connection refused", result["detail"])

    def test_subscription_without_api_key_dies(self):
        with self.assertRaises(SystemExit):
            transport.send_activation(plan_connection(), api_key=None)


if __name__ == "__main__":
    unittest.main()


class EndpointUrlTests(unittest.TestCase):
    def test_versioned_base_appends_endpoint_directly(self):
        # GLM's coding endpoint is versioned /v4 — must not gain an extra /v1.
        url = transport.endpoint_url("https://open.bigmodel.cn/api/coding/paas/v4", "/chat/completions")
        self.assertEqual(url, "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")

    def test_bare_host_gains_v1(self):
        url = transport.endpoint_url("https://api.anthropic.com", "/messages")
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")

    def test_v1_base_appends_endpoint_directly(self):
        url = transport.endpoint_url("https://api.openai.com/v1", "/responses")
        self.assertEqual(url, "https://api.openai.com/v1/responses")


CODEX_NATIVE_AUTH = json.dumps(
    {"tokens": {"access_token": "at", "account_id": "acc-1", "refresh_token": "rt"}}
)


def native_codex_connection(model=None, base_url=None):
    conn = account_connection()
    conn["transport"] = {"kind": "codex-cli", "baseUrl": base_url, "cliCommand": "codex"}
    conn["activation"]["model"] = model
    return conn


class NativePartsTests(unittest.TestCase):
    def test_codex_defaults_speak_the_cli_protocol(self):
        url, headers, body = transport.native_request_parts(
            native_codex_connection(), CODEX_NATIVE_AUTH
        )
        self.assertEqual(url, "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(headers["Authorization"], "Bearer at")
        self.assertEqual(headers["chatgpt-account-id"], "acc-1")
        self.assertEqual(headers["OpenAI-Beta"], "responses=experimental")
        self.assertEqual(headers["originator"], "codex_cli_rs")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
        self.assertEqual(
            body["input"][0]["content"][0]["text"], "Reply with exactly: ok"
        )

    def test_codex_honors_model_and_base_url_overrides(self):
        conn = native_codex_connection(model="gpt-5.6-terra", base_url="https://relay.example/codex")
        url, headers, body = transport.native_request_parts(conn, CODEX_NATIVE_AUTH)
        self.assertEqual(url, "https://relay.example/codex/responses")
        self.assertEqual(body["model"], "gpt-5.6-terra")

    def test_claude_defaults_use_the_oauth_bearer_shape(self):
        conn = account_connection()
        conn["activation"]["model"] = None
        url, headers, body = transport.native_request_parts(conn, CLAUDE_CREDENTIALS)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["Authorization"], "Bearer sk-ant-oat01-test")
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["model"], "claude-sonnet-5")
        self.assertEqual(body["max_tokens"], 4)
        self.assertEqual(body["messages"], [{"role": "user", "content": "Reply with exactly: ok"}])

    def test_non_account_transport_cannot_fire_natively(self):
        with self.assertRaises(ValueError):
            transport.native_request_parts(plan_connection(), "sk-test")


class _BrokenStream:
    """A 200 whose stream dies mid-read — the request still happened."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def readline(self):
        raise OSError("stream reset")


class SendNativeTests(unittest.TestCase):
    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_codex_success_reads_the_sse_stream(self, urlopen):
        urlopen.return_value = io.BytesIO(
            b'event: response.created\ndata: {"type":"response.created"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        )
        result = transport.send_native(native_codex_connection(), CODEX_NATIVE_AUTH, 15)
        self.assertTrue(result["ok"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_header("Authorization"), "Bearer at")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "acc-1")
        self.assertEqual(request.get_header("Openai-beta"), "responses=experimental")

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_codex_response_failed_is_a_failure_with_its_message(self, urlopen):
        urlopen.return_value = io.BytesIO(
            b'event: response.failed\n'
            b'data: {"type":"response.failed","response":{"error":{"message":"usage limit reached"}}}\n\n'
        )
        result = transport.send_native(native_codex_connection(), CODEX_NATIVE_AUTH)
        self.assertFalse(result["ok"])
        self.assertIn("usage limit reached", result["detail"])

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_read_error_after_a_200_is_still_a_success(self, urlopen):
        # A 200 means the provider accepted the request; a dying stream must
        # not record a phantom failure that invites duplicate retries.
        urlopen.return_value = _BrokenStream()
        result = transport.send_native(native_codex_connection(), CODEX_NATIVE_AUTH)
        self.assertTrue(result["ok"])

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_claude_success_reads_the_json_body(self, urlopen):
        urlopen.return_value = io.BytesIO(b'{"id":"msg_1"}')
        result = transport.send_native(account_connection(), CLAUDE_CREDENTIALS)
        self.assertTrue(result["ok"])
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("Anthropic-beta"), "oauth-2025-04-20")

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_rejected_credential_points_at_a_repush(self, urlopen):
        error = transport.urllib.error.HTTPError(
            "url", 401, "Unauthorized", None, io.BytesIO(b'{"detail":"bad token"}')
        )
        urlopen.side_effect = error
        result = transport.send_native(native_codex_connection(), CODEX_NATIVE_AUTH)
        self.assertFalse(result["ok"])
        self.assertIn("credential rejected (HTTP 401)", result["detail"])
        self.assertIn("awewarm remote push", result["detail"])

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_provider_error_passes_through(self, urlopen):
        error = transport.urllib.error.HTTPError(
            "url", 400, "Bad Request", None,
            io.BytesIO(b'{"detail":"the model is not supported with a ChatGPT account"}'),
        )
        urlopen.side_effect = error
        result = transport.send_native(native_codex_connection(), CODEX_NATIVE_AUTH)
        self.assertFalse(result["ok"])
        self.assertIn("HTTP 400", result["detail"])
        self.assertIn("not supported", result["detail"])

    @mock.patch("awewarm.transport.urllib.request.urlopen")
    def test_network_failure(self, urlopen):
        urlopen.side_effect = transport.urllib.error.URLError("connection refused")
        result = transport.send_native(native_codex_connection(), CODEX_NATIVE_AUTH)
        self.assertFalse(result["ok"])
        self.assertIn("connection refused", result["detail"])

    def test_unrecognized_credential_is_a_failure_not_a_crash(self):
        result = transport.send_native(native_codex_connection(), '{"tokens": {}}')
        self.assertFalse(result["ok"])
        self.assertIn("not recognized", result["detail"])
