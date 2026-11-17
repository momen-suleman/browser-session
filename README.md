# browser-session

Recorded, remotely controlled browser sessions over HTTP and WebSocket, built
on [Playwright](https://playwright.dev) with Chromium.

A controller (a person in Orqen Studio, an Orqen workflow, or any program with
the API key) opens a session, drives it with commands such as
`{"action": "click", "text": "Sign in"}`, and watches it live. When the
session closes, everything is saved:

| File | |
|---|---|
| `video.webm` | screen recording of the whole session |
| `report.md`, `report.html` | the session as documentation: timeline, each step and its result, a screenshot after every step, chapters and notes |
| `steps.json` | the steps as data |
| `trace.zip` | Playwright trace (open at <https://trace.playwright.dev>) |
| `network.har` | requests and responses (headers, timings; no bodies) |
| `shots/*.png` | screenshots commands asked for |

## Run

```sh
docker build -t browser-session .
docker run --rm -p 127.0.0.1:9334:9321 --shm-size 1g \
  -e BROWSER_API_KEY=change-me -v browser-session-data:/app/data browser-session
```

or, with Orqen, `docker compose up -d --build browser-session` in Orqen's
`docker/` folder.

| Variable | Default | |
|---|---|---|
| `BROWSER_API_KEY` | (required) | bearer token for every call except `GET /` |
| `BROWSER_ALLOW_DOMAINS` | `*` | domains any session may open; a session can only narrow it |
| `BROWSER_ALLOW_PRIVATE` | off | `1` lets sessions open private, loopback and link-local addresses |
| `BROWSER_MAX_SESSIONS` | `3` | sessions at once |
| `BROWSER_IDLE_TIMEOUT` | `600` | seconds without commands or viewers before a session closes itself |
| `BROWSER_MAX_DURATION` | `3600` | seconds a session may last |
| `BROWSER_SECRET_<NAME>` | | values for `"secret": "<NAME>"` in `fill` / `type` |
| `BROWSER_DATA_DIR` | `/app/data` | recordings live in `<dir>/recordings/<id>/` |
| `PORT` | `9321` | |

## API

All bodies are JSON; all calls except `GET /` need
`Authorization: Bearer <BROWSER_API_KEY>`.

```
GET  /                                health
POST /sessions                        start: {label, start_url, allow_domains, viewport, step_screenshots,
                                             commands, stop_on_error, close}
GET  /sessions, /sessions/{id}        live sessions
POST /sessions/{id}/commands          {commands: [...], stop_on_error}
POST /sessions/{id}/close             finalize the recording
GET  /sessions/{id}/frame             current page, JPEG
GET  /sessions/{id}/live              WebSocket (below)
GET  /recordings, /recordings/{id}    finished recordings and their manifests
GET  /recordings/{id}/files/{name}    a file (Range requests supported)
POST /                                Orqen text contract {"input": "<JSON action>"} -> {"result": "<JSON>"}
```

Command results: `{ok, error_code, steps, extracted, failed_step, session}`;
`extracted` holds what `extract` commands read, by their `name`. Refusals are
`{ok: false, error_code, error: {code, message}}` with a 4xx status.

One-shot recording:

```sh
curl -s -X POST localhost:9334/sessions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{
    "label": "Example", "start_url": "https://example.com", "close": true,
    "commands": [
      {"action": "extract", "selector": "h1", "name": "heading"},
      {"action": "click", "role": "link", "name": "Learn more"},
      {"action": "screenshot", "name": "after", "full_page": true}
    ]}'
```

### Live channel

`GET /sessions/{id}/live` upgrades to a WebSocket. The server sends
`{"type": "state", "session", "steps"}` first, then `{"type": "frame", "data"}`
(base64 JPEG screencast, only while someone is connected),
`{"type": "step", "step"}` for every step from any controller,
`{"type": "blocked", "url", "code"}` and finally `{"type": "closed", "recording"}`.
The client sends `{"type": "commands", "id": "c1", "commands": [...]}` and gets
`{"type": "result", "id": "c1", ...}`; `{"type": "close"}` finalizes the session.

### Commands

`goto`, `click`, `dblclick`, `hover`, `fill`, `type`, `press`, `select`,
`check`, `uncheck`, `scroll`, `wait`, `screenshot`, `extract`, `back`,
`forward`, `reload`, `mark` (a chapter), `note`. Targets: `selector`, `text`,
`role` + `name`, `label`, `placeholder`, `test_id`, or `x`/`y` for mouse
actions. Every command takes `timeout_ms` (default 15000, at most 60000). See
`browser_session/commands.py` for every field.

## Safety

* **No scripts.** There is no evaluate action; a controller drives the page
  like a person.
* **Navigation allow-list** on every document load (redirects and frames too),
  and **no private addresses** by default (DNS is resolved and checked).
* **Secrets** are typed from `BROWSER_SECRET_<NAME>` and masked as `••••` in
  steps, reports, URLs and results, as is anything typed into a password
  field or with `mask: true`. The trace and the HAR are raw captures and may
  contain them; the manifest says so (`raw_files_may_contain_secrets`).
* No downloads, no service workers, bounded sessions, idle timeouts. Files are
  served only if the recording's manifest lists them.

## What is out of scope

* Browsers other than Chromium.
* File uploads and downloads.
* Getting past bot protection or CAPTCHAs.
* Running JavaScript in the page.

## Tests

```sh
pip install -r requirements.txt && python -m playwright install chromium
python -m unittest discover -s tests -v
```

The end-to-end tests drive a real headless Chromium against a local test site
(recording, masking, the navigation guard, the live WebSocket, limits).
