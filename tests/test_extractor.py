from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib import error

import antigravity_trajectory.extractor as extractor_module
from antigravity_trajectory.extractor import (
    WorkspaceProcess,
    _rpc_call,
    collect_sessions,
    export_sessions,
    extract_session,
    list_sessions,
    parse_diagnostics_recent_trajectories,
    parse_workspace_process,
    render_transcript,
)


class ParseWorkspaceProcessTests(unittest.TestCase):
    def test_extracts_workspace_port_and_tokens(self) -> None:
        line = (
            "/Applications/Antigravity.app/Contents/Resources/app/extensions/antigravity/"
            "bin/language_server_macos_arm --enable_lsp --csrf_token token-main "
            "--extension_server_port 63645 --extension_server_csrf_token token-ext "
            "--workspace_id file_tmp_example_project"
        )

        process = parse_workspace_process(line)

        self.assertEqual(
            process.workspace_id,
            "file_tmp_example_project",
        )
        self.assertEqual(process.extension_port, 63645)
        self.assertEqual(process.csrf_token, "token-main")
        self.assertEqual(process.extension_server_csrf_token, "token-ext")


class ParseDiagnosticsTests(unittest.TestCase):
    def test_extracts_recent_trajectory_mapping(self) -> None:
        diagnostics = json.dumps(
            {
                "recentTrajectories": [
                    {
                        "googleAgentId": "cascade-1",
                        "trajectoryId": "trajectory-1",
                        "summary": "Analyzing Book's Evolutionary Psychology",
                        "lastStepIndex": 239,
                        "lastModifiedTime": "2026-03-14T16:21:08.811Z",
                    }
                ]
            }
        )

        trajectories = parse_diagnostics_recent_trajectories(diagnostics)

        self.assertEqual(len(trajectories), 1)
        self.assertEqual(trajectories[0].cascade_id, "cascade-1")
        self.assertEqual(trajectories[0].trajectory_id, "trajectory-1")
        self.assertEqual(
            trajectories[0].summary, "Analyzing Book's Evolutionary Psychology"
        )


class CollectSessionsTests(unittest.TestCase):
    def test_merges_state_and_diagnostics_entries(self) -> None:
        state_sessions = [
            {
                "cascade_id": "cascade-1",
                "title": "State title",
                "workspace_paths": ["/tmp/project"],
                "last_modified": "2026-03-10T10:00:00+08:00",
            }
        ]
        diagnostics = [
            {
                "cascade_id": "cascade-1",
                "trajectory_id": "trajectory-1",
                "summary": "Diagnostics title",
                "last_step_index": 239,
                "last_modified_time": "2026-03-14T16:21:08.811Z",
            }
        ]

        sessions = collect_sessions(state_sessions, diagnostics)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["cascade_id"], "cascade-1")
        self.assertEqual(sessions[0]["trajectory_id"], "trajectory-1")
        self.assertEqual(sessions[0]["title"], "Diagnostics title")
        self.assertEqual(sessions[0]["last_step_index"], 239)


class LiveSessionDiscoveryTests(unittest.TestCase):
    def test_includes_live_rpc_sessions_when_state_and_diagnostics_miss_them(
        self,
    ) -> None:
        live_sessions = [
            {
                "cascade_id": "cascade-live",
                "trajectory_id": "trajectory-live",
                "title": "Live RPC title",
                "last_step_index": 42,
                "last_modified": "2026-03-15T10:00:00Z",
                "workspace_paths": ["/tmp/live-workspace"],
            }
        ]

        with (
            patch(
                "antigravity_trajectory.extractor._load_antigravity_summaries",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_diagnostics_sessions",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_live_trajectory_summaries",
                return_value=live_sessions,
                create=True,
            ),
            patch(
                "antigravity_trajectory.extractor._load_conversation_cache_sessions",
                return_value=[],
                create=True,
            ),
            patch(
                "antigravity_trajectory.extractor._load_brain_sessions",
                return_value=[],
                create=True,
            ),
        ):
            sessions = list_sessions(
                state_db=Path("/tmp/fake-state.vscdb"),
                diagnostics_path=Path("/tmp/fake-diagnostics.txt"),
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["cascade_id"], "cascade-live")
        self.assertEqual(sessions[0]["title"], "Live RPC title")
        self.assertEqual(sessions[0]["workspace_paths"], ["/tmp/live-workspace"])

    def test_ignores_brain_only_sessions_when_not_using_brain_discovery(self) -> None:
        brain_sessions = [
            {
                "cascade_id": "cascade-brain",
                "trajectory_id": "trajectory-brain",
                "title": "Older Brain Session",
                "last_step_index": 16,
                "last_modified": "2026-01-20T06:09:02Z",
                "workspace_paths": ["/tmp/older-workspace"],
            }
        ]

        with (
            patch(
                "antigravity_trajectory.extractor._load_antigravity_summaries",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_diagnostics_sessions",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_live_trajectory_summaries",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_conversation_cache_sessions",
                return_value=[],
                create=True,
            ),
            patch(
                "antigravity_trajectory.extractor._load_brain_sessions",
                return_value=brain_sessions,
                create=True,
            ),
            patch(
                "antigravity_trajectory.extractor._load_offline_sessions",
                return_value=[],
                create=True,
            ),
        ):
            sessions = list_sessions(
                state_db=Path("/tmp/fake-state.vscdb"),
                diagnostics_path=Path("/tmp/fake-diagnostics.txt"),
            )

        self.assertEqual(sessions, [])

    def test_prefers_conversation_cache_sessions_for_older_history(self) -> None:
        conversation_sessions = [
            {
                "cascade_id": "cascade-conversation",
                "trajectory_id": "trajectory-conversation",
                "title": "Older Conversation Cache Session",
                "last_step_index": 88,
                "last_modified": "2026-01-01T00:00:00Z",
                "workspace_paths": ["/tmp/conversation-workspace"],
            }
        ]

        with (
            patch(
                "antigravity_trajectory.extractor._load_antigravity_summaries",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_diagnostics_sessions",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_live_trajectory_summaries",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_conversation_cache_sessions",
                return_value=conversation_sessions,
                create=True,
            ),
            patch(
                "antigravity_trajectory.extractor._load_brain_sessions",
                return_value=[],
                create=True,
            ),
        ):
            sessions = list_sessions(
                state_db=Path("/tmp/fake-state.vscdb"),
                diagnostics_path=Path("/tmp/fake-diagnostics.txt"),
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["cascade_id"], "cascade-conversation")
        self.assertEqual(sessions[0]["title"], "Older Conversation Cache Session")

    def test_drops_stale_state_only_sessions_when_validated_sources_exist(self) -> None:
        state_sessions = [
            {
                "cascade_id": "cascade-stale",
                "title": "Stale state entry",
                "workspace_paths": ["/tmp/stale-workspace"],
                "last_modified": "2026-03-01T00:00:00Z",
            }
        ]
        live_sessions = [
            {
                "cascade_id": "cascade-live",
                "trajectory_id": "trajectory-live",
                "title": "Live session",
                "last_step_index": 5,
                "last_modified": "2026-03-02T00:00:00Z",
                "workspace_paths": ["/tmp/live-workspace"],
            }
        ]

        with (
            patch(
                "antigravity_trajectory.extractor._load_antigravity_summaries",
                return_value=state_sessions,
            ),
            patch(
                "antigravity_trajectory.extractor._load_diagnostics_sessions",
                return_value=[],
            ),
            patch(
                "antigravity_trajectory.extractor._load_live_trajectory_summaries",
                return_value=live_sessions,
                create=True,
            ),
            patch(
                "antigravity_trajectory.extractor._load_conversation_cache_sessions",
                return_value=[],
                create=True,
            ),
            patch(
                "antigravity_trajectory.extractor._load_brain_sessions",
                return_value=[],
                create=True,
            ),
        ):
            sessions = list_sessions(
                state_db=Path("/tmp/fake-state.vscdb"),
                diagnostics_path=Path("/tmp/fake-diagnostics.txt"),
            )

        self.assertEqual([item["cascade_id"] for item in sessions], ["cascade-live"])


class RenderTranscriptTests(unittest.TestCase):
    def test_renders_user_tool_and_assistant_messages(self) -> None:
        steps = [
            {
                "type": "CORTEX_STEP_TYPE_USER_INPUT",
                "userInput": {"userResponse": "hello"},
            },
            {
                "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
                "runCommand": {"command": "pwd", "renderedOutput": {"full": "/tmp"}},
            },
            {
                "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                "plannerResponse": {"response": "world"},
            },
        ]

        transcript = render_transcript(steps)

        self.assertIn("## User", transcript)
        self.assertIn("hello", transcript)
        self.assertIn("## Tool: run_command", transcript)
        self.assertIn("pwd", transcript)
        self.assertIn("## Assistant", transcript)
        self.assertIn("world", transcript)


class ExportSessionsTests(unittest.TestCase):
    def test_writes_all_sessions_and_manifest(self) -> None:
        sessions = [
            {"cascade_id": "cascade-1", "title": "First session"},
            {"cascade_id": "cascade-2", "title": "Second session"},
        ]
        fake_results = {
            "cascade-1": {
                "session": sessions[0],
                "steps": [{"type": "CORTEX_STEP_TYPE_USER_INPUT"}],
                "transcript": "first transcript",
            },
            "cascade-2": {
                "session": sessions[1],
                "steps": [{"type": "CORTEX_STEP_TYPE_USER_INPUT"}],
                "transcript": "second transcript",
            },
        }

        def fake_extract(cascade_id: str, **_: object) -> dict[str, object]:
            return fake_results[cascade_id]

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = export_sessions(
                sessions,
                output_dir=Path(tmpdir),
                format="markdown",
                extract_fn=fake_extract,
            )

            self.assertEqual(manifest["exported_count"], 2)
            self.assertTrue((Path(tmpdir) / "cascade-1.md").exists())
            self.assertTrue((Path(tmpdir) / "cascade-2.md").exists())
            self.assertTrue((Path(tmpdir) / "manifest.json").exists())
            self.assertIn(
                "first transcript",
                (Path(tmpdir) / "cascade-1.md").read_text(encoding="utf-8"),
            )


class RpcCallTests(unittest.TestCase):
    def test_retries_with_https_when_http_endpoint_requires_tls(self) -> None:
        urls: list[str] = []

        class DummyResponse:
            def __enter__(self) -> DummyResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"trajectory":{"trajectoryId":"trajectory-1"}}'

        def fake_urlopen(req, timeout=10, context=None):  # type: ignore[no-untyped-def]
            urls.append(req.full_url)
            if req.full_url.startswith("http://"):
                raise error.HTTPError(
                    req.full_url,
                    400,
                    "bad request",
                    hdrs=None,
                    fp=BytesIO(b"Client sent an HTTP request to an HTTPS server.\n"),
                )
            return DummyResponse()

        with patch(
            "antigravity_trajectory.extractor.request.urlopen",
            side_effect=fake_urlopen,
        ):
            payload = _rpc_call(
                63649,
                "csrf-token",
                "GetCascadeTrajectory",
                {"cascadeId": "cascade-1"},
            )

        self.assertEqual(payload["trajectory"]["trajectoryId"], "trajectory-1")
        self.assertEqual(
            urls,
            [
                "http://127.0.0.1:63649/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory",
                "https://127.0.0.1:63649/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory",
            ],
        )

    def test_caches_successful_scheme_per_port(self) -> None:
        urls: list[str] = []

        class DummyResponse:
            def __enter__(self) -> DummyResponse:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"ok":true}'

        def fake_urlopen(req, timeout=10, context=None):  # type: ignore[no-untyped-def]
            urls.append(req.full_url)
            if req.full_url.startswith("http://"):
                raise error.HTTPError(
                    req.full_url,
                    400,
                    "bad request",
                    hdrs=None,
                    fp=BytesIO(b"Client sent an HTTP request to an HTTPS server.\n"),
                )
            return DummyResponse()

        with (
            patch.dict(
                extractor_module._RPC_SCHEME_CACHE,
                {},
                clear=True,
            ),
            patch(
                "antigravity_trajectory.extractor.request.urlopen",
                side_effect=fake_urlopen,
            ),
        ):
            _rpc_call(63649, "csrf-token", "GetWorkspaceInfos", {})
            _rpc_call(63649, "csrf-token", "GetWorkspaceInfos", {})

        self.assertEqual(
            urls,
            [
                "http://127.0.0.1:63649/exa.language_server_pb.LanguageServerService/GetWorkspaceInfos",
                "https://127.0.0.1:63649/exa.language_server_pb.LanguageServerService/GetWorkspaceInfos",
                "https://127.0.0.1:63649/exa.language_server_pb.LanguageServerService/GetWorkspaceInfos",
            ],
        )


class ExtractSessionLiveRpcTests(unittest.TestCase):
    def test_extract_session_includes_generator_metadata(self) -> None:
        session = {
            "cascade_id": "cascade-1",
            "title": "Live session",
            "workspace_paths": ["/tmp/live-workspace"],
        }
        process = WorkspaceProcess(
            pid=123,
            workspace_id="file_tmp_live_workspace",
            csrf_token="csrf-token",
            extension_server_csrf_token=None,
            extension_port=None,
            command="language_server",
        )
        rpc_calls: list[tuple[str, dict[str, object]]] = []

        def fake_rpc_call(
            port: int,
            csrf_token: str | None,
            method: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            self.assertEqual(port, 63649)
            self.assertEqual(csrf_token, "csrf-token")
            rpc_calls.append((method, payload))
            if method == "GetCascadeTrajectorySteps":
                return {
                    "steps": [
                        {
                            "type": "CORTEX_STEP_TYPE_USER_INPUT",
                            "userInput": {"userResponse": "hello"},
                        }
                    ]
                }
            if method == "GetCascadeTrajectoryGeneratorMetadata":
                offset = payload.get("generatorMetadataOffset", 0)
                if offset == 0:
                    return {"generatorMetadata": [{"stepIndices": [0]}]}
                return {"generatorMetadata": []}
            raise AssertionError(f"Unexpected method: {method}")

        with (
            patch(
                "antigravity_trajectory.extractor.list_sessions",
                return_value=[session],
            ),
            patch(
                "antigravity_trajectory.extractor._select_workspace_process",
                return_value=process,
            ),
            patch(
                "antigravity_trajectory.extractor._find_working_rpc_port",
                return_value=(63649, {"trajectory": {"trajectoryId": "trajectory-1"}}),
            ),
            patch(
                "antigravity_trajectory.extractor._rpc_call",
                side_effect=fake_rpc_call,
            ),
        ):
            result = extract_session("cascade-1")

        self.assertEqual(result["extraction_mode"], "live_rpc")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["generator_metadata"], [{"stepIndices": [0]}])
        self.assertEqual(
            rpc_calls,
            [
                (
                    "GetCascadeTrajectorySteps",
                    {"cascadeId": "cascade-1", "verbosity": 1},
                ),
                (
                    "GetCascadeTrajectoryGeneratorMetadata",
                    {
                        "cascadeId": "cascade-1",
                        "generatorMetadataOffset": 0,
                        "includeMessages": True,
                    },
                ),
                (
                    "GetCascadeTrajectoryGeneratorMetadata",
                    {
                        "cascadeId": "cascade-1",
                        "generatorMetadataOffset": 1,
                        "includeMessages": True,
                    },
                ),
            ],
        )

    def test_extract_session_falls_back_when_generator_metadata_is_too_large(
        self,
    ) -> None:
        session = {
            "cascade_id": "cascade-1",
            "title": "Live session",
            "workspace_paths": ["/tmp/live-workspace"],
        }
        process = WorkspaceProcess(
            pid=123,
            workspace_id="file_tmp_live_workspace",
            csrf_token="csrf-token",
            extension_server_csrf_token=None,
            extension_port=None,
            command="language_server",
        )

        def fake_rpc_call(
            port: int,
            csrf_token: str | None,
            method: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            if method == "GetCascadeTrajectorySteps":
                return {"steps": []}
            if method != "GetCascadeTrajectoryGeneratorMetadata":
                raise AssertionError(f"Unexpected method: {method}")
            offset = payload.get("generatorMetadataOffset", 0)
            include_messages = payload.get("includeMessages")
            if offset == 0 and include_messages is True:
                return {"generatorMetadata": [{"stepIndices": [0]}]}
            if offset == 1 and include_messages is True:
                raise RuntimeError(
                    "generator metadata at offset 1 is 22316826 bytes, larger than 4194304 byte limit"
                )
            if offset == 1 and include_messages is False:
                return {"generatorMetadata": [{"stepIndices": [1]}]}
            if offset == 2 and include_messages is True:
                return {"generatorMetadata": []}
            raise AssertionError(f"Unexpected payload: {payload}")

        with (
            patch(
                "antigravity_trajectory.extractor.list_sessions",
                return_value=[session],
            ),
            patch(
                "antigravity_trajectory.extractor._select_workspace_process",
                return_value=process,
            ),
            patch(
                "antigravity_trajectory.extractor._find_working_rpc_port",
                return_value=(63649, {"trajectory": {"trajectoryId": "trajectory-1"}}),
            ),
            patch(
                "antigravity_trajectory.extractor._rpc_call",
                side_effect=fake_rpc_call,
            ),
        ):
            result = extract_session("cascade-1")

        self.assertEqual(
            result["generator_metadata"],
            [
                {"stepIndices": [0]},
                {"stepIndices": [1], "messagesTruncated": True},
            ],
        )

    def test_extract_session_requires_live_rpc_even_if_offline_bundle_exists(
        self,
    ) -> None:
        session = {
            "cascade_id": "cascade-1",
            "title": "Live session",
            "workspace_paths": ["/tmp/live-workspace"],
        }

        with (
            patch(
                "antigravity_trajectory.extractor.list_sessions",
                return_value=[session],
            ),
            patch(
                "antigravity_trajectory.extractor._select_workspace_process",
                side_effect=RuntimeError(
                    "No running Antigravity workspace language_server process found"
                ),
            ),
            patch(
                "antigravity_trajectory.extractor._load_offline_session_bundle",
                return_value={
                    "session": session,
                    "artifacts": [{"name": "task.md", "content": "offline content"}],
                },
                create=True,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "No running Antigravity"):
                extract_session("cascade-1")


if __name__ == "__main__":
    unittest.main()
