import json
from pathlib import Path

import pytest

from contxt_config import ContxtConfig
from contxt_review_server import (
    GitHubClient,
    ReviewLoopServer,
    build_overview,
    build_snapshot,
    is_github_rate_limit_error,
    session_output_indicates_completion,
)


def make_pr(number=12, decision="REVIEW_REQUIRED", merge_state="CLEAN", status_nodes=None):
    return {
        "number": number,
        "title": "Improve queueing",
        "url": f"https://github.com/acme/demo/pull/{number}",
        "headRefName": f"feature-{number}",
        "baseRefName": "main",
        "mergeStateStatus": merge_state,
        "mergeable": "MERGEABLE",
        "reviewDecision": decision,
        "updatedAt": "2026-03-17T00:00:00Z",
        "isDraft": False,
        "author": {"login": "gabeferns"},
        "repository": {
            "nameWithOwner": "acme/demo",
            "sshUrl": "git@github.com:acme/demo.git",
            "defaultBranchRef": {"name": "main"},
        },
        "latestReviews": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "statusCheckRollup": {"contexts": {"nodes": status_nodes or []}},
    }


def make_thread(thread_id, author, body):
    return {
        "id": thread_id,
        "isResolved": False,
        "updatedAt": "2026-03-17T00:00:00Z",
        "comments": {
            "nodes": [
                {
                    "body": body,
                    "url": f"https://github.com/acme/demo/pull/12#discussion_{thread_id}",
                    "publishedAt": "2026-03-17T00:00:00Z",
                    "author": {"login": author},
                }
            ]
        },
    }


def test_build_snapshot_prioritizes_missing_worktree_reviews_and_ci():
    pr = make_pr(
        status_nodes=[{"name": "lint", "conclusion": "FAILURE"}],
    )
    pr["reviewThreads"]["nodes"] = [
        make_thread("thread-agent", "reflection-agent", "Please tighten the retry loop."),
        make_thread("thread-human", "alice", "This branch needs a docs update."),
    ]
    store = {
        "workflow_states": {},
        "queue_overrides": {},
        "queue_runs": {
            "resolve_review_thread:pr:acme/demo#12:thread-agent": {
                "status": "running",
                "entity_id": "pr:acme/demo#12",
                "session_name": "contxt-review-pr-acme-demo-12",
                "started_at": "2026-03-17T01:00:00Z",
            }
        },
    }

    snapshot = build_snapshot([pr], [], store, ["reflection-agent", "devin-ai-integration"])

    entity = snapshot["entities"][0]
    queue_kinds = [item["kind"] for item in snapshot["queue"]]

    assert entity["lifecycle_state"] == "missing_worktree"
    assert entity["reviews_state"] == "red"
    assert entity["ci_state"] == "red"
    assert queue_kinds == [
        "create_worktree",
        "resolve_review_thread",
        "resolve_review_thread",
        "fix_ci",
    ]
    assert snapshot["queue"][1]["owner"] == "human"
    assert snapshot["queue"][2]["owner"] == "agent"
    assert entity["current_remediation"]["session_name"] == "contxt-review-pr-acme-demo-12"


def test_build_snapshot_marks_ready_to_merge():
    pr = make_pr(
        number=44,
        decision="APPROVED",
        merge_state="CLEAN",
        status_nodes=[{"name": "ci", "conclusion": "SUCCESS"}],
    )
    worktree = {
        "project": "demo",
        "name": "feature-44",
        "worktree_key": "demo/feature-44",
        "worktree_path": "/tmp/demo-feature-44",
        "branch": "feature-44",
        "repo_full_name": "acme/demo",
        "pr_number": 44,
        "dirty": False,
        "ahead": 0,
        "behind": 0,
    }

    snapshot = build_snapshot([pr], [worktree], {"workflow_states": {}, "queue_overrides": {}, "queue_runs": {}}, ["reflection-agent", "devin-ai-integration"])

    entity = snapshot["entities"][0]
    assert entity["ready_to_merge"] is True
    assert snapshot["queue"][0]["kind"] == "ready_to_merge"


def test_build_snapshot_keeps_wip_worktrees_active_and_marks_stale_pr_worktrees_orphaned():
    active_worktree = {
        "project": "demo",
        "name": "working-branch",
        "worktree_key": "demo/working-branch",
        "worktree_path": "/tmp/working-branch",
        "branch": "working-branch",
        "repo_full_name": "acme/demo",
        "pr_number": None,
        "dirty": True,
        "ahead": 2,
        "behind": 0,
    }
    stale_worktree = {
        "project": "demo",
        "name": "old-pr",
        "worktree_key": "demo/old-pr",
        "worktree_path": "/tmp/old-pr",
        "branch": "pr/9-old-pr",
        "repo_full_name": "acme/demo",
        "pr_number": 9,
        "dirty": False,
        "ahead": 0,
        "behind": 0,
    }

    snapshot = build_snapshot(
        [],
        [active_worktree, stale_worktree],
        {"workflow_states": {}, "queue_overrides": {}, "queue_runs": {}},
        ["reflection-agent", "devin-ai-integration"],
    )

    by_title = {entity["title"]: entity for entity in snapshot["entities"]}
    assert by_title["working-branch"]["lifecycle_state"] == "active"
    assert by_title["working-branch"]["status_state"] == "yellow"
    assert by_title["working-branch"]["queue_ids"] == []
    assert by_title["old-pr"]["lifecycle_state"] == "orphaned"
    assert by_title["old-pr"]["queue_ids"] == ["delete_worktree:wt:demo/old-pr"]


def test_github_pr_detail_query_avoids_invalid_review_thread_updated_at():
    assert "reviewThreads(first: 100)" in GitHubClient.PR_THREADS_QUERY
    assert "updatedAt" in GitHubClient.PR_THREADS_QUERY.split("reviewThreads(first: 100)", 1)[1]


def test_build_overview_drops_heavy_entity_fields():
    snapshot = build_snapshot(
        [make_pr(number=55, status_nodes=[{"name": "ci", "conclusion": "SUCCESS"}])],
        [],
        {"workflow_states": {}, "queue_overrides": {}, "queue_runs": {}},
        ["reflection-agent", "devin-ai-integration"],
    )

    overview = build_overview(snapshot)

    assert "details" not in overview["entities"][0]
    assert "unresolved_threads" not in overview["entities"][0]
    assert set(overview["queue"][0].keys()) == {"id", "entity_id", "owner", "title", "status"}


@pytest.mark.asyncio
async def test_set_workflow_state_returns_compact_response(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {"entities": [], "queue": [], "summary": {}}
    server.overview = build_overview(server.snapshot)

    async def fake_refresh_snapshot():
        return server.snapshot

    server.refresh_snapshot = fake_refresh_snapshot

    response = await server.handle_command(
        {"cmd": "set_workflow_state", "entity_id": "pr:acme/demo#12", "state": "working"}
    )

    assert response == {
        "status": "ok",
        "entity_id": "pr:acme/demo#12",
        "workflow_state": "working",
    }
    assert server.store.data["workflow_states"]["pr:acme/demo#12"] == "working"


@pytest.mark.asyncio
async def test_auto_dispatch_runs_create_worktree_for_agent_owned_item(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {
        "entities": [
            {
                "id": "pr:acme/demo#12",
                "kind": "pr",
                "worktree_path": None,
            }
        ],
        "queue": [
            {
                "id": "create_worktree:pr:acme/demo#12",
                "entity_id": "pr:acme/demo#12",
                "kind": "create_worktree",
                "owner": "agent",
                "status": "pending",
            }
        ],
        "summary": {},
    }

    calls = []

    async def fake_create_worktree(entity_id: str):
        calls.append(entity_id)
        return {"status": "ok", "worktree_path": "/tmp/demo-pr-12"}

    server.create_worktree_for_entity = fake_create_worktree

    await server.maybe_dispatch_queue()

    assert calls == ["pr:acme/demo#12"]


@pytest.mark.asyncio
async def test_auto_dispatch_skips_new_work_for_entity_with_running_item(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {
        "entities": [
            {
                "id": "pr:acme/demo#12",
                "kind": "pr",
                "worktree_path": "/tmp/demo-12",
            }
        ],
        "queue": [
            {
                "id": "resolve_review_thread:pr:acme/demo#12:t1",
                "entity_id": "pr:acme/demo#12",
                "kind": "resolve_review_thread",
                "owner": "agent",
                "status": "running",
            },
            {
                "id": "fix_ci:pr:acme/demo#12",
                "entity_id": "pr:acme/demo#12",
                "kind": "fix_ci",
                "owner": "agent",
                "status": "pending",
            },
        ],
        "summary": {},
    }

    calls = []

    async def fake_ensure_agent_session(raw_key: str, cwd: str):
        calls.append((raw_key, cwd))
        return "session"

    server.session_manager.ensure_agent_session = fake_ensure_agent_session

    await server.maybe_dispatch_queue()

    assert calls == []


@pytest.mark.asyncio
async def test_refresh_normalizes_duplicate_running_items(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.store.data["queue_runs"] = {
        "resolve_review_thread:pr:acme/demo#12:t1": {
            "entity_id": "pr:acme/demo#12",
            "session_name": "s",
            "started_at": "2026-03-18T01:00:00Z",
            "status": "running",
        },
        "resolve_review_thread:pr:acme/demo#12:t2": {
            "entity_id": "pr:acme/demo#12",
            "session_name": "s",
            "started_at": "2026-03-18T01:01:00Z",
            "status": "running",
        },
    }
    snapshot = {
        "entities": [{"id": "pr:acme/demo#12"}],
        "queue": [
            {"id": "resolve_review_thread:pr:acme/demo#12:t1", "entity_id": "pr:acme/demo#12", "status": "running"},
            {"id": "resolve_review_thread:pr:acme/demo#12:t2", "entity_id": "pr:acme/demo#12", "status": "running"},
        ],
        "summary": {},
    }

    async def fake_session_exists(session_name: str) -> bool:
        return True

    server.session_manager.session_exists = fake_session_exists

    await server._normalize_running_queue_items(snapshot)

    assert "resolve_review_thread:pr:acme/demo#12:t1" in server.store.data["queue_runs"]
    assert "resolve_review_thread:pr:acme/demo#12:t2" not in server.store.data["queue_runs"]


@pytest.mark.asyncio
async def test_refresh_clears_running_item_when_session_missing(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.store.data["queue_runs"] = {
        "resolve_review_thread:pr:acme/demo#12:t1": {
            "entity_id": "pr:acme/demo#12",
            "session_name": "missing-session",
            "started_at": "2026-03-18T01:00:00Z",
            "status": "running",
        },
    }
    snapshot = {
        "entities": [{"id": "pr:acme/demo#12"}],
        "queue": [
            {"id": "resolve_review_thread:pr:acme/demo#12:t1", "entity_id": "pr:acme/demo#12", "status": "running"},
        ],
        "summary": {},
    }

    async def fake_session_exists(session_name: str) -> bool:
        assert session_name == "missing-session"
        return False

    server.session_manager.session_exists = fake_session_exists

    await server._normalize_running_queue_items(snapshot)

    assert "resolve_review_thread:pr:acme/demo#12:t1" not in server.store.data["queue_runs"]


def test_session_output_indicates_completion_for_claude_prompt():
    output = "\n".join(
        [
            "Applied fix and pushed successfully.",
            "✻ Churned for 3m 12s",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · PR #20073",
        ]
    )
    assert session_output_indicates_completion(output) is True


def test_is_github_rate_limit_error():
    assert is_github_rate_limit_error("GraphQL: API rate limit already exceeded for user ID 123.") is True
    assert is_github_rate_limit_error("other failure") is False


@pytest.mark.asyncio
async def test_refresh_marks_running_item_completed_when_session_returns_to_prompt(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.store.data["queue_runs"] = {
        "resolve_review_thread:pr:acme/demo#12:t1": {
            "entity_id": "pr:acme/demo#12",
            "session_name": "live-session",
            "started_at": "2026-03-18T01:00:00Z",
            "status": "running",
        },
    }
    snapshot = {
        "entities": [{"id": "pr:acme/demo#12"}],
        "queue": [
            {"id": "resolve_review_thread:pr:acme/demo#12:t1", "entity_id": "pr:acme/demo#12", "status": "running"},
        ],
        "summary": {},
    }

    async def fake_session_exists(session_name: str) -> bool:
        assert session_name == "live-session"
        return True

    async def fake_capture_output(session_name: str, lines: int = 120) -> str:
        assert session_name == "live-session"
        return "\n".join(
            [
                "The workspace is clean and ready for the next review signal.",
                "✻ Churned for 3m 12s",
                "❯ ",
                "  ⏵⏵ bypass permissions on (shift+tab to cycle) · PR #20073",
            ]
        )

    server.session_manager.session_exists = fake_session_exists
    server.session_manager.capture_output = fake_capture_output

    await server._normalize_running_queue_items(snapshot)

    assert server.store.data["queue_runs"]["resolve_review_thread:pr:acme/demo#12:t1"]["status"] == "completed"


@pytest.mark.asyncio
async def test_refresh_snapshot_serves_stale_snapshot_during_github_rate_limit(tmp_path, monkeypatch):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_snapshot_path"] = str(tmp_path / "review_snapshot.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {
        "generated_at": "now",
        "entities": [
            {
                "id": "pr:acme/demo#1",
                "kind": "pr",
                "title": "demo",
                "subtitle": "acme/demo#1",
                "workflow_state": "review_loop",
                "lifecycle_state": "active",
                "reviews_state": "yellow",
                "reviews_summary": "pending",
                "ci_state": "yellow",
                "ci_summary": "pending",
                "status_state": "yellow",
                "status_summary": "pending",
                "ready_to_merge": False,
                "current_remediation": None,
            }
        ],
        "queue": [],
        "summary": {"entity_count": 1},
    }
    server.overview = build_overview(server.snapshot)

    async def fake_fetch_prs():
        raise RuntimeError("GraphQL: API rate limit already exceeded for user ID 123.")

    async def fake_scan_worktrees():
        return []

    monkeypatch.setattr(server, "fetch_prs", fake_fetch_prs)
    monkeypatch.setattr(server, "scan_worktrees", fake_scan_worktrees)

    snapshot = await server.refresh_snapshot()

    assert snapshot == server.snapshot
    assert server._rate_limited_until > 0


def test_server_loads_cached_snapshot_on_start(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_snapshot_path"] = str(tmp_path / "review_snapshot.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    snapshot = {
        "generated_at": "now",
        "entities": [
            {
                "id": "pr:acme/demo#1",
                "kind": "pr",
                "title": "demo",
                "subtitle": "acme/demo#1",
                "workflow_state": "review_loop",
                "lifecycle_state": "active",
                "reviews_state": "yellow",
                "reviews_summary": "pending",
                "ci_state": "yellow",
                "ci_summary": "pending",
                "status_state": "yellow",
                "status_summary": "pending",
                "ready_to_merge": False,
                "current_remediation": None,
            }
        ],
        "queue": [],
        "summary": {"entity_count": 1},
    }
    Path(config.config["review_snapshot_path"]).write_text(json.dumps(snapshot))

    server = ReviewLoopServer(config)

    assert server.snapshot == snapshot
    assert server.overview["summary"]["entity_count"] == 1


def test_delete_entity_state_cleans_all_queue_and_workflow_state(tmp_path):
    from contxt_review_server import ReviewLoopStore

    store = ReviewLoopStore(tmp_path / "review_state.json")
    store.data = {
        "workflow_states": {"wt:demo/cross": "working", "pr:acme/demo#1": "review_loop"},
        "queue_overrides": {"delete_worktree:wt:demo/cross": {"owner": "human"}, "x": {"owner": "agent"}},
        "queue_runs": {"delete_worktree:wt:demo/cross": {"status": "running"}, "x": {"status": "running"}},
        "thread_cache": {},
    }

    store.delete_entity_state("wt:demo/cross", ["delete_worktree:wt:demo/cross"])

    assert "wt:demo/cross" not in store.data["workflow_states"]
    assert "delete_worktree:wt:demo/cross" not in store.data["queue_overrides"]
    assert "delete_worktree:wt:demo/cross" not in store.data["queue_runs"]
    assert "pr:acme/demo#1" in store.data["workflow_states"]


def test_server_prunes_stale_local_worktree_entities_from_cached_snapshot(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_snapshot_path"] = str(tmp_path / "review_snapshot.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    snapshot = {
        "generated_at": "now",
        "entities": [
            {
                "id": "wt:demo/cross",
                "kind": "worktree",
                "title": "cross",
                "subtitle": "demo/cross",
                "worktree_key": "demo/cross",
                "worktree_path": str(tmp_path / "worktrees" / "demo" / "cross"),
                "workflow_state": "working",
                "lifecycle_state": "orphaned",
                "reviews_state": "grey",
                "reviews_summary": "No PR attached",
                "ci_state": "grey",
                "ci_summary": "No PR attached",
                "status_state": "grey",
                "status_summary": "Delete or archive this worktree",
                "ready_to_merge": False,
                "queue_ids": ["delete_worktree:wt:demo/cross"],
                "current_remediation": None,
            }
        ],
        "queue": [
            {
                "id": "delete_worktree:wt:demo/cross",
                "entity_id": "wt:demo/cross",
                "owner": "agent",
                "title": "Delete worktree",
                "status": "pending",
            }
        ],
        "summary": {"entity_count": 1, "queue_count": 1, "ready_to_merge_count": 0},
    }
    Path(config.config["review_snapshot_path"]).write_text(json.dumps(snapshot))
    state_path = Path(config.config["review_state_path"])
    state_path.write_text(
        json.dumps(
            {
                "workflow_states": {"wt:demo/cross": "working"},
                "queue_overrides": {},
                "queue_runs": {},
            }
        )
    )

    server = ReviewLoopServer(config)
    server.metadata_file = tmp_path / ".contxt_metadata.json"
    server.metadata_file.write_text("{}")
    pruned = server._prune_stale_local_entities(snapshot)

    assert pruned["entities"] == []
    assert pruned["queue"] == []


def test_update_overview_meta_marks_rate_limit(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_snapshot_path"] = str(tmp_path / "review_snapshot.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {"generated_at": "now", "entities": [], "queue": [], "summary": {"entity_count": 0, "queue_count": 0, "ready_to_merge_count": 0}}
    server._rate_limited_until = 10**12
    server._update_overview_meta()

    assert server.overview["summary"]["rate_limited"] is True
    assert "rate_limit_retry_in_seconds" in server.overview["summary"]


@pytest.mark.asyncio
async def test_delete_worktree_prunes_snapshot_even_when_refresh_is_rate_limited(tmp_path, monkeypatch):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_snapshot_path"] = str(tmp_path / "review_snapshot.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.metadata_file = tmp_path / ".contxt_metadata.json"
    worktree_path = tmp_path / "worktrees" / "demo" / "cross"
    worktree_path.mkdir(parents=True)
    original_repo = tmp_path / "repo"
    original_repo.mkdir(parents=True)
    server.metadata_file.write_text(
        json.dumps(
            {
                "demo": {
                    "cross": {
                        "worktree_path": str(worktree_path),
                        "original_repo": str(original_repo),
                        "branch": "cross",
                    }
                }
            }
        )
    )
    server.snapshot = {
        "generated_at": "now",
        "entities": [
            {
                "id": "wt:demo/cross",
                "kind": "worktree",
                "title": "cross",
                "subtitle": "demo/cross",
                "worktree_key": "demo/cross",
                "workflow_state": "working",
                "lifecycle_state": "orphaned",
                "reviews_state": "grey",
                "reviews_summary": "No PR attached",
                "ci_state": "grey",
                "ci_summary": "No PR attached",
                "status_state": "grey",
                "status_summary": "Delete or archive this worktree",
                "ready_to_merge": False,
                "queue_ids": ["delete_worktree:wt:demo/cross"],
                "current_remediation": None,
            }
        ],
        "queue": [
            {
                "id": "delete_worktree:wt:demo/cross",
                "entity_id": "wt:demo/cross",
                "owner": "agent",
                "title": "Delete worktree",
                "status": "pending",
            }
        ],
        "summary": {"entity_count": 1, "queue_count": 1, "ready_to_merge_count": 0},
    }
    server.overview = build_overview(server.snapshot)
    server.store.data["workflow_states"] = {"wt:demo/cross": "working"}

    async def fake_refresh_snapshot():
        raise RuntimeError("GraphQL: API rate limit already exceeded for user ID 123.")

    monkeypatch.setattr(server, "refresh_snapshot", fake_refresh_snapshot)
    monkeypatch.setattr("contxt_review_server.subprocess.run", lambda *args, **kwargs: None)

    response = await server.delete_worktree_for_entity("wt:demo/cross")

    assert response["status"] == "ok"
    assert server.snapshot["entities"] == []
    assert server.snapshot["queue"] == []
    assert "wt:demo/cross" not in server.store.data["workflow_states"]


@pytest.mark.asyncio
async def test_fetch_prs_uses_cached_threads_for_unchanged_pr(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_snapshot_path"] = str(tmp_path / "review_snapshot.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.store.data["thread_cache"] = {
        "acme/demo#12": {
            "updated_at": "2026-03-17T00:00:00Z",
            "nodes": [{"id": "thread-1", "comments": {"nodes": []}}],
        }
    }

    async def fake_search_authored_prs(limit: int = 100):
        return [{"number": 12, "repository": {"nameWithOwner": "acme/demo"}}]

    async def fake_get_pr_detail(repo_full_name: str, number: int):
        return {
            "number": number,
            "updatedAt": "2026-03-17T00:00:00Z",
            "repository": {"nameWithOwner": repo_full_name},
        }

    async def fake_get_pr_threads(repo_full_name: str, number: int):
        raise AssertionError("unchanged PR should use cached review threads")

    server.github.search_authored_prs = fake_search_authored_prs
    server.github.get_pr_detail = fake_get_pr_detail
    server.github.get_pr_threads = fake_get_pr_threads

    prs = await server.fetch_prs()

    assert prs[0]["reviewThreads"]["nodes"][0]["id"] == "thread-1"


def test_build_agent_prompt_includes_full_review_body_and_resolution_instructions(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_snapshot_path"] = str(tmp_path / "review_snapshot.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    entity = {
        "subtitle": "reflectionai/olympus#20299",
        "worktree_path": "/tmp/opti-crusoe",
        "pr_url": "https://github.com/reflectionai/olympus/pull/20299",
        "unresolved_threads": [
            {
                "id": "thread-1",
                "reviewer_login": "reflection-agent",
                "summary": "This eviction logic is a critical performance bottleneck.",
                "body": "Full review body here.",
                "url": "https://github.com/reflectionai/olympus/pull/20299#discussion_r1",
            }
        ],
    }
    queue_item = {
        "id": "resolve_review_thread:pr:reflectionai/olympus#20299:thread-1",
        "title": "Address agent review",
        "kind": "resolve_review_thread",
        "description": "Address review",
    }

    prompt = server.build_agent_prompt(entity, queue_item)

    assert 'Goal: "Address agent review"' in prompt
    assert "Worktree: /tmp/opti-crusoe" in prompt
    assert "URL: https://github.com/reflectionai/olympus/pull/20299#discussion_r1" in prompt
    assert "Full review body here." in prompt
    assert "Add a GitHub reply comment on the thread describing the fix." in prompt
    assert "Resolve the review thread after the fix is pushed and the reply is posted." in prompt


@pytest.mark.asyncio
async def test_search_authored_prs_uses_rest_search_mapping():
    client = GitHubClient()

    async def fake_gh_json(*args, **kwargs):
        assert args[:2] == ("api", "search/issues")
        return {
            "items": [
                {
                    "number": 12,
                    "title": "Demo",
                    "html_url": "https://github.com/acme/demo/pull/12",
                    "updated_at": "2026-03-17T00:00:00Z",
                    "user": {"login": "gabe"},
                    "repository_url": "https://api.github.com/repos/acme/demo",
                }
            ]
        }

    client._gh_json = fake_gh_json

    results = await client.search_authored_prs()

    assert results == [
        {
            "number": 12,
            "title": "Demo",
            "url": "https://github.com/acme/demo/pull/12",
            "updatedAt": "2026-03-17T00:00:00Z",
            "author": {"login": "gabe"},
            "repository": {"nameWithOwner": "acme/demo"},
        }
    ]


@pytest.mark.asyncio
async def test_create_worktree_uses_contxt_create_command(monkeypatch, tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {
        "entities": [
            {
                "id": "pr:acme/demo#12",
                "kind": "pr",
                "repo_full_name": "acme/demo",
                "pr_number": 12,
                "branch": "feature-12",
            }
        ],
        "queue": [],
        "summary": {},
    }
    server._last_prs = [
        {
            "number": 12,
            "repository": {
                "nameWithOwner": "acme/demo",
                "sshUrl": "git@github.com:acme/demo.git",
            },
        }
    ]

    async def fake_ensure_repo_cache(repo_full_name: str, ssh_url: str):
        return tmp_path / "repos" / "acme" / "demo"

    async def fake_refresh_snapshot():
        return server.snapshot

    calls = []

    class FakeProcess:
        def __init__(self, returncode=0, stderr=b""):
            self.returncode = returncode
            self._stderr = stderr

        async def communicate(self):
            return (b"", self._stderr)

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr(server, "ensure_repo_cache", fake_ensure_repo_cache)
    monkeypatch.setattr(server, "refresh_snapshot", fake_refresh_snapshot)
    monkeypatch.setattr("contxt_review_server.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    response = await server.create_worktree_for_entity("pr:acme/demo#12")

    assert response["status"] == "ok"
    assert calls[0][:5] == ("git", "-C", str(tmp_path / "repos" / "acme" / "demo"), "fetch", "origin")
    assert "python" in calls[1][0]
    assert calls[1][2] == "create"
    assert "--repo-root" in calls[1]
    assert "--branch" in calls[1]
    assert "--base-ref" in calls[1]
    assert "--no-open" in calls[1]


@pytest.mark.asyncio
async def test_capture_entity_session_uses_current_remediation_session(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {
        "entities": [
            {
                "id": "pr:acme/demo#12",
                "current_remediation": {
                    "session_name": "contxt-review-pr-acme-demo-12",
                    "status": "running",
                },
            }
        ],
        "queue": [],
        "summary": {},
    }

    async def fake_capture_output(session_name: str, lines: int = 80):
        assert session_name == "contxt-review-pr-acme-demo-12"
        assert lines == 80
        return "live agent output"

    server.session_manager.capture_output = fake_capture_output

    response = await server.handle_command(
        {"cmd": "capture_entity_session", "entity_id": "pr:acme/demo#12", "lines": 80}
    )

    assert response["status"] == "ok"
    assert response["output"] == "live agent output"


@pytest.mark.asyncio
async def test_scan_worktrees_discovers_untracked_worktree_and_backfills_metadata(monkeypatch, tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.worktrees_base = tmp_path / "worktrees"
    server.metadata_file = server.worktrees_base / ".contxt_metadata.json"
    server.worktrees_base.mkdir(parents=True, exist_ok=True)
    (server.worktrees_base / "olympus" / "pr-12-feature-12").mkdir(parents=True)
    server.metadata_file.write_text("{}")

    responses = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "feature-12",
        ("remote", "get-url", "origin"): "git@github.com:acme/demo.git",
        ("rev-parse", "--path-format=absolute", "--git-common-dir"): str(tmp_path / "repos" / "demo" / ".git"),
        ("status", "--porcelain"): "",
    }

    async def fake_git_output(cwd: str, *args: str) -> str:
        key = tuple(args)
        if key in responses:
            return responses[key]
        raise RuntimeError("missing")

    monkeypatch.setattr(server, "_git_output", fake_git_output)

    worktrees = await server.scan_worktrees()

    assert len(worktrees) == 1
    assert worktrees[0]["branch"] == "feature-12"
    assert worktrees[0]["repo_full_name"] == "acme/demo"
    metadata = json.loads(server.metadata_file.read_text())
    assert metadata["olympus"]["pr-12-feature-12"]["branch"] == "feature-12"
    assert metadata["olympus"]["pr-12-feature-12"]["original_repo"] == str(tmp_path / "repos" / "demo")


def test_build_queue_item_detail_includes_action_context(tmp_path):
    config = ContxtConfig()
    config.config["review_socket_path"] = str(tmp_path / "review.sock")
    config.config["review_state_path"] = str(tmp_path / "review_state.json")
    config.config["review_repo_cache_path"] = str(tmp_path / "repos")
    config.config["review_log_path"] = str(tmp_path / "review.log")

    server = ReviewLoopServer(config)
    server.snapshot = {
        "entities": [
            {
                "id": "pr:acme/demo#12",
                "subtitle": "acme/demo#12",
                "branch": "feature-12",
                "worktree_path": "/tmp/demo-12",
                "unresolved_threads": [],
            }
        ],
        "queue": [
            {
                "id": "fix_ci:pr:acme/demo#12",
                "entity_id": "pr:acme/demo#12",
                "kind": "fix_ci",
                "owner": "agent",
                "status": "pending",
                "title": "Fix CI",
                "description": "CI failed",
                "prompt": "",
            }
        ],
        "summary": {},
    }

    detail = server.build_queue_item_detail("fix_ci:pr:acme/demo#12")

    assert detail is not None
    assert "Kind: fix_ci" in detail
    assert "Agent prompt:" in detail
