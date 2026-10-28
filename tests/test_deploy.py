"""scripts/deploy.py(배포 실행기)와 배포본 코드 버전 기록 자동 테스트. 실제 서버에는 접속하지 않는다."""

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import deploy  # noqa: E402
import run_test  # noqa: E402

HAS_GIT = (ROOT / ".git").exists() and shutil.which("git")
HAS_BASH = shutil.which("bash")


def cfg(**kw):
    c = {"host": "h", "user": "u", "run_user": "u", "base_dir": "/opt/letshwp", "service": "letshwp-bot",
         "service_scope": "system", "python": "/usr/bin/python3", "keep_releases": 5}
    c.update(kw)
    return c


class ConfigTest(unittest.TestCase):
    def test_repo_config_valid(self):
        c = deploy.load_config()
        self.assertEqual(c["service"], "letshwp-bot")

    def test_invalid_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps(cfg(service_scope="docker")), encoding="utf-8")
            with self.assertRaises(deploy.DeployError):
                deploy.load_config(p)
            p.write_text(json.dumps(cfg(host="")), encoding="utf-8")
            with self.assertRaises(deploy.DeployError):
                deploy.load_config(p)

    def test_scope_commands(self):
        self.assertEqual(deploy.systemctl(cfg()), "sudo -n systemctl")
        self.assertEqual(deploy.systemctl(cfg(service_scope="user")), "systemctl --user")
        self.assertEqual(deploy.journalctl(cfg(service_scope="user")), "journalctl --user")

    def test_run_user_owns_shared(self):
        self.assertEqual(deploy.own(cfg()), ":")
        self.assertEqual(deploy.own(cfg(user="root", run_user="letshwp")),
                         "sudo -n chown -R letshwp: /opt/letshwp/shared")
        self.assertEqual(deploy.load_config()["run_user"], "letshwp")

    def test_release_name(self):
        import datetime
        self.assertEqual(deploy.release_name("abcdef1234", datetime.datetime(2026, 10, 28, 9, 5, 1)),
                         "20261028-090501_abcdef1")


@unittest.skipUnless(HAS_BASH, "bash 없음")
class RemoteScriptTest(unittest.TestCase):
    def bash_syntax_ok(self, script):
        p = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        return p.returncode == 0, p.stderr

    def test_generated_scripts_are_valid_bash(self):
        for c in (cfg(), cfg(service_scope="user", base_dir="/home/u/letshwp dir")):
            for s in (deploy.activate_script(c, "20261028-090501_abcdef1"), deploy.health_script(c),
                      deploy.set_env_script(c, "MM_URL")):
                ok, err = self.bash_syntax_ok(s)
                self.assertTrue(ok, err)

    def test_join_bot_script(self):
        c = cfg(mattermost={"container": "mm-1", "bot_username": "letshwp", "team": "letshwp",
                            "channels": ["town-square"]})
        s = deploy.join_bot_script(c)
        ok, err = self.bash_syntax_ok(s)
        self.assertTrue(ok, err)
        self.assertIn("docker exec mm-1 mmctl --local team users add letshwp letshwp", s)
        self.assertIn("channel users add letshwp:town-square letshwp", s)
        self.assertNotIn("token", s)
        with self.assertRaises(deploy.DeployError):
            deploy.join_bot_script(cfg())

    def test_activate_runs_tests_switches_and_rolls_back(self):
        s = deploy.activate_script(cfg(), "R1")
        self.assertIn("-m unittest discover -s tests", s)
        self.assertIn('rm -rf "$REL"; exit 3', s)
        self.assertIn("grep -E '^(Ran|OK|FAILED)' \"$REL/server_tests.log\"", s)
        self.assertIn('mv -Tf "$BASE/current.new" "$BASE/current"', s)
        self.assertIn("sudo -n systemctl restart letshwp-bot", s)
        self.assertIn('"connected": true', s)
        self.assertIn("ROLLED_BACK", s)
        self.assertIn("head -n -5", s)

    def test_set_env_script_replaces_and_appends(self):
        with tempfile.TemporaryDirectory() as d:
            c = cfg(base_dir=d, python=sys.executable)
            env = Path(d) / "shared" / "bot.env"
            env.parent.mkdir()
            env.write_text("MM_URL=http://a\nMM_TOKEN=\n", encoding="utf-8")
            for key, val in (("MM_TOKEN", "tok$en\\1"), ("MM_URL", "http://b"), ("NEW_KEY", "v")):
                p = subprocess.run(["bash", "-c", deploy.set_env_script(c, key)], input=val,
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn(val, p.stdout)
            self.assertEqual(env.read_text(encoding="utf-8"),
                             "MM_URL=http://b\nMM_TOKEN=tok$en\\1\nNEW_KEY=v\n")
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)


class MainTest(unittest.TestCase):
    def test_token_not_via_set_env(self):
        with mock.patch.object(deploy, "ssh") as ssh:
            self.assertEqual(deploy.main(["set-env", "MM_TOKEN=abc"]), 2)
            ssh.assert_not_called()

    def test_ssh_failure_exit_5(self):
        def boom(*a, **k):
            raise deploy.DeployError("서버 접속 실패", 5)
        with mock.patch.object(deploy, "ssh", boom):
            self.assertEqual(deploy.main(["status"]), 5)

    def test_deploy_stops_on_uncommitted_changes(self):
        with mock.patch.object(deploy, "git", return_value=(0, " M src/x.py", "")), \
                mock.patch.object(deploy, "ssh") as ssh:
            self.assertEqual(deploy.main(["deploy"]), 2)
            ssh.assert_not_called()

    def test_deploy_server_test_failure_exit_3(self):
        with mock.patch.object(deploy, "check_git", return_value={"commit": "a" * 40, "branch": "main"}), \
                mock.patch.object(deploy, "run_local_tests", return_value="OK"), \
                mock.patch.object(deploy, "build_package", return_value=b"tar"), \
                mock.patch.object(deploy, "ssh", side_effect=[(0, ""), (3, "SERVER_TESTS_FAILED")]):
            self.assertEqual(deploy.main(["deploy"]), 3)

    def test_deploy_health_failure_exit_4(self):
        with mock.patch.object(deploy, "check_git", return_value={"commit": "a" * 40, "branch": "main"}), \
                mock.patch.object(deploy, "run_local_tests", return_value="OK"), \
                mock.patch.object(deploy, "build_package", return_value=b"tar"), \
                mock.patch.object(deploy, "ssh", side_effect=[(0, ""), (4, "ROLLED_BACK x")]):
            self.assertEqual(deploy.main(["deploy"]), 4)


@unittest.skipUnless(HAS_GIT, "git 저장소가 아님(서버 배포본)")
class PackageTest(unittest.TestCase):
    def test_package_has_tracked_files_and_version(self):
        _, commit, _ = deploy.git("rev-parse", "HEAD")
        tar = deploy.build_package(commit, {"commit": commit, "release": "R"})
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            names = t.getnames()
            v = json.loads(t.extractfile("DEPLOY_VERSION.json").read())
        self.assertIn("scripts/run_fix.py", names)
        self.assertIn("src/formatter.py", names)
        self.assertFalse(any(n.startswith(("output/", "inbox/")) for n in names))
        self.assertEqual(v["commit"], commit)


class DeployVersionTest(unittest.TestCase):
    def test_git_info_falls_back_to_deploy_version(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "DEPLOY_VERSION.json").write_text(
                json.dumps({"commit": "abc1234def", "branch": "main", "deployed_at": "2026-10-28T09:00:00"}),
                encoding="utf-8")
            with mock.patch.object(run_test, "ROOT", Path(d)):
                info = run_test.git_info()
        self.assertEqual(info["commit"], "abc1234def")
        self.assertFalse(info["uncommitted_code_changes"])
        self.assertEqual(info["deployed_at"], "2026-10-28T09:00:00")


if __name__ == "__main__":
    unittest.main()
