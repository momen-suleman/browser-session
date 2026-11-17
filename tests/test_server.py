"""End-to-end tests against a real headless Chromium (skipped without one)."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from browser_session.server import create_app
from browser_session.session import Manager

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}

PAGE = """<!doctype html><title>Form</title>
<form action="/next" method="get">
  <label>Name <input name="name"></label>
  <label>Password <input name="pw" type="password"></label>
  <button type="submit">Send</button>
</form>
<a id="away" href="http://localhost:{port}/next">elsewhere</a>"""


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:
        return False


@unittest.skipUnless(chromium_available(), "Playwright Chromium is not installed")
class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["BROWSER_API_KEY"] = KEY
        self.tmp = Path(tempfile.mkdtemp())
        site = web.Application()

        async def index(request):
            return web.Response(text=PAGE.format(port=request.url.port), content_type="text/html")

        async def nxt(request):
            return web.Response(text=f"<title>Next</title><h1>Hello {request.query.get('name', '')}</h1>", content_type="text/html")

        site.router.add_get("/", index)
        site.router.add_get("/next", nxt)
        self.site = TestServer(site, host="127.0.0.1")
        await self.site.start_server()
        self.base = f"http://127.0.0.1:{self.site.port}"
        manager = Manager(self.tmp, max_sessions=2, idle_timeout=600, max_duration=600,
                          allow_private=True, allow_domains=["*"])
        self.client = TestClient(TestServer(create_app(manager)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def post(self, path, body, status=None):
        resp = await self.client.post(path, json=body, headers=AUTH)
        data = await resp.json()
        if status is not None:
            self.assertEqual(resp.status, status, data)
        return data

    async def test_auth_and_health(self):
        resp = await self.client.get("/")
        self.assertEqual((await resp.json())["ok"], True)
        resp = await self.client.get("/recordings")
        self.assertEqual(resp.status, 401)
        resp = await self.client.get("/recordings", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status, 401)

    async def test_one_shot_recording(self):
        data = await self.post("/sessions", {
            "label": "Form run",
            "start_url": self.base + "/",
            "allow_domains": ["127.0.0.1"],
            "commands": [
                {"action": "mark", "text": "Fill the form"},
                {"action": "fill", "label": "Name", "value": "Ada"},
                {"action": "fill", "label": "Password", "value": "hunter2"},
                {"action": "click", "role": "button", "name": "Send"},
                {"action": "wait", "load_state": "load"},
                {"action": "extract", "selector": "h1", "name": "greeting"},
                {"action": "screenshot", "name": "done", "full_page": True},
                {"action": "back"},
                {"action": "click", "selector": "#away"},
                {"action": "wait", "ms": 300},
                {"action": "note", "text": "the link to localhost must be blocked"},
            ],
            "close": True,
        }, 201)
        self.assertTrue(data["ok"], json.dumps(data["steps"], indent=1))
        extract = [s for s in data["steps"] if s["action"] == "extract"][0]
        self.assertEqual(extract["result"]["value"], "Hello Ada")
        self.assertEqual(data["extracted"], {"greeting": "Hello Ada"})
        self.assertIsNone(data["failed_step"])
        self.assertEqual(data["error_code"], "none")
        rec = data["recording"]
        self.assertEqual(rec["close_reason"], "requested")
        self.assertEqual(rec["video"], "video.webm")
        self.assertTrue(rec["blocked_navigations"], "the localhost link was not blocked")
        names = {f["name"]: f for f in rec["files"]}
        for name in ("video.webm", "trace.zip", "network.har", "report.md", "report.html", "steps.json", "shots/done.png", "steps/003.jpg"):
            self.assertIn(name, names)
        self.assertGreater(names["video.webm"]["bytes"], 0)

        report = (self.tmp / rec["id"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("Fill the form", report)
        self.assertNotIn("hunter2", report)
        self.assertNotIn("hunter2", (self.tmp / rec["id"] / "steps.json").read_text(encoding="utf-8"))
        self.assertNotIn("hunter2", (self.tmp / rec["id"] / "report.html").read_text(encoding="utf-8"))
        self.assertNotIn("hunter2", json.dumps(data["steps"]))
        self.assertTrue(rec["raw_files_may_contain_secrets"])

        resp = await self.client.get(f"/recordings/{rec['id']}/files/video.webm", headers={**AUTH, "Range": "bytes=0-99"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.headers["Content-Type"], "video/webm")
        for bad in ("../session.json", "manifest.json", "nope.txt"):
            resp = await self.client.get(f"/recordings/{rec['id']}/files/{bad}", headers=AUTH)
            self.assertEqual(resp.status, 404, bad)
        listing = await (await self.client.get("/recordings", headers=AUTH)).json()
        self.assertEqual(listing["recordings"][0]["id"], rec["id"])

    async def test_policy_refuses_start_url(self):
        data = await self.post("/sessions", {"start_url": "https://example.com", "allow_domains": ["127.0.0.1"]}, 400)
        self.assertEqual(data["error"]["code"], "domain_not_allowed")
        self.assertEqual(data["error_code"], "domain_not_allowed")
        data = await self.post("/sessions", {"commands": [{"action": "evaluate"}]}, 400)
        self.assertEqual(data["error"]["code"], "unknown_action")

    async def test_live_control_over_websocket(self):
        data = await self.post("/sessions", {"label": "Live", "start_url": self.base + "/"}, 201)
        sid = data["session"]["id"]
        async with self.client.ws_connect(f"/sessions/{sid}/live", headers=AUTH) as ws:
            first = await ws.receive_json(timeout=10)
            self.assertEqual(first["type"], "state")
            await ws.send_json({"type": "command", "id": "c1", "command": {"action": "fill", "label": "Name", "value": "Live"}})
            await ws.send_json({"type": "command", "id": "c2", "command": {"action": "press", "label": "Name", "key": "Enter"}})
            seen = {"frame": 0, "step": 0}
            results = {}
            while len(results) < 2:
                msg = await ws.receive_json(timeout=15)
                seen[msg["type"]] = seen.get(msg["type"], 0) + 1
                if msg["type"] == "result":
                    results[msg["id"]] = msg
            self.assertTrue(results["c1"]["ok"] and results["c2"]["ok"])
            await ws.send_json({"type": "command", "id": "c3", "command": {"action": "extract", "selector": "h1"}})
            while True:
                msg = await ws.receive_json(timeout=15)
                seen[msg["type"]] = seen.get(msg["type"], 0) + 1
                if msg["type"] == "result":
                    self.assertEqual(msg["steps"][0]["result"]["value"], "Hello Live")
                    break
            self.assertGreater(seen["frame"], 0, "no screencast frames arrived")
            await ws.send_json({"type": "close", "id": "end"})
            closed = None
            while closed is None:
                msg = await ws.receive(timeout=30)
                if msg.type.name != "TEXT":
                    break
                body = json.loads(msg.data)
                if body["type"] == "closed":
                    closed = body
        self.assertIsNotNone(closed)
        self.assertEqual(closed["recording"]["steps_total"], 4)
        resp = await self.client.get(f"/sessions/{sid}", headers=AUTH)
        self.assertEqual(resp.status, 404)

    async def test_orqen_contract(self):
        # Commands may come as JSON text (how an Orqen workflow passes a list).
        action = {"action": "session.start", "start_url": self.base + "/", "commands": json.dumps([{"action": "extract", "what": "title"}]), "close": True}
        data = await self.post("/", {"input": json.dumps(action)}, 200)
        result = json.loads(data["result"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"][-1]["result"]["value"], "Form")
        self.assertEqual(result["extracted"], {"step_2": "Form"})
        rid = result["recording"]["id"]
        got = json.loads((await self.post("/", {"input": json.dumps({"action": "recording.get", "recording_id": rid})}))["result"])
        self.assertEqual(got["recording"]["id"], rid)
        bad = json.loads((await self.post("/", {"input": json.dumps({"action": "run_js"})}))["result"])
        self.assertEqual(bad["error_code"], "unknown_action")

    async def test_session_limit(self):
        a = await self.post("/sessions", {}, 201)
        b = await self.post("/sessions", {}, 201)
        c = await self.post("/sessions", {}, 429)
        self.assertEqual(c["error"]["code"], "too_many_sessions")
        for s in (a, b):
            await self.post(f"/sessions/{s['session']['id']}/close", {}, 200)


class Recovery(unittest.TestCase):
    def test_interrupted_folder_gets_a_manifest(self):
        from browser_session import report

        root = Path(tempfile.mkdtemp())
        try:
            folder = root / "bs_20260101-000000-abcd1234"
            folder.mkdir()
            meta = {"id": folder.name, "label": "x", "start_url": "", "started_at": "2026-01-01T00:00:00Z",
                    "started_ms": 1_767_225_600_000, "viewport": {"width": 800, "height": 600}, "allow_domains": ["*"]}
            (folder / "session.json").write_text(json.dumps(meta))
            (folder / "steps.jsonl").write_text(json.dumps({"n": 1, "at_ms": 50, "action": "note", "args": {"text": "hi"}, "ok": True}) + "\n{broken")
            self.assertEqual(report.recover_interrupted(root, ""), [folder.name])
            manifest = report.load_manifest(root, folder.name)
            self.assertEqual(manifest["close_reason"], "interrupted")
            self.assertEqual(manifest["steps_total"], 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))
    unittest.main()
