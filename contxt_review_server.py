#!/usr/bin/env python3
"""
Async review-loop server for GitHub PR orchestration.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from contxt_config import ContxtConfig
from contxt_review_logging import ReviewLogger
from contxt_sessions import SessionError, SessionManager


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def parse_repo_full_name(remote_url: str) -> Optional[str]:
    remote_url = remote_url.strip()
    patterns = [
        r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, remote_url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def slugify(text: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return compact or "item"


def summarize_text(text: str, limit: int = 120) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def is_github_rate_limit_error(message: str) -> bool:
    text = (message or "").lower()
    return "rate limit" in text and "github" in text or "graphql: api rate limit" in text


def determine_ci_state(status_rollup: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    if not status_rollup:
        return ("yellow", "No CI signal yet")

    contexts = status_rollup.get("contexts", {}).get("nodes", [])
    saw_success = False
    saw_pending = False
    for context in contexts:
        state = (
            context.get("conclusion")
            or context.get("status")
            or context.get("state")
            or ""
        ).upper()
        if state in {
            "FAILURE",
            "FAILED",
            "ERROR",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
        }:
            name = context.get("name") or context.get("context") or "CI"
            return ("red", f"{name} failed")
        if state in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"}:
            saw_pending = True
        if state in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            saw_success = True

    if saw_pending:
        return ("yellow", "CI is still running")
    if saw_success:
        return ("green", "CI is green")
    return ("yellow", "Waiting for CI")


def classify_thread(
    pr: Dict[str, Any], thread: Dict[str, Any], agent_authors: List[str]
) -> Optional[Dict[str, Any]]:
    if thread.get("isResolved"):
        return None

    comments = thread.get("comments", {}).get("nodes", [])
    if not comments:
        return None

    pr_author = ((pr.get("author") or {}).get("login") or "").lower()
    agent_set = {author.lower() for author in agent_authors}
    reviewer_login = ""
    for comment in comments:
        login = ((comment.get("author") or {}).get("login") or "").lower()
        if login and login != pr_author:
            reviewer_login = login
            break
    if not reviewer_login:
        reviewer_login = ((comments[-1].get("author") or {}).get("login") or "").lower()

    reviewer_type = "agent" if reviewer_login in agent_set else "human"
    last_comment = comments[-1]
    return {
        "id": thread["id"],
        "url": last_comment.get("url") or pr.get("url"),
        "updated_at": thread.get("updatedAt") or last_comment.get("publishedAt"),
        "reviewer_login": reviewer_login,
        "reviewer_type": reviewer_type,
        "summary": summarize_text(last_comment.get("body", "")),
        "body": last_comment.get("body", ""),
    }


def compute_review_state(
    pr: Dict[str, Any], unresolved_threads: List[Dict[str, Any]]
) -> Tuple[str, str]:
    if unresolved_threads:
        human_count = sum(1 for thread in unresolved_threads if thread["reviewer_type"] == "human")
        agent_count = len(unresolved_threads) - human_count
        parts = []
        if human_count:
            parts.append(f"{human_count} human")
        if agent_count:
            parts.append(f"{agent_count} agent")
        return ("red", f"Unresolved threads: {', '.join(parts)}")

    decision = (pr.get("reviewDecision") or "").upper()
    if decision == "APPROVED":
        return ("green", "Reviews are clear")
    return ("yellow", "Waiting for review signal")


def status_chip_for_pr(
    pr: Optional[Dict[str, Any]], worktree: Optional[Dict[str, Any]]
) -> Tuple[str, str]:
    if pr is None:
        return ("yellow", "PR not submitted")

    merge_state = (pr.get("mergeStateStatus") or "").upper()
    if merge_state == "BEHIND":
        return ("red", "Behind base branch")

    if worktree:
        if worktree.get("dirty"):
            return ("red", "Local changes not pushed")
        if worktree.get("ahead", 0) > 0:
            return ("red", "Local branch has unpushed commits")
    return ("green", "PR submitted")


def github_ready_to_merge(
    pr: Dict[str, Any], review_state: str, ci_state: str, status_state: str
) -> bool:
    if review_state != "green" or ci_state != "green" or status_state != "green":
        return False
    if (pr.get("reviewDecision") or "").upper() != "APPROVED":
        return False
    merge_state = (pr.get("mergeStateStatus") or "").upper()
    return merge_state in {"CLEAN", "HAS_HOOKS", "UNSTABLE"}


def queue_run_state(store: Dict[str, Any], queue_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    run = store.get("queue_runs", {}).get(queue_id)
    if not run:
        return ("pending", None)
    return (run.get("status", "pending"), run)


def build_queue_item(
    entity_id: str,
    kind: str,
    owner: str,
    priority: int,
    title: str,
    description: str,
    detail_key: str = "",
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    queue_id = ":".join(part for part in [kind, entity_id, detail_key] if part)
    return {
        "id": queue_id,
        "entity_id": entity_id,
        "kind": kind,
        "owner": owner,
        "priority": priority,
        "title": title,
        "description": description,
        "prompt": prompt or "",
    }


def session_output_indicates_completion(output: str) -> bool:
    if not output:
        return False

    lines = [line.rstrip() for line in output.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return False

    tail = lines[-12:]
    tail_text = "\n".join(tail).lower()
    has_prompt = any(re.match(r"^[❯>$]\s*$", line.strip()) for line in tail)
    has_footer = "bypass permissions on" in tail_text
    return has_prompt and has_footer


def build_snapshot(
    prs: List[Dict[str, Any]],
    worktrees: List[Dict[str, Any]],
    store: Dict[str, Any],
    agent_authors: List[str],
) -> Dict[str, Any]:
    worktrees_by_key = {worktree["worktree_key"]: worktree for worktree in worktrees}
    unmatched_worktrees = set(worktrees_by_key.keys())

    pr_entities: List[Dict[str, Any]] = []
    queue: List[Dict[str, Any]] = []

    for pr in prs:
        repo_full_name = pr["repository"]["nameWithOwner"]
        entity_id = f"pr:{repo_full_name}#{pr['number']}"
        matching_worktree = None

        for worktree in worktrees:
            if worktree.get("pr_number") == pr["number"] and worktree.get("repo_full_name") == repo_full_name:
                matching_worktree = worktree
                break
            if worktree.get("repo_full_name") == repo_full_name and worktree.get("branch") == pr.get("headRefName"):
                matching_worktree = worktree
                break

        if matching_worktree:
            unmatched_worktrees.discard(matching_worktree["worktree_key"])

        unresolved_threads = []
        for thread in pr.get("reviewThreads", {}).get("nodes", []):
            classified = classify_thread(pr, thread, agent_authors)
            if classified:
                unresolved_threads.append(classified)

        review_state, review_summary = compute_review_state(pr, unresolved_threads)
        ci_state, ci_summary = determine_ci_state(pr.get("statusCheckRollup"))
        pr_state, pr_summary = status_chip_for_pr(pr, matching_worktree)
        workflow_state = store.get("workflow_states", {}).get(
            entity_id,
            "review_loop" if pr else "working",
        )
        ready_to_merge = github_ready_to_merge(pr, review_state, ci_state, pr_state)

        current_run = None
        entity_queue: List[Dict[str, Any]] = []

        if not matching_worktree:
            entity_queue.append(
                build_queue_item(
                    entity_id,
                    "create_worktree",
                    "agent",
                    100,
                    "Create worktree",
                    "No contxt worktree exists for this PR.",
                )
            )

        for thread in unresolved_threads:
            owner = "agent" if thread["reviewer_type"] == "agent" else "human"
            entity_queue.append(
                build_queue_item(
                    entity_id,
                    "resolve_review_thread",
                    owner,
                    90 if owner == "human" else 80,
                    f"Address {thread['reviewer_type']} review",
                    thread["summary"] or "Unresolved review thread.",
                    detail_key=thread["id"],
                )
            )

        if ci_state == "red":
            entity_queue.append(
                build_queue_item(
                    entity_id,
                    "fix_ci",
                    "agent",
                    70,
                    "Fix CI",
                    ci_summary,
                )
            )

        if workflow_state == "review_loop" and not pr.get("url"):
            entity_queue.append(
                build_queue_item(
                    entity_id,
                    "submit_pr",
                    "agent",
                    65,
                    "Submit PR",
                    "Branch is ready for the review loop but no PR exists.",
                )
            )

        if workflow_state == "review_loop" and review_state == "green" and ci_state == "green" and pr_state == "green":
            if ready_to_merge:
                entity_queue.append(
                    build_queue_item(
                        entity_id,
                        "ready_to_merge",
                        "human",
                        55,
                        "Ready to merge",
                        "GitHub says this PR is ready to merge.",
                    )
                )
            else:
                entity_queue.append(
                    build_queue_item(
                        entity_id,
                        "request_reviewers",
                        "human",
                        60,
                        "Send to reviewers",
                        "Everything is green. Request final human review.",
                    )
                )

        if not entity_queue and pr_state == "red":
            entity_queue.append(
                build_queue_item(
                    entity_id,
                    "rebase_branch",
                    "agent",
                    30,
                    "Rebase onto main",
                    "Do the final branch refresh once other blockers are clear.",
                )
            )

        for item in entity_queue:
            override = store.get("queue_overrides", {}).get(item["id"], {})
            if override.get("owner") in {"agent", "human"}:
                item["owner"] = override["owner"]
            if override.get("prompt"):
                item["prompt"] = override["prompt"]
            status, run = queue_run_state(store, item["id"])
            item["status"] = status
            if run and current_run is None and status == "running":
                current_run = run

        queue.extend(entity_queue)
        pr_entities.append(
            {
                "id": entity_id,
                "kind": "pr",
                "title": matching_worktree.get("name") if matching_worktree else pr["headRefName"],
                "subtitle": f"{repo_full_name}#{pr['number']}",
                "repo_full_name": repo_full_name,
                "pr_number": pr["number"],
                "pr_url": pr["url"],
                "branch": pr["headRefName"],
                "base_branch": pr["baseRefName"],
                "worktree_key": matching_worktree.get("worktree_key") if matching_worktree else None,
                "worktree_path": matching_worktree.get("worktree_path") if matching_worktree else None,
                "workflow_state": workflow_state,
                "lifecycle_state": "missing_worktree" if not matching_worktree else "active",
                "reviews_state": review_state,
                "reviews_summary": review_summary,
                "ci_state": ci_state,
                "ci_summary": ci_summary,
                "status_state": pr_state,
                "status_summary": pr_summary,
                "ready_to_merge": ready_to_merge,
                "github_ready": ready_to_merge,
                "unresolved_threads": unresolved_threads,
                "queue_ids": [item["id"] for item in entity_queue],
                "current_remediation": current_run,
                "details": {
                    "merge_state_status": pr.get("mergeStateStatus"),
                    "review_decision": pr.get("reviewDecision"),
                    "mergeable": pr.get("mergeable"),
                    "is_draft": pr.get("isDraft"),
                    "updated_at": pr.get("updatedAt"),
                    "latest_reviews": pr.get("latestReviews", {}).get("nodes", []),
                    "review_requests": pr.get("reviewRequests", {}).get("nodes", []),
                    "worktree_dirty": matching_worktree.get("dirty") if matching_worktree else False,
                    "worktree_ahead": matching_worktree.get("ahead") if matching_worktree else 0,
                    "worktree_behind": matching_worktree.get("behind") if matching_worktree else 0,
                },
            }
        )

    extra_entities: List[Dict[str, Any]] = []
    for key in unmatched_worktrees:
        worktree = worktrees_by_key[key]
        entity_id = f"wt:{key}"
        workflow_state = store.get("workflow_states", {}).get(entity_id, "working")
        is_orphaned = bool(worktree.get("pr_number"))
        entity_queue: List[Dict[str, Any]] = []
        lifecycle_state = "orphaned" if is_orphaned else "active"
        reviews_state = "grey"
        reviews_summary = "No PR submitted yet"
        ci_state = "grey"
        ci_summary = "No PR submitted yet"
        status_state = "yellow"
        status_summary = "PR not submitted"

        if is_orphaned:
            lifecycle_state = "orphaned"
            reviews_summary = "No open PR attached"
            ci_summary = "No open PR attached"
            status_state = "grey"
            status_summary = "Delete or archive this worktree"
            entity_queue.append(
                build_queue_item(
                    entity_id,
                    "delete_worktree",
                    "human",
                    40,
                    "Delete worktree",
                    "This worktree no longer has an open PR.",
                )
            )
        elif workflow_state == "review_loop":
            entity_queue.append(
                build_queue_item(
                    entity_id,
                    "submit_pr",
                    "agent",
                    65,
                    "Submit PR",
                    "This worktree is in the review loop but has not been submitted.",
                )
            )

        for item in entity_queue:
            status, run = queue_run_state(store, item["id"])
            item["status"] = status
        queue.extend(entity_queue)
        extra_entities.append(
            {
                "id": entity_id,
                "kind": "worktree",
                "title": worktree["name"],
                "subtitle": key,
                "repo_full_name": worktree.get("repo_full_name"),
                "pr_number": worktree.get("pr_number"),
                "pr_url": None,
                "branch": worktree.get("branch"),
                "base_branch": None,
                "worktree_key": key,
                "worktree_path": worktree.get("worktree_path"),
                "workflow_state": workflow_state,
                "lifecycle_state": lifecycle_state,
                "reviews_state": reviews_state,
                "reviews_summary": reviews_summary,
                "ci_state": ci_state,
                "ci_summary": ci_summary,
                "status_state": status_state,
                "status_summary": status_summary,
                "ready_to_merge": False,
                "github_ready": False,
                "unresolved_threads": [],
                "queue_ids": [item["id"] for item in entity_queue],
                "current_remediation": None,
                "details": {
                    "worktree_dirty": worktree.get("dirty"),
                    "worktree_ahead": worktree.get("ahead"),
                    "worktree_behind": worktree.get("behind"),
                    "branch": worktree.get("branch"),
                },
            }
        )

    entities = pr_entities + extra_entities
    queue.sort(key=lambda item: (-item["priority"], item["title"], item["id"]))
    entities.sort(
        key=lambda entity: (
            1 if entity["lifecycle_state"] == "orphaned" else 0,
            entity["title"].lower(),
            entity["subtitle"].lower(),
        )
    )

    active_queue_ids = {item["id"] for item in queue}
    stale_runs = [
        queue_id
        for queue_id in list(store.get("queue_runs", {}).keys())
        if queue_id not in active_queue_ids
    ]
    for queue_id in stale_runs:
        store["queue_runs"].pop(queue_id, None)

    return {
        "generated_at": utc_now(),
        "entities": entities,
        "queue": queue,
        "summary": {
            "entity_count": len(entities),
            "queue_count": len(queue),
            "ready_to_merge_count": sum(1 for entity in entities if entity["ready_to_merge"]),
        },
    }


def build_overview(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    entities = []
    for entity in snapshot.get("entities", []):
        entities.append(
            {
                "id": entity["id"],
                "kind": entity["kind"],
                "title": entity["title"],
                "subtitle": entity["subtitle"],
                "pr_url": entity.get("pr_url"),
                "worktree_path": entity.get("worktree_path"),
                "worktree_key": entity.get("worktree_key"),
                "workflow_state": entity["workflow_state"],
                "lifecycle_state": entity["lifecycle_state"],
                "reviews_state": entity["reviews_state"],
                "reviews_summary": entity["reviews_summary"],
                "ci_state": entity["ci_state"],
                "ci_summary": entity["ci_summary"],
                "status_state": entity["status_state"],
                "status_summary": entity["status_summary"],
                "ready_to_merge": entity["ready_to_merge"],
                "current_remediation": entity.get("current_remediation"),
            }
        )

    queue = []
    for item in snapshot.get("queue", []):
        queue.append(
            {
                "id": item["id"],
                "entity_id": item["entity_id"],
                "owner": item["owner"],
                "title": item["title"],
                "status": item.get("status", "pending"),
            }
        )

    return {
        "generated_at": snapshot.get("generated_at"),
        "summary": dict(snapshot.get("summary", {})),
        "entities": entities,
        "queue": queue,
    }


class GitHubClient:
    """Thin async wrapper around `gh`."""

    PR_THREADS_QUERY = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  isOutdated
                  updatedAt
                  comments(first: 20) {
                    nodes {
                      body
                      url
                      createdAt
                      publishedAt
                      author { login }
                    }
                  }
                }
              }
            }
          }
        }
        """

    def __init__(self) -> None:
        self._detail_semaphore = asyncio.Semaphore(6)
        self._thread_semaphore = asyncio.Semaphore(3)

    async def _gh_json(self, *args: str, cwd: Optional[str] = None) -> Any:
        process = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or "gh command failed")
        text = stdout.decode("utf-8", errors="replace").strip()
        return json.loads(text) if text else None

    async def search_authored_prs(self, limit: int = 100) -> List[Dict[str, Any]]:
        payload = await self._gh_json(
            "api",
            "search/issues",
            "-f",
            "q=is:pr is:open author:@me",
            "-f",
            f"per_page={limit}",
        )
        results = []
        for item in payload.get("items", []):
            repo_url = item.get("repository_url", "")
            repo_full_name = "/".join(repo_url.rstrip("/").split("/")[-2:])
            results.append(
                {
                    "number": item["number"],
                    "title": item.get("title"),
                    "url": item.get("html_url"),
                    "updatedAt": item.get("updated_at"),
                    "author": {"login": ((item.get("user") or {}).get("login"))},
                    "repository": {"nameWithOwner": repo_full_name},
                }
            )
        return results

    async def get_pr_detail(self, repo_full_name: str, number: int) -> Dict[str, Any]:
        owner, repo = repo_full_name.split("/", 1)
        async with self._detail_semaphore:
            pr_payload = await self._gh_json("api", f"repos/{owner}/{repo}/pulls/{number}")
            head_sha = ((pr_payload.get("head") or {}).get("sha"))
            reviews, requested_reviewers, check_runs, commit_status = await asyncio.gather(
                self._gh_json("api", f"repos/{owner}/{repo}/pulls/{number}/reviews", "-f", "per_page=100"),
                self._gh_json("api", f"repos/{owner}/{repo}/pulls/{number}/requested_reviewers"),
                self._gh_json(
                    "api",
                    f"repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-f",
                    "per_page=100",
                ),
                self._gh_json("api", f"repos/{owner}/{repo}/commits/{head_sha}/status"),
            )

        latest_reviews = self._latest_reviews(reviews, pr_payload.get("user", {}).get("login"))
        pr = {
            "number": pr_payload["number"],
            "title": pr_payload.get("title"),
            "url": pr_payload.get("html_url"),
            "isDraft": pr_payload.get("draft", False),
            "headRefName": ((pr_payload.get("head") or {}).get("ref")),
            "baseRefName": ((pr_payload.get("base") or {}).get("ref")),
            "mergeStateStatus": (pr_payload.get("mergeable_state") or "").upper(),
            "mergeable": "MERGEABLE" if pr_payload.get("mergeable") else "CONFLICTING",
            "reviewDecision": self._review_decision(latest_reviews),
            "updatedAt": pr_payload.get("updated_at"),
            "author": {"login": ((pr_payload.get("user") or {}).get("login"))},
            "latestReviews": {"nodes": latest_reviews},
            "reviewRequests": {"nodes": self._review_requests(requested_reviewers)},
            "reviewThreads": {"nodes": []},
            "statusCheckRollup": {"contexts": {"nodes": self._status_nodes(check_runs, commit_status)}},
            "repository": {
                "nameWithOwner": repo_full_name,
                "sshUrl": self._ssh_url(pr_payload.get("head", {}).get("repo"), repo_full_name),
                "defaultBranchRef": {"name": (((pr_payload.get("base") or {}).get("repo") or {}).get("default_branch") or "main")},
            },
        }
        return pr

    async def get_pr_threads(self, repo_full_name: str, number: int) -> Dict[str, Any]:
        owner, repo = repo_full_name.split("/", 1)
        async with self._thread_semaphore:
            payload = await self._gh_json(
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={repo}",
                "-F",
                f"number={number}",
                "-f",
                f"query={self.PR_THREADS_QUERY}",
            )
        return payload["data"]["repository"]["pullRequest"]["reviewThreads"]

    def _latest_reviews(self, reviews: List[Dict[str, Any]], pr_author: Optional[str]) -> List[Dict[str, Any]]:
        latest_by_user: Dict[str, Dict[str, Any]] = {}
        for review in reviews:
            login = ((review.get("user") or {}).get("login") or "").lower()
            if not login or login == (pr_author or "").lower():
                continue
            state = (review.get("state") or "").upper()
            if state not in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}:
                continue
            current = latest_by_user.get(login)
            submitted_at = review.get("submitted_at") or ""
            if current and (current.get("submittedAt") or "") > submitted_at:
                continue
            latest_by_user[login] = {
                "state": state,
                "submittedAt": submitted_at,
                "author": {"login": login},
            }
        return list(latest_by_user.values())

    def _review_decision(self, latest_reviews: List[Dict[str, Any]]) -> str:
        states = {review.get("state") for review in latest_reviews}
        if "CHANGES_REQUESTED" in states:
            return "REVIEW_REQUIRED"
        if "APPROVED" in states:
            return "APPROVED"
        return "REVIEW_REQUIRED"

    def _review_requests(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        nodes = []
        for user in payload.get("users", []):
            nodes.append({"requestedReviewer": {"__typename": "User", "login": user.get("login")}})
        for team in payload.get("teams", []):
            nodes.append(
                {
                    "requestedReviewer": {
                        "__typename": "Team",
                        "name": team.get("name"),
                        "slug": team.get("slug"),
                    }
                }
            )
        return nodes

    def _status_nodes(self, check_runs: Dict[str, Any], commit_status: Dict[str, Any]) -> List[Dict[str, Any]]:
        nodes = []
        for run in check_runs.get("check_runs", []):
            nodes.append(
                {
                    "__typename": "CheckRun",
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "detailsUrl": run.get("html_url"),
                }
            )
        for status in commit_status.get("statuses", []):
            nodes.append(
                {
                    "__typename": "StatusContext",
                    "context": status.get("context"),
                    "state": status.get("state"),
                    "targetUrl": status.get("target_url"),
                    "description": status.get("description"),
                }
            )
        return nodes

    def _ssh_url(self, repo_payload: Optional[Dict[str, Any]], repo_full_name: str) -> str:
        if repo_payload and repo_payload.get("ssh_url"):
            return repo_payload["ssh_url"]
        return f"git@github.com:{repo_full_name}.git"


class ReviewLoopStore:
    """Persist workflow overrides and queue dispatch bookkeeping."""

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        data = load_json(
            self.path,
            {
                "workflow_states": {},
                "queue_overrides": {},
                "queue_runs": {},
            },
        )
        for key in ["workflow_states", "queue_overrides", "queue_runs"]:
            data.setdefault(key, {})
        return data

    def save(self) -> None:
        save_json(self.path, self.data)

    def set_workflow_state(self, entity_id: str, state: str) -> None:
        self.data["workflow_states"][entity_id] = state
        self.save()

    def set_queue_override(self, queue_id: str, owner: Optional[str], prompt: Optional[str]) -> None:
        override = self.data["queue_overrides"].setdefault(queue_id, {})
        if owner:
            override["owner"] = owner
        if prompt is not None:
            override["prompt"] = prompt
        self.save()

    def start_run(self, queue_id: str, entity_id: str, session_name: Optional[str]) -> None:
        self.data["queue_runs"][queue_id] = {
            "status": "running",
            "entity_id": entity_id,
            "session_name": session_name,
            "started_at": utc_now(),
        }
        self.save()

    def complete_run(self, queue_id: str, status: str = "completed") -> None:
        if queue_id in self.data["queue_runs"]:
            self.data["queue_runs"][queue_id]["status"] = status
            self.data["queue_runs"][queue_id]["finished_at"] = utc_now()
            self.save()

    def clear_run(self, queue_id: str) -> None:
        if queue_id in self.data["queue_runs"]:
            self.data["queue_runs"].pop(queue_id, None)
            self.save()

    def delete_entity_state(self, entity_id: str, queue_ids: Optional[List[str]] = None) -> None:
        changed = False
        if entity_id in self.data["workflow_states"]:
            self.data["workflow_states"].pop(entity_id, None)
            changed = True
        for queue_id in queue_ids or []:
            if queue_id in self.data["queue_overrides"]:
                self.data["queue_overrides"].pop(queue_id, None)
                changed = True
            if queue_id in self.data["queue_runs"]:
                self.data["queue_runs"].pop(queue_id, None)
                changed = True
        if changed:
            self.save()

    def delete_entity_state_multi(self, entity_ids: set[str], queue_ids: List[str]) -> None:
        changed = False
        for entity_id in entity_ids:
            if entity_id in self.data["workflow_states"]:
                self.data["workflow_states"].pop(entity_id, None)
                changed = True
        for queue_id in queue_ids:
            if queue_id in self.data["queue_overrides"]:
                self.data["queue_overrides"].pop(queue_id, None)
                changed = True
            if queue_id in self.data["queue_runs"]:
                self.data["queue_runs"].pop(queue_id, None)
                changed = True
        if changed:
            self.save()


class ReviewLoopServer:
    """Poll GitHub/worktree state, synthesize queue items, and manage agent sessions."""

    def __init__(self, config: ContxtConfig):
        self.config = config
        self.socket_path = Path(config.get("review_socket_path"))
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(config.get("review_state_path"))
        self.snapshot_path = Path(config.get("review_snapshot_path"))
        self.worktrees_base = Path.home() / "worktrees"
        self.metadata_file = self.worktrees_base / ".contxt_metadata.json"
        self.repo_cache_path = Path(config.get("review_repo_cache_path"))
        self.repo_cache_path.mkdir(parents=True, exist_ok=True)
        self.github = GitHubClient()
        self.store = ReviewLoopStore(self.state_path)
        self.logger = ReviewLogger(config.get("review_log_path"), "server")
        self.snapshot: Dict[str, Any] = load_json(
            self.snapshot_path,
            {"generated_at": utc_now(), "entities": [], "queue": [], "summary": {}},
        )
        self.snapshot = self._prune_stale_local_entities(self.snapshot)
        self.overview: Dict[str, Any] = build_overview(self.snapshot)
        self.running = True
        self.server: Optional[asyncio.AbstractServer] = None
        self._refresh_lock = asyncio.Lock()
        self.session_manager = SessionManager(
            config.get("session_backend", "auto"),
            config.get("review_agent_command") or config.get("agent_command", "claude"),
            config.get("review_session_prefix", "contxt-review"),
        )
        self.agent_authors = config.get("review_agent_authors", ["reflection-agent", "devin-ai-integration"])
        self._rate_limited_until = 0.0
        self._update_overview_meta()
        self.logger.info(
            "review server initialized",
            socket_path=str(self.socket_path),
            state_path=str(self.state_path),
            session_backend=config.get("session_backend", "auto"),
        )

    def _prune_stale_local_entities(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        metadata = load_json(self.metadata_file, {})
        removed_ids: set[str] = set()
        pruned_entities: List[Dict[str, Any]] = []
        for entity in snapshot.get("entities", []):
            if entity.get("kind") != "worktree":
                pruned_entities.append(entity)
                continue
            worktree_key = entity.get("worktree_key") or ""
            project, _, name = worktree_key.partition("/")
            metadata_entry = metadata.get(project, {}).get(name) if project and name else None
            worktree_path = entity.get("worktree_path") or (metadata_entry or {}).get("worktree_path")
            if metadata_entry and worktree_path and Path(worktree_path).exists():
                pruned_entities.append(entity)
                continue
            removed_ids.add(entity["id"])

        if not removed_ids:
            return snapshot

        self.store.delete_entity_state_multi(
            removed_ids,
            [
                item["id"]
                for item in snapshot.get("queue", [])
                if item.get("entity_id") in removed_ids
            ],
        )
        pruned_queue = [item for item in snapshot.get("queue", []) if item.get("entity_id") not in removed_ids]
        pruned_snapshot = {
            **snapshot,
            "entities": pruned_entities,
            "queue": pruned_queue,
            "generated_at": utc_now(),
            "summary": {
                "entity_count": len(pruned_entities),
                "queue_count": len(pruned_queue),
                "ready_to_merge_count": sum(1 for item in pruned_entities if item.get("ready_to_merge")),
            },
        }
        save_json(self.snapshot_path, pruned_snapshot)
        self.logger.info("pruned stale local entities from snapshot", entity_ids=sorted(removed_ids))
        return pruned_snapshot

    def _update_overview_meta(self) -> None:
        summary = dict(self.snapshot.get("summary", {}))
        if self._rate_limited_until > time.monotonic():
            summary["rate_limited"] = True
            summary["rate_limit_retry_in_seconds"] = max(0, int(self._rate_limited_until - time.monotonic()))
        else:
            summary["rate_limited"] = False
            summary.pop("rate_limit_retry_in_seconds", None)
        self.overview = {
            **build_overview(self.snapshot),
            "summary": summary,
        }

    async def _git_output(self, cwd: str, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "git command failed")
        return stdout.decode("utf-8", errors="replace").strip()

    async def _inspect_worktree(self, project: str, name: str, info: Dict[str, Any]) -> Dict[str, Any]:
        path = info.get("worktree_path")
        worktree = {
            "project": project,
            "name": name,
            "worktree_key": f"{project}/{name}",
            "worktree_path": path,
            "branch": info.get("branch"),
            "repo_full_name": info.get("repo_full_name"),
            "pr_number": info.get("pr_number"),
            "original_repo": info.get("original_repo"),
            "dirty": False,
            "ahead": 0,
            "behind": 0,
        }
        if not path or not Path(path).exists():
            return worktree

        try:
            worktree["branch"] = worktree["branch"] or await self._git_output(path, "rev-parse", "--abbrev-ref", "HEAD")
        except RuntimeError:
            return worktree

        try:
            remote_url = await self._git_output(path, "remote", "get-url", "origin")
            worktree["repo_full_name"] = worktree["repo_full_name"] or parse_repo_full_name(remote_url)
        except RuntimeError:
            pass

        if not worktree.get("original_repo"):
            try:
                common_dir = await self._git_output(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
                common_path = Path(common_dir)
                worktree["original_repo"] = str(
                    common_path.parent if common_path.name == ".git" else common_path
                )
            except RuntimeError:
                pass

        try:
            status = await self._git_output(path, "status", "--porcelain")
            worktree["dirty"] = bool(status)
        except RuntimeError:
            pass

        try:
            upstream = await self._git_output(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            counts = await self._git_output(path, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
            behind, ahead = counts.split()
            worktree["ahead"] = int(ahead)
            worktree["behind"] = int(behind)
        except RuntimeError:
            pass

        return worktree

    def _discover_untracked_worktree_paths(self, metadata: Dict[str, Any]) -> List[tuple[str, str, str]]:
        tracked_paths = {
            str(Path(info.get("worktree_path")).resolve())
            for worktrees in metadata.values()
            if isinstance(worktrees, dict)
            for info in worktrees.values()
            if isinstance(info, dict) and info.get("worktree_path")
        }

        discovered: List[tuple[str, str, str]] = []
        if not self.worktrees_base.exists():
            return discovered

        for project_dir in sorted(self.worktrees_base.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            if project_dir.name == ".venvs":
                continue
            for worktree_dir in sorted(project_dir.iterdir()):
                if not worktree_dir.is_dir() or worktree_dir.name.startswith("."):
                    continue
                resolved = str(worktree_dir.resolve())
                if resolved in tracked_paths:
                    continue
                discovered.append((project_dir.name, worktree_dir.name, resolved))
        return discovered

    def _record_discovered_worktree(self, worktree: Dict[str, Any]) -> None:
        project = worktree["project"]
        name = worktree["name"]
        path = worktree.get("worktree_path")
        branch = worktree.get("branch")
        original_repo = worktree.get("original_repo")
        if not project or not name or not path or not branch or not original_repo:
            return

        metadata = load_json(self.metadata_file, {})
        metadata.setdefault(project, {})
        current = metadata[project].get(name, {})
        updated = {
            **current,
            "worktree_path": path,
            "original_repo": original_repo,
            "branch": branch,
            "venv_path": current.get("venv_path", str(self.worktrees_base / ".venvs" / project / name)),
        }
        if worktree.get("repo_full_name"):
            updated["repo_full_name"] = worktree["repo_full_name"]
        if worktree.get("pr_number") is not None:
            updated["pr_number"] = worktree["pr_number"]

        if metadata[project].get(name) == updated:
            return

        metadata[project][name] = updated
        save_json(self.metadata_file, metadata)
        self.logger.info(
            "discovered existing worktree",
            project=project,
            name=name,
            worktree_path=path,
            branch=branch,
        )

    async def scan_worktrees(self) -> List[Dict[str, Any]]:
        metadata = load_json(self.metadata_file, {})
        tasks = []
        for project, worktrees in metadata.items():
            if not isinstance(worktrees, dict):
                continue
            for name, info in worktrees.items():
                if isinstance(info, dict):
                    tasks.append(self._inspect_worktree(project, name, info))
        for project, name, path in self._discover_untracked_worktree_paths(metadata):
            tasks.append(
                self._inspect_worktree(
                    project,
                    name,
                    {
                        "worktree_path": path,
                        "original_repo": None,
                        "branch": None,
                        "repo_full_name": None,
                        "pr_number": None,
                    },
                )
            )
        if not tasks:
            return []
        worktrees = await asyncio.gather(*tasks)
        for worktree in worktrees:
            self._record_discovered_worktree(worktree)
        return worktrees

    async def fetch_prs(self) -> List[Dict[str, Any]]:
        search_results = await self.github.search_authored_prs(limit=100)
        tasks = []
        for result in search_results:
            repo_name = result["repository"]["nameWithOwner"]
            tasks.append(self.github.get_pr_detail(repo_name, result["number"]))
        if not tasks:
            return []
        prs = await asyncio.gather(*tasks)
        thread_cache = self.store.data.setdefault("thread_cache", {})
        for pr in prs:
            cache_key = f"{pr['repository']['nameWithOwner']}#{pr['number']}"
            cached = thread_cache.get(cache_key)
            if cached and cached.get("updated_at") == pr.get("updatedAt"):
                pr["reviewThreads"] = {"nodes": cached.get("nodes", [])}
                continue
            try:
                threads = await self.github.get_pr_threads(pr["repository"]["nameWithOwner"], pr["number"])
                pr["reviewThreads"] = threads
                thread_cache[cache_key] = {
                    "updated_at": pr.get("updatedAt"),
                    "nodes": threads.get("nodes", []),
                }
            except RuntimeError as exc:
                if cached and is_github_rate_limit_error(str(exc)):
                    pr["reviewThreads"] = {"nodes": cached.get("nodes", [])}
                    continue
                raise
        self.store.save()
        return prs

    def build_agent_prompt(self, entity: Dict[str, Any], queue_item: Dict[str, Any]) -> str:
        lines = [
            f'Goal: "{queue_item["title"]}"',
            f"Worktree: {entity.get('worktree_path') or 'missing worktree'}",
        ]
        if queue_item["kind"] == "resolve_review_thread":
            thread = next(
                (
                    item
                    for item in entity.get("unresolved_threads", [])
                    if queue_item["id"].endswith(item["id"])
                ),
                None,
            )
            if thread:
                lines.extend(
                    [
                        f"URL: {thread['url']}",
                        "Constraints:",
                        "- Keep the branch pushed and green.",
                        "- Do not rebase onto main until every other blocker is clear.",
                        "- Add a GitHub reply comment on the thread describing the fix.",
                        "- Resolve the review thread after the fix is pushed and the reply is posted.",
                        "",
                        f"Reviewer: {thread['reviewer_login']}",
                        f"Review summary: {thread['summary']}",
                        "Review contents:",
                        thread.get("body") or "(full review body unavailable)",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"URL: {entity.get('pr_url') or 'unknown'}",
                        "Constraints:",
                        "- Keep the branch pushed and green.",
                        "- Do not rebase onto main until every other blocker is clear.",
                        "- Add a GitHub reply comment on the thread describing the fix.",
                        "- Resolve the review thread after the fix is pushed and the reply is posted.",
                        "",
                        "Review contents:",
                        "(thread body unavailable in local state; look it up from the linked PR if needed)",
                    ]
                )
        else:
            lines.extend(
                [
                    f"URL: {entity.get('pr_url') or 'unknown'}",
                    "Constraints:",
                    "- Keep the branch pushed and green.",
                    "- Do not rebase onto main until every other blocker is clear.",
                    "",
                    f"Context: {queue_item['description']}",
                ]
            )
        lines.append("")
        lines.append("Leave the workspace clean, pushed, and ready for the next review signal when you stop.")
        return "\n".join(lines)

    def build_queue_item_detail(self, queue_id: str) -> Optional[str]:
        item = next((entry for entry in self.snapshot.get("queue", []) if entry["id"] == queue_id), None)
        if not item:
            return None
        entity = next((entry for entry in self.snapshot.get("entities", []) if entry["id"] == item["entity_id"]), None)

        lines = [
            f"Title: {item['title']}",
            f"Kind: {item['kind']}",
            f"Owner: {item['owner']}",
            f"Status: {item.get('status', 'pending')}",
            f"Target: {item['entity_id']}",
            "",
            item.get("description", ""),
        ]

        if item["kind"] == "create_worktree" and entity:
            lines.extend(
                [
                    "",
                    "Scripted action:",
                    f"- create worktree for {entity['subtitle']}",
                    f"- branch: {entity.get('branch') or 'unknown'}",
                ]
            )
        elif item["kind"] == "delete_worktree" and entity:
            lines.extend(
                [
                    "",
                    "Scripted action:",
                    f"- delete worktree {entity.get('worktree_key') or entity['id']}",
                ]
            )
        elif entity:
            prompt = item.get("prompt") or self.build_agent_prompt(entity, item)
            lines.extend(
                [
                    "",
                    "Agent prompt:",
                    prompt,
                ]
            )

        return "\n".join(line for line in lines if line is not None)

    async def ensure_repo_cache(self, repo_full_name: str, ssh_url: str) -> Path:
        owner, repo = repo_full_name.split("/", 1)
        repo_root = self.repo_cache_path / owner / repo
        if repo_root.exists():
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo_root),
                "fetch",
                "--all",
                "--prune",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.communicate()
            return repo_root
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            "gh",
            "repo",
            "clone",
            repo_full_name,
            str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode != 0:
            process = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                ssh_url,
                str(repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "failed to clone repo")
        return repo_root

    async def create_worktree_for_entity(self, entity_id: str) -> Dict[str, Any]:
        self.logger.info("create worktree requested", entity_id=entity_id)
        entity = next((item for item in self.snapshot["entities"] if item["id"] == entity_id), None)
        if not entity or entity["kind"] != "pr":
            self.logger.error("create worktree failed", entity_id=entity_id, reason="pr entity not found")
            return {"status": "error", "message": "PR entity not found"}

        pr = next(
            (
                pr
                for pr in self._last_prs
                if pr["number"] == entity["pr_number"] and pr["repository"]["nameWithOwner"] == entity["repo_full_name"]
            ),
            None,
        )
        if not pr:
            self.logger.error("create worktree failed", entity_id=entity_id, reason="pr data unavailable")
            return {"status": "error", "message": "PR data not available"}

        repo_root = await self.ensure_repo_cache(
            entity["repo_full_name"],
            pr["repository"]["sshUrl"],
        )
        fetch = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_root),
            "fetch",
            "origin",
            entity["branch"],
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await fetch.communicate()
        if fetch.returncode != 0:
            self.logger.error(
                "create worktree fetch failed",
                entity_id=entity_id,
                stderr=stderr.decode("utf-8", errors="replace").strip(),
            )
            return {"status": "error", "message": stderr.decode("utf-8", errors="replace").strip()}

        repo_name = entity["repo_full_name"].split("/", 1)[1]
        worktree_name = f"pr-{entity['pr_number']}-{slugify(entity['branch'])}"[:64]
        worktree_path = self.worktrees_base / repo_name / worktree_name
        contxt_entrypoint = Path(__file__).resolve().with_name("contxt")
        create = await asyncio.create_subprocess_exec(
            sys.executable,
            str(contxt_entrypoint),
            "create",
            worktree_name,
            "-p",
            repo_name,
            "--repo-root",
            str(repo_root),
            "--branch",
            entity["branch"],
            "--base-ref",
            f"origin/{entity['branch']}",
            "--no-open",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await create.communicate()
        if create.returncode != 0:
            self.logger.error(
                "create worktree contxt create failed",
                entity_id=entity_id,
                stderr=stderr.decode("utf-8", errors="replace").strip(),
            )
            return {"status": "error", "message": stderr.decode("utf-8", errors="replace").strip()}

        await self.refresh_snapshot()
        self.logger.info(
            "create worktree completed",
            entity_id=entity_id,
            worktree_path=str(worktree_path),
            local_branch=entity["branch"],
        )
        return {"status": "ok", "worktree_path": str(worktree_path)}

    async def delete_worktree_for_entity(self, entity_id: str) -> Dict[str, Any]:
        self.logger.info("delete worktree requested", entity_id=entity_id)
        entity = next((item for item in self.snapshot["entities"] if item["id"] == entity_id), None)
        if not entity or not entity.get("worktree_key"):
            self.logger.error("delete worktree failed", entity_id=entity_id, reason="worktree entity not found")
            return {"status": "error", "message": "Worktree entity not found"}

        metadata = load_json(self.metadata_file, {})
        project, name = entity["worktree_key"].split("/", 1)
        worktree_info = metadata.get(project, {}).get(name)
        if not worktree_info:
            self.logger.error("delete worktree failed", entity_id=entity_id, reason="metadata not found")
            return {"status": "error", "message": "Worktree metadata not found"}

        worktree_path = worktree_info["worktree_path"]
        original_repo = worktree_info.get("original_repo")
        branch_name = worktree_info.get("branch")

        if original_repo and Path(worktree_path).exists():
            subprocess.run(
                ["git", "-C", original_repo, "worktree", "remove", "--force", worktree_path],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if branch_name and original_repo:
            subprocess.run(
                ["git", "-C", original_repo, "branch", "-D", branch_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if Path(worktree_path).exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

        del metadata[project][name]
        if not metadata[project]:
            del metadata[project]
        save_json(self.metadata_file, metadata)
        self.store.delete_entity_state(entity_id, entity.get("queue_ids", []))
        self.snapshot["entities"] = [item for item in self.snapshot.get("entities", []) if item["id"] != entity_id]
        self.snapshot["queue"] = [item for item in self.snapshot.get("queue", []) if item.get("entity_id") != entity_id]
        self.snapshot["summary"] = {
            "entity_count": len(self.snapshot["entities"]),
            "queue_count": len(self.snapshot["queue"]),
            "ready_to_merge_count": sum(1 for item in self.snapshot["entities"] if item.get("ready_to_merge")),
        }
        self.snapshot["generated_at"] = utc_now()
        self._update_overview_meta()
        save_json(self.snapshot_path, self.snapshot)
        try:
            await self.refresh_snapshot()
        except RuntimeError as exc:
            self.logger.error(
                "delete worktree refresh failed; keeping locally pruned snapshot",
                entity_id=entity_id,
                error=str(exc),
            )
        self.logger.info("delete worktree completed", entity_id=entity_id, worktree_key=entity["worktree_key"])
        return {"status": "ok"}

    async def refresh_snapshot(self) -> Dict[str, Any]:
        async with self._refresh_lock:
            self.snapshot = self._prune_stale_local_entities(self.snapshot)
            self._update_overview_meta()
            if self._rate_limited_until > time.monotonic():
                self.logger.info(
                    "refresh snapshot skipped during github rate-limit backoff",
                    retry_in_seconds=max(0, int(self._rate_limited_until - time.monotonic())),
                )
                return self.snapshot
            started = time.monotonic()
            self.logger.info("refresh snapshot started")
            try:
                prs, worktrees = await asyncio.gather(self.fetch_prs(), self.scan_worktrees())
                self._last_prs = prs
                snapshot = build_snapshot(prs, worktrees, self.store.data, self.agent_authors)
                await self._normalize_running_queue_items(snapshot)
                snapshot = build_snapshot(prs, worktrees, self.store.data, self.agent_authors)
                self.snapshot = snapshot
                save_json(self.state_path, self.store.data)
                save_json(self.snapshot_path, self.snapshot)
                self._rate_limited_until = 0.0
                self._update_overview_meta()
                self.logger.info(
                    "refresh snapshot completed",
                    entity_count=snapshot["summary"].get("entity_count", 0),
                    queue_count=snapshot["summary"].get("queue_count", 0),
                    ready_to_merge_count=snapshot["summary"].get("ready_to_merge_count", 0),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                return snapshot
            except RuntimeError as exc:
                if is_github_rate_limit_error(str(exc)):
                    self.snapshot = self._prune_stale_local_entities(self.snapshot)
                    self._rate_limited_until = time.monotonic() + 300
                    self._update_overview_meta()
                    self.logger.error(
                        "github rate limit hit; serving stale snapshot during backoff",
                        retry_in_seconds=300,
                        error=str(exc),
                    )
                    return self.snapshot
                raise

    async def _normalize_running_queue_items(self, snapshot: Dict[str, Any]) -> None:
        for item in snapshot.get("queue", []):
            if item.get("status") != "running":
                continue
            run = self.store.data.get("queue_runs", {}).get(item["id"], {})
            session_name = run.get("session_name")
            if not session_name:
                self.logger.info("clearing running queue item with no session", queue_id=item["id"])
                self.store.clear_run(item["id"])
                continue
            if not await self.session_manager.session_exists(session_name):
                self.logger.info(
                    "clearing stale running queue item with missing session",
                    queue_id=item["id"],
                    session_name=session_name,
                )
                self.store.clear_run(item["id"])
                continue
            try:
                output = await self.session_manager.capture_output(session_name, 120)
            except Exception as exc:
                self.logger.error(
                    "capture output failed while normalizing running item",
                    queue_id=item["id"],
                    session_name=session_name,
                    error=str(exc),
                )
                continue
            if session_output_indicates_completion(output):
                self.logger.info(
                    "marking queue item completed from session output",
                    queue_id=item["id"],
                    session_name=session_name,
                )
                self.store.complete_run(item["id"])

        running_by_entity: Dict[str, list[Dict[str, Any]]] = {}
        for item in snapshot.get("queue", []):
            if item.get("status") == "running":
                running_by_entity.setdefault(item["entity_id"], []).append(item)

        changed = False
        for entity_id, items in running_by_entity.items():
            if len(items) <= 1:
                continue
            items.sort(
                key=lambda item: self.store.data.get("queue_runs", {}).get(item["id"], {}).get("started_at", "")
            )
            for item in items[1:]:
                self.logger.info(
                    "clearing duplicate running queue item",
                    entity_id=entity_id,
                    queue_id=item["id"],
                )
                self.store.clear_run(item["id"])
                changed = True

        if changed:
            self.logger.info("normalized running queue items")

    async def maybe_dispatch_queue(self) -> None:
        if not self.config.get("review_auto_dispatch_agents", True):
            return
        running_entities = {
            item["entity_id"]
            for item in self.snapshot.get("queue", [])
            if item.get("status") == "running"
        }
        for item in self.snapshot.get("queue", []):
            if item["owner"] != "agent" or item["status"] == "running":
                continue
            if item["kind"] in {"delete_worktree", "ready_to_merge", "request_reviewers"}:
                continue
            if item["entity_id"] in running_entities:
                continue
            if item["kind"] == "create_worktree":
                self.logger.info(
                    "auto dispatch create worktree starting",
                    queue_id=item["id"],
                    entity_id=item["entity_id"],
                )
                result = await self.create_worktree_for_entity(item["entity_id"])
                if result.get("status") != "ok":
                    self.logger.error(
                        "auto dispatch create worktree failed",
                        queue_id=item["id"],
                        entity_id=item["entity_id"],
                        detail=result.get("message"),
                    )
                else:
                    self.logger.info(
                        "auto dispatch create worktree completed",
                        queue_id=item["id"],
                        entity_id=item["entity_id"],
                        worktree_path=result.get("worktree_path"),
                    )
                return
            entity = next((candidate for candidate in self.snapshot["entities"] if candidate["id"] == item["entity_id"]), None)
            if not entity or not entity.get("worktree_path"):
                continue
            session_name = None
            try:
                self.logger.info(
                    "auto dispatch starting",
                    queue_id=item["id"],
                    entity_id=item["entity_id"],
                    kind=item["kind"],
                )
                session_name = await self.session_manager.ensure_agent_session(item["entity_id"], entity["worktree_path"])
                prompt = item["prompt"] or self.build_agent_prompt(entity, item)
                await self.session_manager.send_prompt(session_name, prompt)
                self.store.start_run(item["id"], item["entity_id"], session_name)
                self.logger.info(
                    "auto dispatch completed",
                    queue_id=item["id"],
                    entity_id=item["entity_id"],
                    session_name=session_name,
                )
            except (RuntimeError, SessionError) as exc:
                self.store.set_queue_override(item["id"], None, f"{item.get('prompt', '')}\nDispatch failed: {exc}")
                self.logger.error(
                    "auto dispatch failed",
                    queue_id=item["id"],
                    entity_id=item["entity_id"],
                    error=str(exc),
                )
            await self.refresh_snapshot()
            return

    async def handle_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        name = command.get("cmd")
        self.logger.info("command received", command=name)
        if name == "get_overview":
            self.snapshot = self._prune_stale_local_entities(self.snapshot)
            self._update_overview_meta()
            return {"status": "ok", "overview": self.overview}
        if name == "refresh_overview":
            await self.refresh_snapshot()
            return {"status": "ok", "overview": self.overview}
        if name == "get_snapshot":
            return {"status": "ok", "snapshot": self.snapshot}
        if name == "refresh_now":
            snapshot = await self.refresh_snapshot()
            return {"status": "ok", "snapshot": snapshot}
        if name == "set_workflow_state":
            current = self.store.data.get("workflow_states", {}).get(command["entity_id"])
            self.store.set_workflow_state(command["entity_id"], command["state"])
            self.logger.info(
                "workflow state updated",
                entity_id=command["entity_id"],
                previous_state=current,
                new_state=command["state"],
            )
            await self.refresh_snapshot()
            return {
                "status": "ok",
                "entity_id": command["entity_id"],
                "workflow_state": command["state"],
            }
        if name == "set_queue_owner":
            self.store.set_queue_override(command["queue_id"], command.get("owner"), command.get("prompt"))
            self.logger.info(
                "queue owner updated",
                queue_id=command["queue_id"],
                owner=command.get("owner"),
                prompt=command.get("prompt"),
            )
            await self.refresh_snapshot()
            return {
                "status": "ok",
                "queue_id": command["queue_id"],
                "owner": command.get("owner"),
            }
        if name == "dispatch_queue_item":
            queue_id = command["queue_id"]
            item = next((entry for entry in self.snapshot["queue"] if entry["id"] == queue_id), None)
            if not item:
                self.logger.error("queue dispatch failed", queue_id=queue_id, reason="item not found")
                return {"status": "error", "message": "Queue item not found"}
            entity = next((entry for entry in self.snapshot["entities"] if entry["id"] == item["entity_id"]), None)
            if item["kind"] == "create_worktree":
                return await self.create_worktree_for_entity(item["entity_id"])
            if item["kind"] == "delete_worktree":
                return await self.delete_worktree_for_entity(item["entity_id"])
            if not entity or not entity.get("worktree_path"):
                self.logger.error("queue dispatch failed", queue_id=queue_id, reason="no worktree")
                return {"status": "error", "message": "No worktree available for this item"}
            session_name = await self.session_manager.ensure_agent_session(item["entity_id"], entity["worktree_path"])
            prompt = command.get("prompt") or item["prompt"] or self.build_agent_prompt(entity, item)
            await self.session_manager.send_prompt(session_name, prompt)
            self.store.start_run(queue_id, item["entity_id"], session_name)
            self.logger.info(
                "queue item dispatched",
                queue_id=queue_id,
                entity_id=item["entity_id"],
                kind=item["kind"],
                session_name=session_name,
            )
            await self.refresh_snapshot()
            return {
                "status": "ok",
                "queue_id": queue_id,
                "entity_id": item["entity_id"],
                "session_name": session_name,
            }
        if name == "get_queue_item_detail":
            detail = self.build_queue_item_detail(command["queue_id"])
            if detail is None:
                return {"status": "error", "message": "Queue item not found"}
            return {"status": "ok", "detail": detail}
        if name == "capture_session":
            queue_id = command["queue_id"]
            run = self.store.data.get("queue_runs", {}).get(queue_id)
            if not run or not run.get("session_name"):
                return {"status": "error", "message": "No active session for queue item"}
            output = await self.session_manager.capture_output(run["session_name"], command.get("lines", 80))
            return {"status": "ok", "output": output}
        if name == "capture_entity_session":
            entity_id = command["entity_id"]
            entity = next((entry for entry in self.snapshot["entities"] if entry["id"] == entity_id), None)
            remediation = entity.get("current_remediation") if entity else None
            session_name = remediation.get("session_name") if remediation else None
            if not session_name:
                return {"status": "error", "message": "No active remediation session for entity"}
            output = await self.session_manager.capture_output(session_name, command.get("lines", 80))
            return {"status": "ok", "output": output, "session_name": session_name}
        if name == "shutdown":
            self.running = False
            if self.server:
                self.server.close()
            self.logger.info("shutdown requested")
            return {"status": "ok"}
        return {"status": "error", "message": f"Unknown command: {name}"}

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                command = json.loads(line.decode("utf-8"))
                response = await self.handle_command(command)
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
                if command.get("cmd") == "shutdown":
                    break
        except Exception as exc:
            self.logger.error("client handler failed", error=str(exc))
            writer.write((json.dumps({"status": "error", "message": str(exc)}) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def refresh_loop(self, interval: int) -> None:
        while self.running:
            try:
                await self.refresh_snapshot()
            except Exception as exc:
                self.logger.error("refresh loop failed", error=str(exc))
            await asyncio.sleep(interval)

    async def dispatch_loop(self, interval: int) -> None:
        while self.running:
            try:
                await self.maybe_dispatch_queue()
            except Exception as exc:
                self.logger.error("dispatch loop failed", error=str(exc))
            await asyncio.sleep(interval)

    async def run(self) -> None:
        self._last_prs: List[Dict[str, Any]] = []
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(self.handle_client, path=str(self.socket_path))
        self.logger.info("server listening", socket_path=str(self.socket_path))
        initial_refresh = asyncio.create_task(self.refresh_snapshot())
        refresh_task = asyncio.create_task(self.refresh_loop(int(self.config.get("review_poll_seconds", 90))))
        dispatch_task = asyncio.create_task(self.dispatch_loop(int(self.config.get("review_dispatch_seconds", 10))))
        try:
            async with self.server:
                await self.server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            initial_refresh.cancel()
            refresh_task.cancel()
            dispatch_task.cancel()
            self.logger.info("server stopped")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await initial_refresh
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await refresh_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dispatch_task
            if self.socket_path.exists():
                self.socket_path.unlink()


async def async_main() -> None:
    config = ContxtConfig()
    server = ReviewLoopServer(config)
    await server.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
