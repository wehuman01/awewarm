import io
import subprocess
import unittest
from unittest import mock

from helpers import account_connection, plan_connection

from awewarm import transport


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
