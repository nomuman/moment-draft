import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException
from waitress.proxy_headers import proxy_headers_middleware

from moment_draft.__main__ import main
from moment_draft.admission import AdmissionLimits, register_admission
from moment_draft.config import Config
from moment_draft.projects import Problem, Projects


CODE = "R7xQz2Kp9Wm4Nv8Ts6Yh3Df5Jb1Lc0Ua"
ORIGIN = "https://trial.example.org"


class PublicConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(Path("unused"), public_trial=True, origin=ORIGIN, access_code=CODE, trusted_proxy="127.0.0.1")

    def test_explicit_public_configuration(self):
        self.config.bind("127.0.0.1", 8770)
        self.config.bind("0.0.0.0", 8770)
        self.assertNotIn(CODE, repr(self.config))

    def test_incomplete_or_conflicting_public_settings_refused(self):
        for changes in (
            {"access_code": ""}, {"access_code": "a" * 32}, {"access_code": "example" + CODE},
            {"trusted_proxy": ""}, {"trusted_proxy": "*"}, {"trusted_proxy": "10.0.0.0/8"},
            {"trusted_proxy": "0.0.0.0"}, {"trusted_proxy": "localhost"},
            {"origin": "http://trial.example.org"}, {"origin": "https://127.0.0.1"},
            {"origin": ORIGIN + "/"}, {"origin": ORIGIN + "?x=1"},
            {"origin": "https://user@trial.example.org"}, {"origin": "https://TRIAL.example.org"},
            {"origin": ORIGIN + ":8443"}, {"origin": ORIGIN + ":443"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.config, **changes).validate()
        with self.assertRaises(ValueError):
            self.config.bind("0.0.0.0", 8770, container_local=True)

    def test_code_alone_never_enables_public(self):
        config = Config.environment({"MOMENT_ACCESS_CODE": CODE})
        self.assertFalse(config.public_trial)
        with self.assertRaises(ValueError):
            config.bind("0.0.0.0", 8770)
        config.bind("127.0.0.1", 8770)

    def test_environment_days_and_flags_are_bounded(self):
        for value in ("0", "8", "-1", "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config.environment({"MOMENT_RETENTION_DAYS": value})
        for flag in ("MOMENT_PUBLIC_TRIAL", "MOMENT_CONTAINER_LOCAL"):
            with self.assertRaises(ValueError):
                Config.environment({flag: "true"})
        replace(self.config, retention_seconds=.01).validate()


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.projects = Projects(Config(Path(directory.name), public_trial=True, origin=ORIGIN, access_code=CODE, trusted_proxy="127.0.0.1", free_floor=0))
        self.addCleanup(self.projects.close)
        self.app = Flask(__name__)
        register_admission(self.app, self.projects)
        self.app.add_url_rule("/", endpoint="index", view_func=lambda: "editor")
        self.app.add_url_rule("/api/session", endpoint="session", view_func=lambda: "session")
        self.app.add_url_rule("/healthz", endpoint="health", view_func=lambda: "ok")
        self.app.register_error_handler(Problem, lambda exc: (jsonify(error=str(exc)), exc.status))
        self.app.register_error_handler(HTTPException, lambda exc: (jsonify(error="invalid"), exc.code))
        self.client = self.app.test_client()

    def admit(self, client=None, **kwargs):
        return (client or self.client).post("/api/admission", base_url=ORIGIN, json={"code": CODE}, headers={"Origin": ORIGIN}, **kwargs)

    def test_no_session_or_private_bootstrap_before_admission(self):
        self.assertIn("アクセスコード".encode(), self.client.get("/", base_url=ORIGIN, buffered=True).data)
        for route in ("/api/session", "/api/projects", "/static/app.js"):
            self.assertEqual(self.client.get(route, base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.projects.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

        self.assertEqual(self.client.get("/static/admission.js", base_url=ORIGIN, buffered=True).status_code, 200)

    def test_success_issues_secure_cookie_and_restores_owner(self):
        response = self.admit()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["Set-Cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=Strict"):
            self.assertIn(flag, cookie)
        self.assertNotIn(CODE, response.get_data(as_text=True))
        self.assertEqual(self.client.get("/", base_url=ORIGIN, buffered=True).data, b"editor")
        self.assertEqual(self.admit().status_code, 200)
        self.assertEqual(self.projects.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(self.admit(self.app.test_client()).status_code, 200)
        self.assertEqual(self.projects.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 2)

    def test_invitation_navigation_opens_admission_without_creating_session(self):
        for site in ("cross-site", "same-site"):
            with self.subTest(site=site):
                response = self.client.get("/", base_url=ORIGIN, headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "navigate"}, buffered=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("アクセスコード".encode(), response.data)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertEqual(self.client.get("/api/session", base_url=ORIGIN, headers={"Sec-Fetch-Site": site}).status_code, 403)
        self.assertEqual(self.projects.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.admit().status_code, 200)
        response = self.client.get("/", base_url=ORIGIN, headers={"Sec-Fetch-Site": "cross-site"}, buffered=True)
        self.assertIn("アクセスコード".encode(), response.data)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_bad_origin_host_fetch_metadata_and_insecure_transport(self):
        for headers in ({}, {"Origin": "https://other.example.org"}, {"Origin": ORIGIN, "Host": "other.example.org"}, {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}, {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.post("/api/admission", base_url=ORIGIN, json={"code": CODE}, headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/healthz", base_url="http://trial.example.org", headers={"X-Forwarded-Proto": "https"}).status_code, 403)

    def test_body_limit_declared_and_streamed(self):
        response = self.client.post("/api/admission", base_url=ORIGIN, data=b"x" * 1025, content_type="application/json", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 413)
        response = self.client.open("/api/admission", method="POST", base_url=ORIGIN, content_type="application/json", headers={"Origin": ORIGIN}, environ_overrides={"wsgi.input": io.BytesIO(b" " * 1025), "wsgi.input_terminated": True, "CONTENT_LENGTH": ""})
        self.assertEqual(response.status_code, 413)

    def test_wrong_code_is_backed_off(self):
        first = self.client.post("/api/admission", base_url=ORIGIN, json={"code": "wrong"}, headers={"Origin": ORIGIN})
        self.assertEqual(first.status_code, 401)
        second = self.admit()
        self.assertEqual(second.status_code, 429)
        self.assertGreater(int(second.headers["Retry-After"]), 0)

    def test_waitress_only_known_proxy_can_supply_scheme(self):
        self.app.wsgi_app = proxy_headers_middleware(self.app.wsgi_app, trusted_proxy="127.0.0.1", trusted_proxy_count=1, trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"}, clear_untrusted=True)
        for peer, expected in (("192.0.2.5", 403), ("127.0.0.1", 200)):
            response = self.client.get("/", base_url="http://trial.example.org", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.10", "X-Forwarded-Host": "evil.example.org"}, environ_overrides={"REMOTE_ADDR": peer}, buffered=True)
            self.assertEqual(response.status_code, expected)


class AdmissionLimitTests(unittest.TestCase):
    def test_global_and_address_caps_recover_with_bounded_state(self):
        now = [0]
        limits = AdmissionLimits(clock=lambda: now[0])
        for index in range(100):
            self.assertEqual(limits.attempt(str(index), True), 0)
        self.assertEqual(limits.attempt("new", True), 60)
        now[0] = 61
        for _ in range(5):
            self.assertEqual(limits.attempt("one", True), 0)
        self.assertGreater(limits.attempt("one", True), 0)
        for minute in range(1, 20):
            now[0] += 61
            for index in range(100):
                limits.attempt(f"{minute}-{index}", False)
        self.assertLessEqual(len(limits.clients), 500)


class PublicStartupTests(unittest.TestCase):
    def test_public_startup_refuses_an_app_without_admission_hook(self):
        env = {"MOMENT_PUBLIC_TRIAL": "1", "MOMENT_ORIGIN": ORIGIN,
               "MOMENT_ACCESS_CODE": CODE, "MOMENT_TRUSTED_PROXY": "127.0.0.1"}
        projects = Mock()
        with patch.dict("os.environ", env, clear=True), patch("sys.argv", ["moment_draft"]), patch("moment_draft.__main__.Projects", return_value=projects), patch("moment_draft.__main__.create_app", return_value=Flask(__name__)), patch("moment_draft.__main__.create_server") as create_server:
            with self.assertRaises(RuntimeError):
                main()
        projects.close.assert_called_once()
        create_server.assert_not_called()
