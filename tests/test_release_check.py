import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.release_check import FREE_FLOOR, evaluate


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        os.chmod(self.directory.name, 0o700)
        self.env = {
            "MOMENT_DATA_DIR": self.directory.name,
            "MOMENT_FFMPEG": sys.executable,
            "MOMENT_HOST": "127.0.0.1",
            "MOMENT_ORIGIN": "http://127.0.0.1:8770",
            "MOMENT_PORT": "8770",
        }

    def check(self, mode="local"):
        with patch("scripts.release_check.shutil.disk_usage", return_value=SimpleNamespace(free=FREE_FLOOR)):
            return {item.name: item for item in evaluate(self.env, mode)}

    def test_private_local_configuration_passes_without_writing_storage(self):
        checks = self.check()
        self.assertTrue(all(item.status == "pass" for item in checks.values()))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_container_bind_requires_explicit_local_exception(self):
        self.env["MOMENT_HOST"] = "0.0.0.0"
        self.assertEqual(self.check()["bind"].status, "fail")
        self.env["MOMENT_CONTAINER_LOCAL"] = "1"
        self.assertEqual(self.check()["bind"].status, "pass")

    def test_public_config_needs_https_and_admission_and_remains_pending(self):
        self.env.update({"MOMENT_HOST": "0.0.0.0", "MOMENT_ORIGIN": "https://example.org", "MOMENT_ACCESS_CODE": "R7xQz2Kp9Wm4Nv8Ts6Yh3Df5Jb1Lc0Ua", "MOMENT_PUBLIC_TRIAL": "1", "MOMENT_TRUSTED_PROXY": "127.0.0.1"})
        checks = self.check("public")
        self.assertEqual(checks["origin"].status, "pass")
        self.assertEqual(checks["admission"].status, "pass")
        self.assertEqual(checks["live_ingress"].status, "pending")
        self.assertNotIn("secret-value", repr(checks))

    def test_public_mode_cannot_pass_without_runtime_flags_or_with_conflicting_flags(self):
        self.env.update({"MOMENT_ORIGIN": "https://trial.example.org", "MOMENT_ACCESS_CODE": "R7xQz2Kp9Wm4Nv8Ts6Yh3Df5Jb1Lc0Ua"})
        self.assertEqual(self.check("public")["implementation"].status, "fail")
        self.env.update({"MOMENT_PUBLIC_TRIAL": "1", "MOMENT_TRUSTED_PROXY": "127.0.0.1"})
        self.assertEqual(self.check("public")["implementation"].status, "pass")
        self.assertEqual(self.check("local")["implementation"].status, "fail")
        self.env["MOMENT_CONTAINER_LOCAL"] = "1"
        self.assertEqual(self.check("public")["implementation"].status, "fail")

    def test_storage_and_space_fail_closed(self):
        os.chmod(self.directory.name, 0o755)
        with patch("scripts.release_check.shutil.disk_usage", return_value=SimpleNamespace(free=FREE_FLOOR - 1)):
            checks = {item.name: item for item in evaluate(self.env, "local")}
        self.assertEqual(checks["storage"].status, "fail")
        self.assertEqual(checks["free_space"].status, "fail")

    def test_public_origin_rejects_ip_literal(self):
        self.env["MOMENT_ORIGIN"] = "https://127.0.0.2"
        self.assertEqual(self.check("public")["origin"].status, "fail")

    def test_storage_rejects_owner_without_write_access(self):
        os.chmod(self.directory.name, 0o500)
        self.addCleanup(os.chmod, self.directory.name, 0o700)
        self.assertEqual(self.check()["storage"].status, "fail")


if __name__ == "__main__":
    unittest.main()
