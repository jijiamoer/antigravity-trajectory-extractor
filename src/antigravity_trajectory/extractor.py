"""Core extraction logic for Antigravity trajectory history."""

from __future__ import annotations

import base64
import json
import platform
import re
import shlex
import sqlite3
import ssl
import struct
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import unquote

DEFAULT_TZ = timezone(timedelta(hours=8))
ANTIGRAVITY_SUMMARIES_KEY = "antigravityUnifiedStateSync.trajectorySummaries"
CONVERSATIONS_ROOT = Path.home() / ".gemini/antigravity/conversations"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
BASE64_TEXT_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")
RECENT_TRAJECTORY_RE = re.compile(
    r'"googleAgentId"\s*:\s*"(?P<cascade>[^"]+)".*?'
    r'"trajectoryId"\s*:\s*"(?P<trajectory>[^"]+)".*?'
    r'"summary"\s*:\s*"(?P<summary>(?:\\.|[^"])*)".*?'
    r'"lastStepIndex"\s*:\s*(?P<last_step>\d+).*?'
    r'(?:"lastModifiedTime"\s*:\s*"(?P<modified>[^"]+)")?',
    re.S,
)
CLIENT_TRAJECTORY_VERBOSITY_DEBUG = 1
GENERATOR_METADATA_SIZE_LIMIT_MARKER = "larger than 4194304 byte limit"
_RPC_SCHEME_CACHE: dict[int, str] = {}


@dataclass
class WorkspaceProcess:
    pid: int | None
    workspace_id: str
    csrf_token: str | None
    extension_server_csrf_token: str | None
    extension_port: int | None
    command: str


@dataclass
class DiagnosticsTrajectory:
    cascade_id: str
    trajectory_id: str | None
    summary: str
    last_step_index: int | None
    last_modified_time: str | None


def find_antigravity_paths() -> tuple[Path | None, Path | None]:
    system = platform.system()
    home = Path.home()

    if system == "Darwin":
        app_support = home / "Library/Application Support"
        downloads = [home / "Downloads"]
    elif system == "Linux":
        app_support = home / ".config"
        downloads = [home / "Downloads"]
    else:
        app_support = home / "AppData/Roaming"
        downloads = [home / "Downloads"]

    state_db = app_support / "Antigravity" / "User/globalStorage/state.vscdb"
    diagnostics_file = None
    for folder in downloads:
        if not folder.exists():
            continue
        matches = sorted(folder.glob("Antigravity-diagnostics*.txt"))
        if matches:
            diagnostics_file = max(matches, key=lambda path: path.stat().st_mtime)
            break
    return (
        state_db if state_db.exists() else None,
        diagnostics_file,
    )


def parse_workspace_process(line: str) -> WorkspaceProcess:
    pid_match = re.match(r"^\s*(\d+)\s+(.*)$", line)
    pid = int(pid_match.group(1)) if pid_match else None
    command = pid_match.group(2) if pid_match else line.strip()
    tokens = shlex.split(command)

    values: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token.startswith("--")
            and index + 1 < len(tokens)
            and not tokens[index + 1].startswith("--")
        ):
            values[token[2:]] = tokens[index + 1]
            index += 2
            continue
        index += 1

    workspace_id = values.get("workspace_id") or ""
    if not workspace_id:
        raise ValueError("workspace_id not found in process args")

    extension_port = None
    if values.get("extension_server_port"):
        extension_port = int(values["extension_server_port"])

    return WorkspaceProcess(
        pid=pid,
        workspace_id=workspace_id,
        csrf_token=values.get("csrf_token"),
        extension_server_csrf_token=values.get("extension_server_csrf_token"),
        extension_port=extension_port,
        command=command,
    )


def parse_diagnostics_recent_trajectories(text: str) -> list[DiagnosticsTrajectory]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict) and isinstance(data.get("recentTrajectories"), list):
        return [
            DiagnosticsTrajectory(
                cascade_id=item.get("googleAgentId", ""),
                trajectory_id=item.get("trajectoryId"),
                summary=item.get("summary", ""),
                last_step_index=item.get("lastStepIndex"),
                last_modified_time=item.get("lastModifiedTime"),
            )
            for item in data["recentTrajectories"]
            if item.get("googleAgentId")
        ]

    trajectories: list[DiagnosticsTrajectory] = []
    for match in RECENT_TRAJECTORY_RE.finditer(text):
        summary = bytes(match.group("summary"), "utf-8").decode("unicode_escape")
        trajectories.append(
            DiagnosticsTrajectory(
                cascade_id=match.group("cascade"),
                trajectory_id=match.group("trajectory"),
                summary=summary,
                last_step_index=int(match.group("last_step")),
                last_modified_time=match.group("modified"),
            )
        )
    return trajectories


def collect_sessions(
    state_sessions: list[dict[str, Any]],
    diagnostics_sessions: list[dict[str, Any]] | list[DiagnosticsTrajectory],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for session in state_sessions:
        cascade_id = session.get("cascade_id")
        if cascade_id:
            merged[cascade_id] = dict(session)

    for item in diagnostics_sessions:
        if isinstance(item, DiagnosticsTrajectory):
            diag = {
                "cascade_id": item.cascade_id,
                "trajectory_id": item.trajectory_id,
                "summary": item.summary,
                "last_step_index": item.last_step_index,
                "last_modified_time": item.last_modified_time,
            }
        else:
            diag = item

        cascade_id = diag.get("cascade_id")
        if not cascade_id:
            continue
        current = merged.setdefault(cascade_id, {"cascade_id": cascade_id})
        if diag.get("trajectory_id"):
            current["trajectory_id"] = diag["trajectory_id"]
        if diag.get("summary"):
            current["title"] = diag["summary"]
        elif diag.get("title"):
            current["title"] = diag["title"]
        if diag.get("last_step_index") is not None:
            current["last_step_index"] = diag["last_step_index"]
        if diag.get("last_modified_time"):
            current["last_modified"] = diag["last_modified_time"]
        elif diag.get("last_modified"):
            current["last_modified"] = diag["last_modified"]
        if diag.get("workspace_paths"):
            current["workspace_paths"] = list(diag["workspace_paths"])

    return sorted(
        merged.values(),
        key=lambda item: item.get("last_modified") or "",
        reverse=True,
    )


def render_transcript(steps: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for step in steps:
        step_type = step.get("type")
        if step_type == "CORTEX_STEP_TYPE_USER_INPUT":
            text = step.get("userInput", {}).get("userResponse") or ""
            if text:
                sections.append(f"## User\n\n{text}")
        elif step_type == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            response_text = step.get("plannerResponse", {}).get("response") or ""
            if response_text:
                sections.append(f"## Assistant\n\n{response_text}")
        elif step_type == "CORTEX_STEP_TYPE_RUN_COMMAND":
            payload = step.get("runCommand", {})
            command = payload.get("command") or payload.get("commandLine") or ""
            output = payload.get("renderedOutput", {})
            if isinstance(output, dict):
                output_text = output.get("full") or ""
            else:
                output_text = str(output or "")
            body = ["## Tool: run_command", "", "```bash", command, "```"]
            if output_text:
                body.extend(["", "```text", output_text, "```"])
            sections.append("\n".join(body))
        elif step_type == "CORTEX_STEP_TYPE_VIEW_FILE":
            payload = step.get("viewFile", {})
            path = payload.get("absolutePath") or payload.get("absolutePathUri") or ""
            if path:
                sections.append(f"## Tool: view_file\n\n`{path}`")
        elif step_type == "CORTEX_STEP_TYPE_ERROR_MESSAGE":
            payload = step.get("errorMessage", {})
            text = payload.get("shortError") or payload.get("text") or ""
            if text:
                sections.append(f"## Error\n\n{text}")
    return "\n\n".join(sections)


def list_workspaces(state_db: Path | None = None) -> list[str]:
    sessions = list_sessions(state_db=state_db)
    workspaces: set[str] = set()
    for session in sessions:
        for path in session.get("workspace_paths", []):
            workspaces.add(path)
    return sorted(workspaces)


def list_sessions(
    *,
    state_db: Path | None = None,
    diagnostics_path: Path | None = None,
    workspace: str | None = None,
) -> list[dict[str, Any]]:
    if state_db is None and diagnostics_path is None:
        state_db, diagnostics_path = find_antigravity_paths()

    state_sessions = _load_antigravity_summaries(state_db)
    diagnostics_sessions = _load_diagnostics_sessions(diagnostics_path)
    live_sessions = _load_live_trajectory_summaries()
    conversation_cache_sessions = _load_conversation_cache_sessions()

    sessions = collect_sessions(state_sessions, diagnostics_sessions)
    sessions = collect_sessions(sessions, live_sessions)
    sessions = collect_sessions(sessions, conversation_cache_sessions)

    validated_ids = {
        item["cascade_id"]
        for item in [*live_sessions, *conversation_cache_sessions]
        if item.get("cascade_id")
    }
    if validated_ids:
        sessions = [
            session
            for session in sessions
            if session.get("cascade_id") in validated_ids
        ]

    if workspace is None:
        return sessions
    return [
        session
        for session in sessions
        if workspace in session.get("workspace_paths", [])
    ]


def extract_session(
    cascade_id: str,
    *,
    state_db: Path | None = None,
    diagnostics_path: Path | None = None,
) -> dict[str, Any]:
    sessions = list_sessions(state_db=state_db, diagnostics_path=diagnostics_path)
    session = next(
        (item for item in sessions if item["cascade_id"] == cascade_id), None
    )
    if session is None:
        raise KeyError(f"Session not found: {cascade_id}")

    process = _select_workspace_process(session)
    rpc_port, trajectory_meta = _find_working_rpc_port(process, cascade_id)
    steps = _fetch_live_steps(rpc_port, process.csrf_token, cascade_id)
    generator_metadata = _fetch_live_generator_metadata(
        rpc_port,
        process.csrf_token,
        cascade_id,
    )

    return {
        "session": session,
        "workspace_process": {
            "pid": process.pid,
            "workspace_id": process.workspace_id,
            "rpc_port": rpc_port,
        },
        "trajectory": trajectory_meta,
        "steps": steps,
        "generator_metadata": generator_metadata,
        "transcript": render_transcript(steps),
        "extraction_mode": "live_rpc",
    }


def export_sessions(
    sessions: list[dict[str, Any]],
    *,
    output_dir: Path,
    format: str = "markdown",
    extract_fn: Any = None,
    state_db: Path | None = None,
    diagnostics_path: Path | None = None,
) -> dict[str, Any]:
    if format not in {"markdown", "json"}:
        raise ValueError(f"Unsupported export format: {format}")

    output_dir.mkdir(parents=True, exist_ok=True)
    extension = "json" if format == "json" else "md"
    extractor = extract_fn or extract_session

    manifest_items: list[dict[str, Any]] = []
    exported_count = 0
    failed_count = 0

    for session in sessions:
        cascade_id = session.get("cascade_id")
        if not cascade_id:
            continue

        target_path = output_dir / f"{cascade_id}.{extension}"
        manifest_entry = {
            "cascade_id": cascade_id,
            "title": session.get("title") or "",
            "workspace_paths": session.get("workspace_paths") or [],
            "output_path": str(target_path),
        }

        try:
            result = extractor(
                cascade_id,
                state_db=state_db,
                diagnostics_path=diagnostics_path,
            )
            if format == "json":
                output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            else:
                output = result.get("transcript", "") + "\n"
            target_path.write_text(output, encoding="utf-8")
            manifest_entry["status"] = "exported"
            manifest_entry["step_count"] = len(result.get("steps", []))
            manifest_entry["generator_metadata_count"] = len(
                result.get("generator_metadata", [])
            )
            exported_count += 1
        except Exception as exc:
            manifest_entry["status"] = "failed"
            manifest_entry["error"] = str(exc)
            failed_count += 1

        manifest_items.append(manifest_entry)

    manifest = {
        "format": format,
        "exported_count": exported_count,
        "failed_count": failed_count,
        "sessions": manifest_items,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _read_sqlite_value(state_db: Path, key: str) -> str:
    conn = sqlite3.connect(str(state_db))
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            raise KeyError(f"{key!r} key not found in state.vscdb")
        return row[0]
    finally:
        conn.close()


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data):
        value = data[pos]
        result |= (value & 0x7F) << shift
        pos += 1
        if not (value & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _parse_fields(data: bytes, start: int, end: int) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        try:
            tag, next_pos = _decode_varint(data, cursor)
            if tag == 0:
                cursor = next_pos
                continue
            field_number, wire_type = tag >> 3, tag & 7
            if wire_type == 0:
                value, cursor = _decode_varint(data, next_pos)
                fields.append({"fn": field_number, "type": "varint", "value": value})
            elif wire_type == 2:
                size, start_pos = _decode_varint(data, next_pos)
                if size < 0 or size > end - start_pos:
                    break
                cursor = start_pos + size
                fields.append(
                    {
                        "fn": field_number,
                        "type": "bytes",
                        "start": start_pos,
                        "end": cursor,
                    }
                )
            elif wire_type == 1:
                fields.append(
                    {
                        "fn": field_number,
                        "type": "fixed64",
                        "value": struct.unpack_from("<Q", data, next_pos)[0],
                    }
                )
                cursor = next_pos + 8
            elif wire_type == 5:
                fields.append(
                    {
                        "fn": field_number,
                        "type": "fixed32",
                        "value": struct.unpack_from("<I", data, next_pos)[0],
                    }
                )
                cursor = next_pos + 4
            else:
                break
        except Exception:
            break
    return fields


def _try_decode_str(data: bytes, start: int, end: int) -> str | None:
    try:
        return data[start:end].decode("utf-8")
    except Exception:
        return None


def _looks_like_base64_text(text: str) -> bool:
    cleaned = text.strip()
    return (
        len(cleaned) >= 16
        and len(cleaned) % 4 == 0
        and bool(BASE64_TEXT_RE.fullmatch(cleaned))
    )


def _maybe_decode_base64_blob(data: bytes) -> bytes | None:
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not _looks_like_base64_text(text):
        return None
    try:
        return base64.b64decode(text, validate=True)
    except Exception:
        return None


def _parse_timestamp(data: bytes, start: int, end: int) -> datetime | None:
    fields = _parse_fields(data, start, end)
    seconds = 0
    nanos = 0
    for field in fields:
        if field["fn"] == 1 and field["type"] == "varint":
            seconds = field["value"]
        elif field["fn"] == 2 and field["type"] == "varint":
            nanos = field["value"]
    if 1577836800 < seconds < 2208988800:
        return datetime.fromtimestamp(seconds + nanos / 1e9, tz=DEFAULT_TZ)
    return None


def _is_probable_title(text: str) -> bool:
    cleaned = text.strip()
    if len(cleaned) < 4 or len(cleaned) > 200:
        return False
    if cleaned.startswith("file://") or UUID_RE.fullmatch(cleaned):
        return False
    if _looks_like_base64_text(cleaned) and not any(ch.isspace() for ch in cleaned):
        return False
    if (
        "/" in cleaned
        and " " not in cleaned
        and not re.search(r"[\u4e00-\u9fff]", cleaned)
    ):
        return False
    return any(ch.isspace() for ch in cleaned) or bool(
        re.search(r"[\u4e00-\u9fff]", cleaned)
    )


def _walk_message_strings(
    data: bytes,
    *,
    max_depth: int = 5,
    _depth: int = 0,
) -> tuple[list[str], list[datetime]]:
    if _depth > max_depth:
        return [], []

    fields = _parse_fields(data, 0, len(data))
    if not fields:
        return [], []

    strings: list[str] = []
    timestamps: list[datetime] = []
    seen_strings: set[str] = set()

    for field in fields:
        if field["type"] != "bytes":
            continue
        start = field["start"]
        end = field["end"]
        raw = data[start:end]

        text = _try_decode_str(data, start, end)
        if text is not None:
            cleaned = text.strip()
            if cleaned and cleaned not in seen_strings:
                strings.append(cleaned)
                seen_strings.add(cleaned)

        timestamp = _parse_timestamp(data, start, end)
        if timestamp is not None:
            timestamps.append(timestamp)
            continue

        nested = _maybe_decode_base64_blob(raw)
        if nested is not None:
            sub_strings, sub_timestamps = _walk_message_strings(
                nested,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        else:
            sub_strings, sub_timestamps = _walk_message_strings(
                raw,
                max_depth=max_depth,
                _depth=_depth + 1,
            )

        for item in sub_strings:
            if item not in seen_strings:
                strings.append(item)
                seen_strings.add(item)
        timestamps.extend(sub_timestamps)

    return strings, timestamps


def _file_uri_to_path(uri: str) -> str:
    decoded = unquote(uri)
    if decoded.startswith("file:///"):
        path_part = decoded[8:]
        if len(path_part) > 1 and path_part[1] == ":":
            return path_part
        return "/" + path_part
    return decoded


def _parse_antigravity_summary_entry(entry_blob: bytes) -> dict[str, Any]:
    entry_fields = _parse_fields(entry_blob, 0, len(entry_blob))
    cascade_id = ""
    summary_blob = b""

    for field in entry_fields:
        if field["fn"] == 1 and field["type"] == "bytes" and not cascade_id:
            cascade_id = (
                _try_decode_str(entry_blob, field["start"], field["end"]) or ""
            ).strip()
        elif field["fn"] == 2 and field["type"] == "bytes" and not summary_blob:
            summary_blob = entry_blob[field["start"] : field["end"]]

    decoded = _maybe_decode_base64_blob(summary_blob) or summary_blob
    strings, timestamps = _walk_message_strings(decoded)
    workspace_paths: list[str] = []
    for item in strings:
        if item.startswith("file://"):
            path = _file_uri_to_path(item)
            if path not in workspace_paths:
                workspace_paths.append(path)

    title = next((item for item in strings if _is_probable_title(item)), "")
    return {
        "cascade_id": cascade_id,
        "title": title,
        "workspace_paths": workspace_paths,
        "last_modified": max(timestamps).isoformat() if timestamps else None,
    }


def _load_antigravity_summaries(state_db: Path | None) -> list[dict[str, Any]]:
    if state_db is None or not state_db.exists():
        return []
    try:
        raw = _read_sqlite_value(state_db, ANTIGRAVITY_SUMMARIES_KEY)
    except (KeyError, sqlite3.Error):
        return []
    blob = base64.b64decode(raw)
    sessions: list[dict[str, Any]] = []
    for field in _parse_fields(blob, 0, len(blob)):
        if field["fn"] != 1 or field["type"] != "bytes":
            continue
        entry = _parse_antigravity_summary_entry(blob[field["start"] : field["end"]])
        if entry.get("cascade_id"):
            sessions.append(entry)
    return sessions


def _load_diagnostics_sessions(
    diagnostics_path: Path | None,
) -> list[DiagnosticsTrajectory]:
    if diagnostics_path is None or not diagnostics_path.exists():
        return []
    try:
        text = diagnostics_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return parse_diagnostics_recent_trajectories(text)


def _workspace_uris_to_paths(items: list[str]) -> list[str]:
    paths: list[str] = []
    for item in items:
        if not item or not item.startswith("file://"):
            continue
        path = _file_uri_to_path(item)
        if path not in paths:
            paths.append(path)
    return paths


def _workspace_paths_from_summary(summary: dict[str, Any]) -> list[str]:
    workspace_uris: list[str] = []

    for workspace in summary.get("workspaces", []):
        if not isinstance(workspace, dict):
            continue
        for key in ("workspaceFolderAbsoluteUri", "gitRootAbsoluteUri"):
            value = workspace.get(key)
            if value and value not in workspace_uris:
                workspace_uris.append(value)

    metadata = summary.get("trajectoryMetadata", {})
    if isinstance(metadata, dict):
        for value in metadata.get("workspaceUris", []):
            if value and value not in workspace_uris:
                workspace_uris.append(value)
        for workspace in metadata.get("workspaces", []):
            if not isinstance(workspace, dict):
                continue
            for key in ("workspaceFolderAbsoluteUri", "gitRootAbsoluteUri"):
                value = workspace.get(key)
                if value and value not in workspace_uris:
                    workspace_uris.append(value)

    return _workspace_uris_to_paths(workspace_uris)


def _truncate_title(text: str, limit: int = 80) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _title_from_steps(steps: list[dict[str, Any]]) -> str:
    for step in steps:
        if step.get("type") != "CORTEX_STEP_TYPE_USER_INPUT":
            continue
        text = step.get("userInput", {}).get("userResponse") or ""
        if text.strip():
            return _truncate_title(text)
    return ""


def _trajectory_payload_to_session(payload: dict[str, Any]) -> dict[str, Any]:
    trajectory = payload.get("trajectory", payload)
    steps = trajectory.get("steps", []) if isinstance(trajectory, dict) else []
    return {
        "cascade_id": trajectory.get("cascadeId") or "",
        "trajectory_id": trajectory.get("trajectoryId"),
        "title": trajectory.get("summary") or _title_from_steps(steps),
        "workspace_paths": _workspace_paths_from_summary(trajectory),
        "last_modified": trajectory.get("lastModifiedTime")
        or trajectory.get("createdAt")
        or trajectory.get("metadata", {}).get("createdAt"),
        "last_step_index": payload.get("numTotalSteps")
        or trajectory.get("stepCount")
        or len(steps),
    }


def _discover_rpc_contexts() -> list[tuple[int, str]]:
    contexts: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()

    for process in _discover_workspace_processes():
        if not process.csrf_token:
            continue

        ports = _listening_ports(process.pid)
        if process.extension_port is not None and process.extension_port not in ports:
            ports.append(process.extension_port)

        for port in ports:
            key = (port, process.csrf_token)
            if key in seen:
                continue
            seen.add(key)
            contexts.append(key)

    return contexts


def _load_conversation_candidate_ids() -> list[str]:
    if not CONVERSATIONS_ROOT.exists():
        return []
    return sorted(
        path.stem
        for path in CONVERSATIONS_ROOT.glob("*.pb")
        if UUID_RE.fullmatch(path.stem)
    )


def _summary_to_session(cascade_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    session = {
        "cascade_id": cascade_id,
        "trajectory_id": summary.get("trajectoryId"),
        "title": summary.get("summary") or "",
        "workspace_paths": _workspace_paths_from_summary(summary),
        "last_modified": summary.get("lastModifiedTime"),
    }

    if summary.get("stepCount") is not None:
        session["last_step_index"] = summary["stepCount"]

    return session


def _load_live_trajectory_summaries() -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}

    for port, csrf_token in _discover_rpc_contexts():
        try:
            payload = _rpc_call(
                port,
                csrf_token,
                "GetAllCascadeTrajectories",
                {},
            )
        except Exception:
            continue

        trajectory_summaries = payload.get("trajectorySummaries", {})
        if not isinstance(trajectory_summaries, dict):
            continue

        for cascade_id, summary in trajectory_summaries.items():
            if not isinstance(summary, dict):
                continue
            current = sessions.setdefault(cascade_id, {"cascade_id": cascade_id})
            current.update(_summary_to_session(cascade_id, summary))

    return sorted(
        sessions.values(),
        key=lambda item: item.get("last_modified") or "",
        reverse=True,
    )


def _load_conversation_cache_sessions() -> list[dict[str, Any]]:
    candidate_ids = _load_conversation_candidate_ids()
    if not candidate_ids:
        return []

    sessions: dict[str, dict[str, Any]] = {}
    for port, csrf_token in _discover_rpc_contexts():
        remaining = [cid for cid in candidate_ids if cid not in sessions]
        if not remaining:
            break

        for cascade_id in remaining:
            try:
                payload = _rpc_call(
                    port,
                    csrf_token,
                    "GetCascadeTrajectory",
                    {"cascadeId": cascade_id},
                )
            except Exception:
                continue

            session = _trajectory_payload_to_session(payload)
            if session.get("cascade_id"):
                sessions[cascade_id] = session

    return sorted(
        sessions.values(),
        key=lambda item: item.get("last_modified") or "",
        reverse=True,
    )


def _discover_workspace_processes() -> list[WorkspaceProcess]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    processes: list[WorkspaceProcess] = []
    for line in result.stdout.splitlines():
        if "workspace_id" not in line or "language_server" not in line:
            continue
        if "Antigravity.app" not in line and "app_data_dir antigravity" not in line:
            continue
        try:
            processes.append(parse_workspace_process(line))
        except ValueError:
            continue
    return processes


def _encode_workspace_path(path: str) -> str:
    return f"file_{path.lstrip('/').replace('/', '_')}"


def _select_workspace_process(session: dict[str, Any]) -> WorkspaceProcess:
    processes = _discover_workspace_processes()
    workspace_paths = session.get("workspace_paths", [])
    encoded = {_encode_workspace_path(path) for path in workspace_paths}

    for process in processes:
        if process.workspace_id in encoded:
            return process
    if processes:
        return processes[0]
    raise RuntimeError("No running Antigravity workspace language_server process found")


def _listening_ports(pid: int | None) -> list[int]:
    if pid is None:
        return []
    result = subprocess.run(
        ["lsof", "-Pan", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        match = re.search(r":(\d+)\s+\(LISTEN\)", line)
        if match:
            ports.add(int(match.group(1)))
    return sorted(ports)


def _find_working_rpc_port(
    process: WorkspaceProcess,
    cascade_id: str,
) -> tuple[int, dict[str, Any]]:
    if not process.csrf_token:
        raise RuntimeError("Workspace process does not expose csrf_token")

    errors_seen: list[str] = []
    for port in _listening_ports(process.pid):
        try:
            payload = _rpc_call(
                port,
                process.csrf_token,
                "GetCascadeTrajectory",
                {"cascadeId": cascade_id},
            )
            trajectory = payload.get("trajectory", payload)
            if trajectory.get("trajectoryId"):
                return port, payload
        except Exception as exc:
            errors_seen.append(f"{port}: {exc}")
            continue

    if process.extension_port is not None:
        try:
            payload = _rpc_call(
                process.extension_port,
                process.csrf_token,
                "GetCascadeTrajectory",
                {"cascadeId": cascade_id},
            )
            trajectory = payload.get("trajectory", payload)
            if trajectory.get("trajectoryId"):
                return process.extension_port, payload
        except Exception as exc:
            errors_seen.append(f"{process.extension_port}: {exc}")

    raise RuntimeError(
        "Could not discover a working trajectory RPC port. Tried: "
        + ", ".join(errors_seen)
    )


def _fetch_live_steps(
    port: int,
    csrf_token: str | None,
    cascade_id: str,
) -> list[dict[str, Any]]:
    response = _rpc_call(
        port,
        csrf_token,
        "GetCascadeTrajectorySteps",
        {
            "cascadeId": cascade_id,
            "verbosity": CLIENT_TRAJECTORY_VERBOSITY_DEBUG,
        },
    )
    steps = response.get("steps", [])
    return steps if isinstance(steps, list) else []


def _fetch_live_generator_metadata(
    port: int,
    csrf_token: str | None,
    cascade_id: str,
) -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    offset = 0

    while True:
        try:
            response = _rpc_call(
                port,
                csrf_token,
                "GetCascadeTrajectoryGeneratorMetadata",
                {
                    "cascadeId": cascade_id,
                    "generatorMetadataOffset": offset,
                    "includeMessages": True,
                },
            )
            chunk = response.get("generatorMetadata", [])
        except RuntimeError as exc:
            if GENERATOR_METADATA_SIZE_LIMIT_MARKER not in str(exc):
                raise
            response = _rpc_call(
                port,
                csrf_token,
                "GetCascadeTrajectoryGeneratorMetadata",
                {
                    "cascadeId": cascade_id,
                    "generatorMetadataOffset": offset,
                    "includeMessages": False,
                },
            )
            chunk = response.get("generatorMetadata", [])
            if not isinstance(chunk, list) or not chunk:
                raise
            metadata.extend(_mark_generator_metadata_truncated(chunk))
            offset += len(chunk)
            continue

        if not isinstance(chunk, list) or not chunk:
            return metadata

        metadata.extend(chunk)
        offset += len(chunk)


def _mark_generator_metadata_truncated(
    chunk: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    marked: list[dict[str, Any]] = []
    for item in chunk:
        if isinstance(item, dict):
            marked.append({**item, "messagesTruncated": True})
        else:
            marked.append(item)
    return marked


def _rpc_call(
    port: int,
    csrf_token: str | None,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not csrf_token:
        raise RuntimeError("Missing csrf token")

    body = json.dumps(payload).encode("utf-8")
    errors_seen: list[str] = []

    for scheme in _rpc_schemes_for_port(port):
        url = (
            f"{scheme}://127.0.0.1:{port}"
            f"/exa.language_server_pb.LanguageServerService/{method}"
        )
        req = request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf_token,
            },
            method="POST",
        )

        try:
            if scheme == "https":
                context = ssl._create_unverified_context()
                with request.urlopen(req, timeout=10, context=context) as response:
                    content = response.read().decode("utf-8")
            else:
                with request.urlopen(req, timeout=10) as response:
                    content = response.read().decode("utf-8")
        except error.HTTPError as exc:
            content = exc.read().decode("utf-8", errors="ignore")
            errors_seen.append(f"{scheme} HTTP {exc.code}: {content or exc.reason}")
            continue
        except error.URLError as exc:
            errors_seen.append(f"{scheme} {exc.reason}")
            continue

        try:
            payload_data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON response: {content[:200]}") from exc

        _RPC_SCHEME_CACHE[port] = scheme
        return payload_data

    raise RuntimeError("; ".join(errors_seen))


def _rpc_schemes_for_port(port: int) -> list[str]:
    preferred = _RPC_SCHEME_CACHE.get(port)
    schemes: list[str] = []
    if preferred:
        schemes.append(preferred)
    for scheme in ("http", "https"):
        if scheme not in schemes:
            schemes.append(scheme)
    return schemes
