"""HTTP + WebSocket API of browser-session.

    GET  /                                   health (no auth)
    POST /                                   Orqen text contract {"input": "<json action>"}
    GET  /sessions                           live sessions
    POST /sessions                           start one (optionally run commands and close: a one-shot recording)
    GET  /sessions/{id}                      one live session
    POST /sessions/{id}/commands             run commands, in order
    POST /sessions/{id}/close                stop and finalize the recording
    GET  /sessions/{id}/frame                the current page as a JPEG
    GET  /sessions/{id}/live                 WebSocket: frames and steps out, commands in
    GET  /recordings                         finished recordings, newest first
    GET  /recordings/{id}                    one recording's manifest (files listed)
    GET  /recordings/{id}/files/{name}       a file of it (Range requests for video seeking)

Everything except `GET /` needs `Authorization: Bearer <BROWSER_API_KEY>`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os

from aiohttp import WSMsgType, web

from . import __version__, report
from .commands import CommandError, normalize_all
from .session import Manager, SessionError, valid_id

MAX_BODY = 256 * 1024
MANAGER = web.AppKey("manager", Manager)


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"ok": False, "error_code": code, "error": {"code": code, "message": message}}, status=status)


@web.middleware
async def guard(request: web.Request, handler):
    if request.path == "/" and request.method == "GET":
        return await handler(request)
    key = os.environ.get("BROWSER_API_KEY", "")
    if not key:
        if os.environ.get("BROWSER_ALLOW_NO_AUTH") != "1":
            return _error(503, "not_configured", "set BROWSER_API_KEY to use this service")
    else:
        given = request.headers.get("Authorization", "")
        token = given[7:] if given.startswith("Bearer ") else ""
        if not hmac.compare_digest(token.encode(), key.encode()):
            return _error(401, "unauthorized", "missing or wrong bearer token")
    try:
        return await handler(request)
    except SessionError as err:
        return _error(err.status, err.code, err.message)
    except CommandError as err:
        return _error(400, err.code, err.message)


async def _json(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SessionError("invalid_json", "the body is not JSON") from None
    if not isinstance(body, dict):
        raise SessionError("invalid_json", "the body is a JSON object")
    return body


def _manager(request: web.Request) -> Manager:
    return request.app[MANAGER]


def _session_id(request: web.Request) -> str:
    value = request.match_info["id"]
    if not valid_id(value):
        raise SessionError("unknown_session", "not a session id", 404)
    return value


# -------------------------------------------------------------- sessions --


async def health(request: web.Request) -> web.Response:
    manager = _manager(request)
    return web.json_response({
        "ok": True,
        "service": "browser-session",
        "version": __version__,
        "sessions": len(manager.sessions),
        "max_sessions": manager.max_sessions,
        "auth": bool(os.environ.get("BROWSER_API_KEY")),
    })


def extracted(steps: list[dict]) -> dict:
    """What `extract` steps read, by their `name` (or `step_<n>`)."""
    out = {}
    for step in steps:
        if step["action"] == "extract" and step["ok"]:
            out[step["args"].get("name") or f"step_{step['n']}"] = (step.get("result") or {}).get("value")
    return out


def _has_commands(raw) -> bool:
    return bool(raw) and raw not in ("[]", "null")


async def start_session(manager: Manager, body: dict) -> dict:
    commands = normalize_all(body["commands"]) if _has_commands(body.get("commands")) else []
    session = await manager.create(body)
    steps = list(session.steps)
    if commands:
        steps += await session.run(commands, stop_on_error=bool(body.get("stop_on_error", True)))
    failed = next((s for s in steps if not s["ok"]), None)
    out = {"ok": failed is None, "error_code": failed["error_code"] if failed else "none", "session": session.public(),
           "steps": steps, "extracted": extracted(steps), "failed_step": failed}
    if body.get("close"):
        out["recording"] = await manager.close(session.id)
        out["session"]["state"] = "closed"
    return out


async def run_commands(manager: Manager, session_id: str, body: dict) -> dict:
    commands = normalize_all(body.get("commands", body.get("command")))
    session = manager.get(session_id)
    steps = await session.run(commands, stop_on_error=bool(body.get("stop_on_error", True)))
    failed = next((s for s in steps if not s["ok"]), None)
    return {"ok": failed is None, "error_code": failed["error_code"] if failed else "none", "steps": steps,
            "session": session.public(), "extracted": extracted(steps), "failed_step": failed}


async def list_sessions(request: web.Request) -> web.Response:
    return web.json_response({"sessions": [s.public() for s in _manager(request).sessions.values()]})


async def create_session(request: web.Request) -> web.Response:
    return web.json_response(await start_session(_manager(request), await _json(request)), status=201)


async def get_session(request: web.Request) -> web.Response:
    return web.json_response({"session": _manager(request).get(_session_id(request)).public()})


async def post_commands(request: web.Request) -> web.Response:
    return web.json_response(await run_commands(_manager(request), _session_id(request), await _json(request)))


async def close_session(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "recording": await _manager(request).close(_session_id(request))})


async def frame(request: web.Request) -> web.Response:
    image = await _manager(request).get(_session_id(request)).frame_jpeg()
    return web.Response(body=image, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def live(request: web.Request) -> web.WebSocketResponse:
    manager = _manager(request)
    session = manager.get(_session_id(request))
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_BODY)
    await ws.prepare(request)
    queue = await session.subscribe()

    async def pump() -> None:
        while not ws.closed:
            event = await queue.get()
            await ws.send_json(event)
            if event["type"] == "closed":
                await ws.close()

    async def handle(message: dict) -> None:
        ref = message.get("id")
        kind = message.get("type")
        try:
            if kind in ("command", "commands"):
                payload = message.get("command") if kind == "command" else message.get("commands")
                result = await run_commands(manager, session.id, {"commands": payload, "stop_on_error": message.get("stop_on_error", True)})
                await ws.send_json({"type": "result", "id": ref, **result})
            elif kind == "close":
                recording = await manager.close(session.id)
                await ws.send_json({"type": "result", "id": ref, "ok": True, "recording": recording})
            elif kind == "ping":
                await ws.send_json({"type": "pong", "id": ref, "session": session.public()})
            else:
                await ws.send_json({"type": "error", "id": ref, "code": "unknown_message", "message": "type is command, commands, close or ping"})
        except (SessionError, CommandError) as err:
            if not ws.closed:
                await ws.send_json({"type": "error", "id": ref, "code": err.code, "message": err.message})

    sender = asyncio.ensure_future(pump())
    tasks: set[asyncio.Task] = set()
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                message = json.loads(msg.data)
                if not isinstance(message, dict):
                    raise ValueError
            except ValueError:
                await ws.send_json({"type": "error", "code": "invalid_json", "message": "send JSON objects"})
                continue
            task = asyncio.ensure_future(handle(message))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        sender.cancel()
        await session.unsubscribe(queue)
    return ws


# ------------------------------------------------------------ recordings --


def _recording_id(request: web.Request) -> str:
    value = request.match_info["id"]
    if not valid_id(value):
        raise SessionError("unknown_recording", "not a recording id", 404)
    return value


async def list_recordings(request: web.Request) -> web.Response:
    return web.json_response({"recordings": report.list_recordings(_manager(request).root)})


async def get_recording(request: web.Request) -> web.Response:
    manifest = report.load_manifest(_manager(request).root, _recording_id(request))
    if manifest is None:
        raise SessionError("unknown_recording", "no finished recording with this id (a live session has none yet)", 404)
    return web.json_response({"recording": manifest})


async def recording_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    path = report.file_path(_manager(request).root, _recording_id(request), name)
    if path is None:
        raise SessionError("unknown_file", "no such file in this recording", 404)
    headers = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
    if path.suffix == ".html":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"
    if path.suffix in (".zip", ".har", ".jsonl"):
        headers["Content-Disposition"] = f'attachment; filename="{request.match_info["id"]}-{path.name}"'
    response = web.FileResponse(path, headers=headers)
    response.content_type = report.CONTENT_TYPES.get(path.suffix, "application/octet-stream").split(";")[0]
    return response


# ------------------------------------------------------ Orqen text contract --


async def contract(request: web.Request) -> web.Response:
    """`{"input": "<json>"}` -> `{"result": "<json>"}`; see README for actions."""
    body = await _json(request)
    raw = body.get("input")
    if not isinstance(raw, str):
        return _error(400, "invalid_input", "the body is {\"input\": \"<JSON text>\"}")
    try:
        action = json.loads(raw)
        if not isinstance(action, dict):
            raise ValueError
    except ValueError:
        return web.json_response({"result": json.dumps({"ok": False, "error_code": "invalid_input", "error": "input is a JSON object"})})
    manager = _manager(request)
    name = action.get("action")
    try:
        if name == "session.start":
            result = await start_session(manager, action)
        elif name == "session.run":
            result = await run_commands(manager, str(action.get("session_id", "")), action)
        elif name == "session.close":
            result = {"ok": True, "recording": await manager.close(str(action.get("session_id", "")))}
        elif name == "session.get":
            result = {"ok": True, "session": manager.get(str(action.get("session_id", ""))).public()}
        elif name == "sessions.list":
            result = {"ok": True, "sessions": [s.public() for s in manager.sessions.values()]}
        elif name == "recording.get":
            rid = str(action.get("recording_id", ""))
            manifest = report.load_manifest(manager.root, rid) if valid_id(rid) else None
            result = {"ok": manifest is not None, "recording": manifest} if manifest else {"ok": False, "error_code": "unknown_recording", "error": "no such recording"}
        elif name == "recordings.list":
            result = {"ok": True, "recordings": report.list_recordings(manager.root, 50)}
        else:
            result = {"ok": False, "error_code": "unknown_action", "error": "actions: session.start, session.run, session.close, session.get, sessions.list, recording.get, recordings.list"}
    except (SessionError, CommandError) as err:
        result = {"ok": False, "error_code": err.code, "error": err.message}
    return web.json_response({"result": json.dumps(result, ensure_ascii=False)})


# ------------------------------------------------------------------ app --


def create_app(manager: Manager | None = None) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[MANAGER] = manager or Manager.from_env()

    async def on_startup(app: web.Application) -> None:
        await app[MANAGER].startup()

    async def on_cleanup(app: web.Application) -> None:
        await app[MANAGER].shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", health)
    app.router.add_post("/", contract)
    app.router.add_get("/sessions", list_sessions)
    app.router.add_post("/sessions", create_session)
    app.router.add_get("/sessions/{id}", get_session)
    app.router.add_post("/sessions/{id}/commands", post_commands)
    app.router.add_post("/sessions/{id}/close", close_session)
    app.router.add_get("/sessions/{id}/frame", frame)
    app.router.add_get("/sessions/{id}/live", live)
    app.router.add_get("/recordings", list_recordings)
    app.router.add_get("/recordings/{id}", get_recording)
    app.router.add_get("/recordings/{id}/files/{name:.+}", recording_file)
    return app


def main() -> None:
    port = int(os.environ.get("PORT", "9321"))
    host = os.environ.get("HOST", "0.0.0.0")
    if not os.environ.get("BROWSER_API_KEY"):
        print("browser-session: BROWSER_API_KEY is not set; every call except GET / is refused", flush=True)
    web.run_app(create_app(), host=host, port=port, print=lambda *_: print(f"browser-session on {host}:{port}", flush=True))


if __name__ == "__main__":
    main()
