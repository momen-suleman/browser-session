"""Recordings on disk: the manifest, the step log and the readable report.

A recording folder holds:

    manifest.json   what is here (the index the API serves)
    session.json    options the session started with
    steps.jsonl     one line per step, appended as it happens
    steps.json      the same steps as one document
    report.md       the session as documentation: timeline, results, shots
    report.html     the same, self-contained apart from the screenshots
    video.webm      the screen recording (video-2.webm... for extra tabs)
    trace.zip       Playwright trace: open at https://trace.playwright.dev
    network.har     every request and response (headers, timings; no bodies)
    steps/NNN.jpg   a screenshot after every step
    shots/*.png     screenshots a command asked for
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

CONTENT_TYPES = {
    ".webm": "video/webm",
    ".zip": "application/zip",
    ".har": "application/json",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}
KINDS = {
    "video.webm": "video",
    "trace.zip": "trace",
    "network.har": "network",
    "report.md": "report",
    "report.html": "report",
    "steps.json": "steps",
}


def _iso(ms: int) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def _clock(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _describe(step: dict) -> str:
    args = step.get("args", {})
    target = args.get("target") or {}
    where = ""
    if "x" in target:
        where = f" at ({target['x']}, {target['y']})"
    elif target:
        key, value = next(iter(target.items()))
        where = f" {key}={value!r}"
        if "name" in target and key == "role":
            where += f" name={target['name']!r}"
    action = step["action"]
    if action == "goto":
        return f"Open {args.get('url')}"
    if action in ("note", "mark"):
        return args.get("text", "")
    if action == "fill":
        source = f"secret {args['secret']}" if "secret" in args else repr(args.get("value"))
        return f"Fill{where} with {source}"
    if action == "type":
        source = f"secret {args['secret']}" if "secret" in args else repr(args.get("text"))
        return f"Type {source}{where}"
    if action == "press":
        return f"Press {args.get('key')}{where}"
    if action == "select":
        return f"Select {', '.join(args.get('values', []))}{where}"
    if action == "scroll":
        return f"Scroll by ({args.get('dx')}, {args.get('dy')})"
    if action == "wait":
        if "ms" in args:
            return f"Wait {args['ms']} ms"
        if "load_state" in args:
            return f"Wait for {args['load_state']}"
        return f"Wait until{where} is {args.get('state')}"
    if action == "extract":
        return f"Read {args.get('what')}{where}"
    if action == "screenshot":
        return f"Screenshot{where}"
    return f"{action.capitalize()}{where}"


def _result_text(step: dict) -> str:
    if not step["ok"]:
        return f"FAILED ({step.get('error_code')}): {step.get('error', '')}"
    result = step.get("result")
    if not result:
        return "ok"
    if "value" in result:
        value = result["value"]
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        text = " ".join(text.split())
        return text[:300] + ("…" if len(text) > 300 else "")
    return ", ".join(f"{k}: {v}" for k, v in result.items())


def finalize(folder: Path, meta: dict, steps: list[dict], blocked: list[dict], videos: list[str],
             reason: str, ended_ms: int) -> dict:
    (folder / "steps.json").write_text(json.dumps(steps, indent=2, ensure_ascii=False), encoding="utf-8")
    duration = ended_ms - meta["started_ms"]
    manifest = {
        "id": meta["id"],
        "label": meta["label"],
        "start_url": meta["start_url"],
        "started_at": meta["started_at"],
        "ended_at": _iso(ended_ms),
        "duration_ms": duration,
        "close_reason": reason,
        "viewport": meta["viewport"],
        "allow_domains": meta["allow_domains"],
        "steps_total": len(steps),
        "steps_failed": sum(1 for s in steps if not s["ok"]),
        "blocked_navigations": blocked,
        "video": videos[0] if videos else None,
        "videos": videos,
        "trace": "trace.zip" if (folder / "trace.zip").exists() else None,
        "har": "network.har" if (folder / "network.har").exists() else None,
        "report": "report.md",
        "report_html": "report.html",
        "steps": "steps.json",
        # Masked values never appear in steps or reports, but the trace and
        # the network log are raw captures of the browser and may hold them.
        "raw_files_may_contain_secrets": bool(meta.get("typed_secrets")),
    }
    (folder / "report.md").write_text(render_markdown(manifest, steps), encoding="utf-8")
    (folder / "report.html").write_text(render_html(manifest, steps), encoding="utf-8")
    manifest["files"] = list_files(folder)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def list_files(folder: Path) -> list[dict]:
    files = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        name = path.relative_to(folder).as_posix()
        files.append({
            "name": name,
            "kind": KINDS.get(name, "screenshot" if path.suffix in (".jpg", ".png") else "data"),
            "bytes": path.stat().st_size,
            "content_type": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
        })
    return files


def render_markdown(m: dict, steps: list[dict]) -> str:
    lines = [
        f"# {m['label']}",
        "",
        f"- **Recording:** `{m['id']}`",
        f"- **Started:** {m['started_at']} · **Ended:** {m['ended_at']} · **Duration:** {_clock(m['duration_ms'])}",
        f"- **Start page:** {m['start_url'] or '(none)'}",
        f"- **Steps:** {m['steps_total']} ({m['steps_failed']} failed) · **Closed:** {m['close_reason']}",
        f"- **Allowed domains:** {', '.join(m['allow_domains'])}",
        f"- **Files:** video `{m['video'] or 'none'}`, trace `{m['trace'] or 'none'}`"
        f" (open at https://trace.playwright.dev), network log `{m['har'] or 'none'}`",
        "",
        "## Timeline",
        "",
        "| # | Time | Step | Result | Page |",
        "|---|---|---|---|---|",
    ]
    for s in steps:
        cell = lambda t: str(t).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        lines.append(
            f"| {s['n']} | {_clock(s['at_ms'])} | {cell(_describe(s))} | {cell(_result_text(s))} | {cell(s.get('title') or s.get('url') or '')} |"
        )
    if m["blocked_navigations"]:
        lines += ["", "## Blocked navigations", ""]
        lines += [f"- {_clock(b['at_ms'])} `{b['url']}` ({b['code']})" for b in m["blocked_navigations"]]
    lines += ["", "## Steps", ""]
    for s in steps:
        if s["action"] == "mark":
            lines += [f"### {s['args'].get('text', '')}", ""]
            continue
        lines.append(f"**{s['n']}. {_describe(s)}** — {_clock(s['at_ms'])}, {s.get('duration_ms', 0)} ms")
        lines.append("")
        lines.append(f"Result: {_result_text(s)}")
        if s.get("url"):
            lines.append(f"Page: {s.get('title') or ''} <{s['url']}>")
        if s.get("screenshot"):
            lines += ["", f"![step {s['n']}]({s['screenshot']})"]
        lines.append("")
    return "\n".join(lines) + "\n"


def render_html(m: dict, steps: list[dict]) -> str:
    e = html.escape
    rows = []
    for s in steps:
        if s["action"] == "mark":
            rows.append(f"<h2>{e(s['args'].get('text', ''))}</h2>")
            continue
        status = "ok" if s["ok"] else "fail"
        shot = f'<img loading="lazy" src="{e(s["screenshot"])}" alt="step {s["n"]}">' if s.get("screenshot") else ""
        rows.append(
            f'<section class="{status}"><div class="meta"><b>{s["n"]}</b> <span>{_clock(s["at_ms"])}</span></div>'
            f"<div><p class=\"what\">{e(_describe(s))}</p><p class=\"res\">{e(_result_text(s))}</p>"
            f"<p class=\"page\">{e(s.get('title') or '')} {e(s.get('url') or '')}</p>{shot}</div></section>"
        )
    video = f'<video controls src="{e(m["video"])}"></video>' if m["video"] else "<p>No video.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(m['label'])}</title>
<style>
:root{{--bg:#fff;--fg:#1b1d21;--muted:#6b7078;--line:#e3e5e8;--ok:#1e7a46;--fail:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141619;--fg:#e8e9eb;--muted:#9aa0a8;--line:#2b2f35;--ok:#5cc88a;--fail:#f08a80}}}}
body{{margin:0 auto;max-width:960px;padding:24px 16px;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
h1{{margin:0 0 4px}} .sub{{color:var(--muted);margin:0 0 16px}} video{{width:100%;border:1px solid var(--line);border-radius:8px}}
section{{display:grid;grid-template-columns:64px 1fr;gap:12px;border-top:1px solid var(--line);padding:12px 0}}
section.fail .what{{color:var(--fail)}} .meta span,.page{{color:var(--muted);font-size:13px}} p{{margin:2px 0}}
.what{{font-weight:600}} img{{max-width:100%;border:1px solid var(--line);border-radius:6px;margin-top:6px}}
</style></head><body>
<h1>{e(m['label'])}</h1>
<p class="sub">{e(m['started_at'])} · {_clock(m['duration_ms'])} · {m['steps_total']} steps, {m['steps_failed']} failed · closed: {e(m['close_reason'])}</p>
{video}
{''.join(rows)}
</body></html>
"""


def load_manifest(root: Path, recording_id: str) -> dict | None:
    path = root / recording_id / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_recordings(root: Path, limit: int = 200) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir(), reverse=True):
        manifest = folder / "manifest.json"
        if manifest.is_file():
            m = json.loads(manifest.read_text(encoding="utf-8"))
            m.pop("files", None)
            out.append(m)
        if len(out) >= limit:
            break
    return out


def file_path(root: Path, recording_id: str, name: str) -> Path | None:
    """Only files the manifest lists are served: no paths from the caller."""
    manifest = load_manifest(root, recording_id)
    if manifest is None:
        return None
    if name not in {f["name"] for f in manifest.get("files", [])}:
        return None
    path = root / recording_id / name
    return path if path.is_file() else None


def recover_interrupted(root: Path, now_iso: str) -> list[str]:
    """Folders a crash or restart left without a manifest get one now."""
    import time

    recovered = []
    for folder in root.iterdir() if root.is_dir() else []:
        meta_path = folder / "session.json"
        if not meta_path.is_file() or (folder / "manifest.json").exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        steps = []
        log = folder / "steps.jsonl"
        if log.is_file():
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    steps.append(json.loads(line))
                except json.JSONDecodeError:
                    break
        videos = []
        partial = sorted((folder / "video").glob("*.webm")) if (folder / "video").is_dir() else []
        for index, source in enumerate(partial):
            if source.stat().st_size > 0:
                name = "video.webm" if index == 0 else f"video-{index + 1}.webm"
                shutil.move(str(source), str(folder / name))
                videos.append(name)
        shutil.rmtree(folder / "video", ignore_errors=True)
        ended = meta["started_ms"] + (steps[-1]["at_ms"] if steps else 0)
        finalize(folder, meta, steps, [], videos, "interrupted", max(ended, int(time.time() * 1000) if not steps else ended))
        recovered.append(folder.name)
    return recovered
