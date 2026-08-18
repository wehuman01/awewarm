import os
import plistlib
import unittest
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import install


def ok_run(returncode=0, stderr=""):
    return mock.Mock(returncode=returncode, stdout="", stderr=stderr)


class BuildPlistTests(IsolatedTestCase):
    def test_plist_shape(self):
        plist = install.build_plist("/usr/local/bin/awewarm")
        self.assertEqual(plist["Label"], install.LABEL)
        self.assertEqual(plist["ProgramArguments"], ["/usr/local/bin/awewarm", "run"])
        self.assertEqual(plist["StartInterval"], 60)
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["StandardOutPath"].endswith("launchd.log"))

    def test_plist_passes_awewarm_env_through(self):
        plist = install.build_plist("/usr/local/bin/awewarm")
        env = plist["EnvironmentVariables"]
        self.assertEqual(
            {key: value for key, value in env.items() if key.startswith("AWEWARM_")},
            {
                "AWEWARM_CONFIG": str(self.tmp_path / "config.json"),
                "AWEWARM_STATE": str(self.tmp_path / "state.json"),
                "AWEWARM_LOG": str(self.tmp_path / "awewarm.log"),
                "AWEWARM_PLIST": str(self.tmp_path / "agent.plist"),
            },
        )

    def test_plist_carries_path_for_cli_resolution(self):
        # launchd's default PATH lacks user-local install dirs; the installer
        # must propagate the current PATH or CLIs like `claude` never resolve.
        plist = install.build_plist("/usr/local/bin/awewarm")
        self.assertEqual(plist["EnvironmentVariables"].get("PATH"), os.environ["PATH"])

    def test_plist_path_env_override(self):
        self.assertEqual(install.plist_path(), self.tmp_path / "agent.plist")


class InstallTests(IsolatedTestCase):
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value="/usr/local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_install_writes_plist_and_bootstraps(self, run, which):
        plist = install.install_scheduler()
        self.assertTrue(plist.exists())
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["Label"], install.LABEL)
        self.assertTrue(install.scheduler_installed())
        argvs = [call[0][0] for call in run.call_args_list]
        self.assertTrue(any("bootout" in argv for argv in argvs))
        self.assertTrue(any("bootstrap" in argv for argv in argvs))

    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value=None)
    def test_install_without_entry_point_dies(self, which):
        with self.assertRaises(SystemExit):
            install.install_scheduler()

    @mock.patch("awewarm.install.sys.platform", "linux")
    def test_install_refuses_non_macos(self):
        with self.assertRaises(SystemExit):
            install.install_scheduler()

    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value="/usr/local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run")
    def test_install_falls_back_to_legacy_load(self, run, which):
        run.side_effect = [
            ok_run(),  # bootout
            ok_run(returncode=1, stderr="bootstrap failed"),  # bootstrap
            ok_run(),  # legacy load
        ]
        plist = install.install_scheduler()
        self.assertTrue(plist.exists())

    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.shutil.which", return_value="/usr/local/bin/awewarm")
    @mock.patch("awewarm.install.subprocess.run")
    def test_install_all_launchctl_failures_die(self, run, which):
        run.side_effect = [
            ok_run(),
            ok_run(returncode=1, stderr="bootstrap failed"),
            ok_run(returncode=1, stderr="load failed"),
        ]
        with self.assertRaises(SystemExit):
            install.install_scheduler()


class UninstallTests(IsolatedTestCase):
    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_uninstall_removes_plist(self, run):
        plist = install.plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(b"placeholder")
        self.assertTrue(install.uninstall_scheduler())
        self.assertFalse(plist.exists())
        self.assertFalse(install.scheduler_installed())

    @mock.patch("awewarm.install.sys.platform", "darwin")
    @mock.patch("awewarm.install.subprocess.run", return_value=ok_run())
    def test_uninstall_when_absent(self, run):
        self.assertFalse(install.uninstall_scheduler())


if __name__ == "__main__":
    unittest.main()
