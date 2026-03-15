"""Command-line interface for Antigravity trajectory extraction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extractor import (
    export_sessions,
    extract_session,
    find_antigravity_paths,
    list_sessions,
    list_workspaces,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Antigravity workspace/session history via the local language server",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        help="Override the Antigravity state.vscdb path",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="Optional manually exported Antigravity diagnostics file to enrich titles/step counts",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("workspaces", help="List workspaces with tracked sessions")

    sessions = subparsers.add_parser("sessions", help="List known sessions")
    sessions.add_argument("--workspace", help="Filter sessions by exact workspace path")

    extract = subparsers.add_parser("extract", help="Extract one session")
    extract.add_argument("cascade_id", help="Session cascade/googleAgent ID")
    extract.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format",
    )
    extract.add_argument("--output", "-o", type=Path, help="Write output to file")

    extract_all = subparsers.add_parser(
        "extract-all",
        help="Extract all discovered sessions into a directory",
    )
    extract_all.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for exported session files",
    )
    extract_all.add_argument(
        "--workspace",
        help="Filter sessions by exact workspace path before export",
    )
    extract_all.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format for exported session files",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    default_state_db, default_diagnostics = find_antigravity_paths()
    state_db = args.state_db or default_state_db
    diagnostics = args.diagnostics or default_diagnostics

    if args.command == "workspaces":
        workspaces = list_workspaces(state_db=state_db)
        if not workspaces:
            print("No Antigravity workspaces found.", file=sys.stderr)
            raise SystemExit(1)
        print("\n".join(workspaces))
        return

    if args.command == "sessions":
        sessions = list_sessions(
            state_db=state_db,
            diagnostics_path=diagnostics,
            workspace=args.workspace,
        )
        if not sessions:
            print("No Antigravity sessions found.", file=sys.stderr)
            raise SystemExit(1)
        print(f"{'Cascade ID':36}  {'Steps':>5}  {'Title / Workspace'}")
        print("-" * 110)
        for session in sessions:
            title = session.get("title") or ""
            workspace = ", ".join(session.get("workspace_paths", []))
            label = title or workspace or "?"
            steps = session.get("last_step_index")
            step_text = str(steps) if steps is not None else "-"
            print(f"{session['cascade_id']:36}  {step_text:>5}  {label}")
        return

    if args.command == "extract-all":
        sessions = list_sessions(
            state_db=state_db,
            diagnostics_path=diagnostics,
            workspace=args.workspace,
        )
        if not sessions:
            print("No Antigravity sessions found.", file=sys.stderr)
            raise SystemExit(1)

        manifest = export_sessions(
            sessions,
            output_dir=args.output_dir,
            format=args.format,
            state_db=state_db,
            diagnostics_path=diagnostics,
        )
        print(
            f"Exported {manifest['exported_count']} sessions to {args.output_dir}"
            f" ({manifest['failed_count']} failed)",
            file=sys.stderr,
        )
        return

    try:
        result = extract_session(
            args.cascade_id,
            state_db=state_db,
            diagnostics_path=diagnostics,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.format == "json":
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    else:
        output = result["transcript"] + "\n"

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Wrote {args.format} output to {args.output}", file=sys.stderr)
        return
    print(output, end="")


if __name__ == "__main__":
    main()
