import os
import unittest

from browser_session.commands import CommandError, normalize, normalize_all, public_args, resolve_secret
from browser_session.policy import PolicyError, check_url_shape, effective_domains, parse_domains


class Commands(unittest.TestCase):
    def bad(self, cmd, code):
        with self.assertRaises(CommandError) as ctx:
            normalize(cmd)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)

    def test_unknown_and_script_actions_are_refused(self):
        self.bad({"action": "evaluate", "script": "1"}, "unknown_action")
        self.bad({"action": "eval"}, "unknown_action")
        self.bad("goto", "invalid_command")

    def test_goto(self):
        self.assertEqual(normalize({"action": "goto", "url": " https://a.b "})["url"], "https://a.b")
        self.bad({"action": "goto"}, "missing_field")
        self.bad({"action": "goto", "url": "x", "wait_until": "never"}, "invalid_field")

    def test_targets(self):
        self.assertEqual(normalize({"action": "click", "text": "Sign in"})["target"], {"text": "Sign in"})
        self.assertEqual(normalize({"action": "click", "role": "button", "name": "Go"})["target"], {"role": "button", "name": "Go"})
        self.assertEqual(normalize({"action": "click", "x": 10, "y": 20})["target"], {"x": 10, "y": 20})
        self.bad({"action": "click"}, "missing_target")
        self.bad({"action": "click", "text": "a", "selector": "b"}, "invalid_target")
        self.bad({"action": "fill", "x": 1, "y": 2, "value": "v"}, "invalid_target")

    def test_fill_masks_secrets(self):
        cmd = normalize({"action": "fill", "label": "Password", "secret": "SITE_PASSWORD"})
        self.assertTrue(cmd["mask"])
        self.assertNotIn("value", cmd)
        self.bad({"action": "fill", "label": "x", "secret": "lower"}, "invalid_field")
        self.bad({"action": "fill", "label": "x", "value": "a", "secret": "A"}, "invalid_field")
        shown = public_args(normalize({"action": "fill", "label": "x", "value": "hunter2", "mask": True}))
        self.assertNotIn("hunter2", str(shown))

    def test_resolve_secret(self):
        os.environ["BROWSER_SECRET_T_ONE"] = "s3"
        self.assertEqual(resolve_secret("T_ONE"), "s3")
        with self.assertRaises(CommandError):
            resolve_secret("T_MISSING")

    def test_misc(self):
        self.assertEqual(normalize({"action": "wait"})["ms"], 1000)
        self.assertEqual(normalize({"action": "wait", "selector": "#a"})["state"], "visible")
        self.bad({"action": "wait", "ms": 999_999}, "invalid_field")
        self.bad({"action": "press", "key": "Enter;rm"}, "invalid_field")
        self.bad({"action": "screenshot", "name": "../x"}, "invalid_field")
        self.bad({"action": "extract", "what": "value"}, "missing_target")
        self.assertEqual(normalize({"action": "select", "selector": "s", "value": "a"})["values"], ["a"])

    def test_normalize_all(self):
        self.assertEqual(len(normalize_all({"action": "back"})), 1)
        with self.assertRaises(CommandError) as ctx:
            normalize_all([{"action": "back"}, {"action": "nope"}])
        self.assertIn("command 2", ctx.exception.message)
        with self.assertRaises(CommandError):
            normalize_all([{"action": "back"}] * 101)


class Policy(unittest.TestCase):
    def test_domains(self):
        self.assertEqual(parse_domains("Example.com, *.test.org"), ["example.com", "test.org"])
        with self.assertRaises(PolicyError):
            parse_domains(["http://x"])
        self.assertEqual(effective_domains([], ["*"]), ["*"])
        self.assertEqual(effective_domains(["a.example.com"], ["example.com"]), ["a.example.com"])
        with self.assertRaises(PolicyError):
            effective_domains(["other.com"], ["example.com"])

    def test_url_shape(self):
        check_url_shape("https://sub.example.com/x", ["example.com"])
        check_url_shape("about:blank", [])
        for url, code in [
            ("file:///etc/passwd", "scheme_not_allowed"),
            ("javascript:alert(1)", "scheme_not_allowed"),
            ("https://u:p@example.com", "invalid_url"),
            ("https://badexample.com", "domain_not_allowed"),
        ]:
            with self.assertRaises(PolicyError) as ctx:
                check_url_shape(url, ["example.com"])
            self.assertEqual(ctx.exception.code, code)


if __name__ == "__main__":
    unittest.main()
