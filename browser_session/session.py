"""One recorded browser session, and the manager that owns all of them.

A session is a Playwright browser context with video, trace and HAR
recording on. Commands run one at a time (a lock), each becomes a step in
`steps.jsonl` (written as it happens, so a crash keeps the log), with a
screenshot after it. Live viewers subscribe to events: screencast frames
(Chrome DevTools `Page.startScreencast`, only while someone watches), steps
and state changes. Closing the session finalizes the recording.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote, quote_plus

from . import commands as cmdlib
from . import report
from .policy import PolicyError, check_url, effective_domains, parse_domains

ID_PREFIX = "bs_"


def new_id() -> str:
    return f"{ID_PREFIX}{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"


def valid_id(value: str) -> bool:
    if not value.startswith(ID_PREFIX) or len(value) > 64:
        return False
    return all(c.isalnum() or c in "-_" for c in value)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


class SessionError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Session:
    def __init__(self, manager: "Manager", options: dict):
        self.manager = manager
        self.id = new_id()
        self.dir: Path = manager.root / self.id
        self.label = options["label"]
        self.start_url = options["start_url"]
        self.viewport = options["viewport"]
        self.domains = options["allow_domains"]
        self.step_screenshots = options["step_screenshots"]
        self.state = "starting"
        self.started_ms = now_ms()
        self.last_activity = time.monotonic()
        self.steps: list[dict] = []
        self.blocked: list[dict] = []
        self.lock = asyncio.Lock()
        self.subscribers: set[asyncio.Queue] = set()
        self.context = None
        self.page = None
        self.pages: list = []
        self.cdp = None
        self.cdp_page = None
        self.last_frame: str | None = None
        self.manifest: dict | None = None
        self._pending_mask = 0
        self._masked: set[str] = set()

    # ------------------------------------------------------------ start --

    async def start(self, browser) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "steps").mkdir()
        (self.dir / "video").mkdir()
        self.context = await browser.new_context(
            viewport=self.viewport,
            record_video_dir=str(self.dir / "video"),
            record_video_size=self.viewport,
            record_har_path=str(self.dir / "network.har"),
            record_har_content="omit",
            accept_downloads=False,
            service_workers="block",
        )
        self.context.set_default_timeout(cmdlib.DEFAULT_TIMEOUT_MS)
        await self.context.tracing.start(screenshots=True, snapshots=True, title=self.label)
        await self.context.route("**/*", self._guard)
        self.context.on("page", self._on_page)
        page = await self.context.new_page()
        self._on_page(page)
        self.state = "running"
        self._write_meta()
        if self.start_url:
            await self.run([cmdlib.normalize({"action": "goto", "url": self.start_url})])

    def _on_page(self, page) -> None:
        if page in self.pages:
            return
        self.pages.append(page)
        previous = self.page
        self.page = page
        page.on("close", lambda p: self._on_page_close(p))
        if previous is not None:
            self._event({"type": "page", "message": "a new tab opened; commands now go to it"})
            if self.subscribers:
                asyncio.ensure_future(self._restart_screencast())

    def _on_page_close(self, page) -> None:
        if page is self.page:
            alive = [p for p in self.pages if not p.is_closed()]
            self.page = alive[-1] if alive else None
            if self.page is not None and self.subscribers:
                asyncio.ensure_future(self._restart_screencast())

    async def _guard(self, route) -> None:
        request = route.request
        if request.resource_type == "document":
            try:
                await check_url(request.url, self.domains, self.manager.allow_private)
            except PolicyError as err:
                entry = {"at_ms": now_ms() - self.started_ms, "url": self._scrub(request.url[:500]), "code": err.code}
                self.blocked.append(entry)
                self._event({"type": "blocked", **entry, "message": err.message})
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    # ------------------------------------------------------------- live --

    def _event(self, event: dict) -> None:
        for queue in list(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=24)
        self.subscribers.add(queue)
        # A viewer joining mid-session gets the steps so far (the last 200).
        queue.put_nowait({"type": "state", "session": self.public(), "steps": self.steps[-200:]})
        if self.last_frame:
            queue.put_nowait({"type": "frame", "data": self.last_frame})
        if len(self.subscribers) == 1:
            await self._restart_screencast()
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)
        if not self.subscribers:
            await self._stop_screencast()

    async def _stop_screencast(self) -> None:
        cdp, self.cdp, self.cdp_page = self.cdp, None, None
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            except Exception:
                pass

    async def _restart_screencast(self) -> None:
        if self.state != "running" or self.page is None or self.page is self.cdp_page:
            return
        await self._stop_screencast()
        page = self.page
        try:
            cdp = await self.context.new_cdp_session(page)
        except Exception:
            return
        self.cdp, self.cdp_page = cdp, page

        def on_frame(params: dict) -> None:
            self.last_frame = params["data"]
            self._event({"type": "frame", "data": params["data"]})
            asyncio.ensure_future(_ack(cdp, params["sessionId"]))

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 60, "maxWidth": self.viewport["width"], "maxHeight": self.viewport["height"], "everyNthFrame": 1},
        )

    async def frame_jpeg(self) -> bytes:
        if self.page is None or self.state != "running":
            raise SessionError("not_running", "the session has no open page", 409)
        return await self.page.screenshot(type="jpeg", quality=60)

    # --------------------------------------------------------- commands --

    async def run(self, commands: list[dict], stop_on_error: bool = True) -> list[dict]:
        results = []
        async with self.lock:
            for cmd in commands:
                if self.state != "running":
                    raise SessionError("not_running", f"the session is {self.state}", 409)
                step = await self._step(cmd)
                results.append(step)
                if not step["ok"] and stop_on_error:
                    break
        return results

    async def _step(self, cmd: dict) -> dict:
        self.last_activity = time.monotonic()
        number = len(self.steps) + 1
        step = {
            "n": number,
            "at_ms": now_ms() - self.started_ms,
            "action": cmd["action"],
            "args": cmdlib.public_args(cmd),
            "ok": True,
        }
        started = time.monotonic()
        try:
            result = await self._execute(cmd, number)
            if result is not None:
                step["result"] = result
        except (cmdlib.CommandError, PolicyError) as err:
            step.update(ok=False, error_code=err.code, error=err.message)
        except Exception as err:  # Playwright errors: timeouts, detached elements...
            step.update(ok=False, error_code=_error_code(err), error=_first_line(err))
        step["duration_ms"] = int((time.monotonic() - started) * 1000)
        if self._pending_mask == number:
            step["args"] = cmdlib.public_args({**cmd, "mask": True})
        page = self.page
        if page is not None and not page.is_closed():
            step["url"] = page.url
            try:
                step["title"] = await page.title()
            except Exception:
                step["title"] = ""
            if self.step_screenshots and cmd["action"] not in ("note", "mark"):
                name = f"steps/{number:03d}.jpg"
                try:
                    await page.screenshot(path=str(self.dir / name), type="jpeg", quality=55)
                    step["screenshot"] = name
                except Exception:
                    pass
        if self._masked:
            step = self._scrub(step)
        self.steps.append(step)
        with open(self.dir / "steps.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(step, ensure_ascii=False) + "\n")
        self._event({"type": "step", "step": step})
        self.last_activity = time.monotonic()
        return step

    def _page(self):
        if self.page is None or self.page.is_closed():
            raise cmdlib.CommandError("no_page", "every tab of this session is closed")
        return self.page

    def _locator(self, target: dict):
        page = self._page()
        exact = target.get("exact")
        if "selector" in target:
            loc = page.locator(target["selector"])
        elif "text" in target:
            loc = page.get_by_text(target["text"], exact=exact)
        elif "role" in target:
            loc = page.get_by_role(target["role"], name=target.get("name"), exact=exact)
        elif "label" in target:
            loc = page.get_by_label(target["label"], exact=exact)
        elif "placeholder" in target:
            loc = page.get_by_placeholder(target["placeholder"], exact=exact)
        else:
            loc = page.get_by_test_id(target["test_id"])
        return loc.first

    async def _execute(self, cmd: dict, number: int):
        action = cmd["action"]
        timeout = cmd.get("timeout_ms", cmdlib.DEFAULT_TIMEOUT_MS)
        target = cmd.get("target")
        point = target if target and "x" in target else None

        if action == "goto":
            await check_url(cmd["url"], self.domains, self.manager.allow_private)
            response = await self._page().goto(cmd["url"], wait_until=cmd["wait_until"], timeout=timeout)
            return {"status": response.status if response else None}
        if action in ("click", "dblclick", "hover"):
            if point:
                mouse = self._page().mouse
                if action == "hover":
                    await mouse.move(point["x"], point["y"])
                else:
                    await mouse.click(point["x"], point["y"], button=cmd["button"], click_count=2 if action == "dblclick" else 1)
                return None
            loc = self._locator(target)
            if action == "hover":
                await loc.hover(timeout=timeout)
            elif action == "dblclick":
                await loc.dblclick(button=cmd["button"], timeout=timeout)
            else:
                await loc.click(button=cmd["button"], timeout=timeout)
            return None
        if action == "fill":
            value = cmdlib.resolve_secret(cmd["secret"]) if "secret" in cmd else cmd["value"]
            loc = self._locator(target)
            masked = cmd.get("mask")
            if not masked:
                kind = (await loc.get_attribute("type", timeout=timeout) or "").lower()
                if kind == "password":
                    self._mask_current(number)
                    masked = True
            if masked:
                self._remember_masked(value)
            await loc.fill(value, timeout=timeout)
            return None
        if action == "type":
            text = cmdlib.resolve_secret(cmd["secret"]) if "secret" in cmd else cmd["text"]
            if cmd.get("mask"):
                self._remember_masked(text)
            if target:
                await self._locator(target).press_sequentially(text, delay=cmd["delay_ms"], timeout=timeout)
            else:
                await self._page().keyboard.type(text, delay=cmd["delay_ms"])
            return None
        if action == "press":
            if target:
                await self._locator(target).press(cmd["key"], timeout=timeout)
            else:
                await self._page().keyboard.press(cmd["key"])
            return None
        if action == "select":
            chosen = await self._locator(target).select_option(cmd["values"], timeout=timeout)
            return {"selected": chosen}
        if action in ("check", "uncheck"):
            loc = self._locator(target)
            await (loc.check(timeout=timeout) if action == "check" else loc.uncheck(timeout=timeout))
            return None
        if action == "scroll":
            await self._page().mouse.wheel(cmd["dx"], cmd["dy"])
            await asyncio.sleep(0.2)
            return None
        if action == "wait":
            if target:
                await self._locator(target).wait_for(state=cmd["state"], timeout=timeout)
            elif "load_state" in cmd:
                await self._page().wait_for_load_state(cmd["load_state"], timeout=timeout)
            else:
                await asyncio.sleep(cmd["ms"] / 1000)
            return None
        if action == "screenshot":
            (self.dir / "shots").mkdir(exist_ok=True)
            name = f"shots/{cmd.get('name') or f'step-{number:03d}'}.png"
            if (self.dir / name).exists():
                name = f"shots/{cmd.get('name', 'shot')}-{number:03d}.png"
            if target:
                await self._locator(target).screenshot(path=str(self.dir / name), timeout=timeout)
            else:
                await self._page().screenshot(path=str(self.dir / name), full_page=cmd["full_page"], timeout=timeout)
            return {"file": name}
        if action == "extract":
            return await self._extract(cmd, target, timeout)
        if action in ("back", "forward", "reload"):
            page = self._page()
            response = await getattr(page, "go_back" if action == "back" else "go_forward" if action == "forward" else "reload")()
            return {"status": response.status if response else None}
        if action in ("note", "mark"):
            return None
        raise cmdlib.CommandError("unknown_action", action)

    def _remember_masked(self, value: str) -> None:
        """Masked values are also scrubbed from URLs, titles and results."""
        if len(value) >= 3:
            self._masked.add(value)

    def _scrub(self, value):
        if isinstance(value, str):
            for secret in self._masked:
                for form in (secret, quote(secret, safe=""), quote_plus(secret, safe="")):
                    value = value.replace(form, "••••")
            return value
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        return value

    def _mask_current(self, number: int) -> None:
        """A value typed into a password field is masked in the step log."""
        self._pending_mask = number

    async def _extract(self, cmd: dict, target: dict | None, timeout: int):
        what = cmd["what"]
        page = self._page()
        if what == "title":
            return {"value": await page.title()}
        if what == "url":
            return {"value": page.url}
        if what == "links":
            scope = self._locator(target) if target else page.locator("body")
            links = await scope.locator("a[href]").evaluate_all(
                "els => els.slice(0, 200).map(a => ({text: (a.innerText || '').trim().slice(0, 200), href: a.href}))"
            )
            return {"value": links}
        loc = self._locator(target) if target else page.locator("body")
        if what == "text":
            value = await loc.inner_text(timeout=timeout)
        elif what == "html":
            value = await loc.inner_html(timeout=timeout)
        elif what == "value":
            value = await loc.input_value(timeout=timeout)
        else:
            value = await loc.get_attribute(cmd["attribute"], timeout=timeout)
        if isinstance(value, str) and len(value) > 50_000:
            return {"value": value[:50_000], "truncated": True}
        return {"value": value}

    # ------------------------------------------------------------ close --

    async def close(self, reason: str = "requested") -> dict:
        if self.manifest is not None:
            return self.manifest
        async with self.lock:
            if self.manifest is not None:
                return self.manifest
            self.state = "closing"
            self._event({"type": "state", "session": self.public()})
            await self._stop_screencast()
            videos = []
            for page in self.pages:
                if page.video:
                    videos.append(page.video)
            try:
                await self.context.tracing.stop(path=str(self.dir / "trace.zip"))
            except Exception:
                pass
            try:
                await self.context.close()
            except Exception:
                pass
            names = []
            for index, video in enumerate(videos):
                try:
                    source = Path(await video.path())
                except Exception:
                    continue
                if source.exists() and source.stat().st_size > 0:
                    name = "video.webm" if index == 0 else f"video-{index + 1}.webm"
                    shutil.move(str(source), str(self.dir / name))
                    names.append(name)
            shutil.rmtree(self.dir / "video", ignore_errors=True)
            self.state = "closed"
            self.manifest = report.finalize(self.dir, self._meta(), self.steps, self.blocked, names, reason, now_ms())
            self._event({"type": "closed", "recording": self.manifest})
            return self.manifest

    # ------------------------------------------------------------- info --

    def _meta(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "start_url": self.start_url,
            "started_at": iso(self.started_ms),
            "started_ms": self.started_ms,
            "viewport": self.viewport,
            "allow_domains": self.domains,
            "typed_secrets": bool(self._masked),
        }

    def _write_meta(self) -> None:
        (self.dir / "session.json").write_text(json.dumps(self._meta(), indent=2), encoding="utf-8")

    def public(self) -> dict:
        page = self.page
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "started_at": iso(self.started_ms),
            "url": page.url if page is not None and not page.is_closed() else None,
            "tabs": len([p for p in self.pages if not p.is_closed()]),
            "steps": len(self.steps),
            "steps_failed": sum(1 for s in self.steps if not s["ok"]),
            "viewers": len(self.subscribers),
            "viewport": self.viewport,
            "allow_domains": self.domains,
            "idle_seconds": int(time.monotonic() - self.last_activity),
        }


async def _ack(cdp, session_id) -> None:
    try:
        await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
    except Exception:
        pass


def _first_line(err: Exception) -> str:
    text = str(err).strip().splitlines()
    return (text[0] if text else type(err).__name__)[:500]


def _error_code(err: Exception) -> str:
    name = type(err).__name__
    text = str(err)
    if name == "TimeoutError" or "Timeout" in text[:80]:
        return "timeout"
    if "net::ERR_BLOCKED_BY_CLIENT" in text:
        return "navigation_blocked"
    if "net::" in text:
        return "network_error"
    if "strict mode violation" in text:
        return "ambiguous_target"
    return "browser_error"


class Manager:
    def __init__(self, root: Path, *, max_sessions: int, idle_timeout: int, max_duration: int,
                 allow_private: bool, allow_domains: list[str], headless: bool = True):
        self.root = root
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.max_duration = max_duration
        self.allow_private = allow_private
        self.allow_domains = allow_domains
        self.headless = headless
        self.sessions: dict[str, Session] = {}
        self._pw = None
        self._browser = None
        self._sweeper = None
        self._start_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "Manager":
        root = Path(os.environ.get("BROWSER_DATA_DIR", "data")) / "recordings"
        return cls(
            root,
            max_sessions=int(os.environ.get("BROWSER_MAX_SESSIONS", "3")),
            idle_timeout=int(os.environ.get("BROWSER_IDLE_TIMEOUT", "600")),
            max_duration=int(os.environ.get("BROWSER_MAX_DURATION", "3600")),
            allow_private=os.environ.get("BROWSER_ALLOW_PRIVATE", "") == "1",
            allow_domains=parse_domains(os.environ.get("BROWSER_ALLOW_DOMAINS", "*")),
            headless=os.environ.get("BROWSER_HEADLESS", "1") != "0",
        )

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        report.recover_interrupted(self.root, iso(now_ms()))
        self._sweeper = asyncio.ensure_future(self._sweep())

    async def shutdown(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for session in list(self.sessions.values()):
            await self.close(session.id, "shutdown")
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _browser_ready(self):
        async with self._start_lock:
            if self._browser is None or not self._browser.is_connected():
                from playwright.async_api import async_playwright

                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=self.headless)
            return self._browser

    def options(self, body: dict) -> dict:
        label = body.get("label") or "Browser session"
        if not isinstance(label, str) or len(label) > 120:
            raise SessionError("invalid_field", "'label' is text up to 120 characters")
        start_url = body.get("start_url") or ""
        if not isinstance(start_url, str) or len(start_url) > cmdlib.MAX_URL:
            raise SessionError("invalid_field", "'start_url' is a URL")
        viewport = body.get("viewport") or {}
        try:
            width = int(viewport.get("width", 1280))
            height = int(viewport.get("height", 800))
        except (TypeError, ValueError, AttributeError):
            raise SessionError("invalid_field", "'viewport' is {width, height}") from None
        if not (320 <= width <= 1920 and 240 <= height <= 1200):
            raise SessionError("invalid_field", "viewport must be 320-1920 x 240-1200")
        try:
            domains = effective_domains(parse_domains(body.get("allow_domains")), self.allow_domains)
        except PolicyError as err:
            raise SessionError(err.code, err.message) from None
        return {
            "label": label.strip() or "Browser session",
            "start_url": start_url.strip(),
            "viewport": {"width": width, "height": height},
            "allow_domains": domains,
            "step_screenshots": bool(body.get("step_screenshots", True)),
        }

    async def create(self, body: dict) -> Session:
        options = self.options(body)
        if options["start_url"]:
            try:
                await check_url(options["start_url"], options["allow_domains"], self.allow_private)
            except PolicyError as err:
                raise SessionError(err.code, err.message) from None
        live = [s for s in self.sessions.values() if s.state in ("starting", "running")]
        if len(live) >= self.max_sessions:
            raise SessionError("too_many_sessions", f"at most {self.max_sessions} sessions at once; close one first", 429)
        session = Session(self, options)
        self.sessions[session.id] = session
        try:
            await session.start(await self._browser_ready())
        except SessionError:
            raise
        except Exception as err:
            await self.close(session.id, "start_failed")
            raise SessionError("start_failed", _first_line(err), 502) from None
        return session

    def get(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionError("unknown_session", "no live session with this id (closed sessions are recordings)", 404)
        return session

    async def close(self, session_id: str, reason: str = "requested") -> dict:
        session = self.get(session_id)
        try:
            manifest = await session.close(reason)
        finally:
            self.sessions.pop(session_id, None)
        return manifest

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(15)
            for session in list(self.sessions.values()):
                if session.state != "running":
                    continue
                idle = time.monotonic() - session.last_activity
                age = (now_ms() - session.started_ms) / 1000
                if session.subscribers:
                    idle = 0  # someone is watching
                if idle > self.idle_timeout:
                    await self.close(session.id, "idle_timeout")
                elif age > self.max_duration:
                    await self.close(session.id, "max_duration")
