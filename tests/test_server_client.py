import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import chain

import pytest


def test_concurrent_todo_updates_preserve_data(running_server, server_client_factory):
    server, socket_path, layout = running_server
    key = layout["primary_key"]

    client_a = server_client_factory()
    client_b = server_client_factory()

    initial = client_a.send_command({"cmd": "get_todos", "key": key})
    assert initial["status"] == "ok"
    base_version = initial["version"]
    existing_count = len(initial["todos"])

    barrier = threading.Barrier(2)
    operations = {
        "client_a": [{"op": "add", "text": "client A entry"}],
        "client_b": [{"op": "add", "text": "client B entry"}],
    }

    def issue_update(client, ops):
        barrier.wait()
        response = client.send_command(
            {"cmd": "update_todos", "key": key, "operations": ops, "version": base_version}
        )
        return client, ops, response

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(issue_update, client_a, operations["client_a"]),
            pool.submit(issue_update, client_b, operations["client_b"]),
        ]
        results = [future.result() for future in futures]

    statuses = {resp["status"] for _, _, resp in results}
    assert statuses == {"ok", "conflict"}

    conflict_client = None
    conflict_ops = None
    conflict_resp = None
    success_version = None
    for client, ops, resp in results:
        if resp["status"] == "conflict":
            conflict_client = client
            conflict_ops = ops
            conflict_resp = resp
        else:
            success_version = resp["version"]

    assert conflict_client and conflict_ops and conflict_resp
    assert success_version is not None

    retry = conflict_client.send_command(
        {
            "cmd": "update_todos",
            "key": key,
            "operations": conflict_ops,
            "version": conflict_resp["version"],
        }
    )
    assert retry["status"] == "ok"

    snapshot = client_a.send_command({"cmd": "get_todos", "key": key})
    assert snapshot["status"] == "ok"
    texts = [todo["text"] for todo in snapshot["todos"]]
    assert "client A entry" in texts
    assert "client B entry" in texts
    assert len(snapshot["todos"]) == existing_count + 2


def test_parallel_session_inputs_keep_every_command(
    running_server, server_client_factory, worktree_layout
):
    server, socket_path, layout = running_server
    key = layout["primary_key"]
    path = layout["primary_path"]

    starter = server_client_factory()
    start_resp = starter.send_command({"cmd": "start_session", "key": key, "path": path})
    assert start_resp["status"] == "ok"

    # Kick off several writers to stress ordering and delivery.
    writers = [server_client_factory() for _ in range(3)]
    payloads = [
        [f"writer-{idx}-cmd-{i}\n" for i in range(3)]
        for idx, _ in enumerate(writers)
    ]
    barrier = threading.Barrier(len(writers))

    def spam(client, data):
        barrier.wait()
        for chunk in data:
            resp = client.send_command({"cmd": "write_input", "key": key, "data": chunk})
            assert resp["status"] == "ok"

    with ThreadPoolExecutor(max_workers=len(writers)) as pool:
        futures = [pool.submit(spam, client, data) for client, data in zip(writers, payloads)]
        for future in futures:
            future.result()

    # ask for latest output to force the server to touch the fake PTY buffers.
    output_resp = starter.send_command({"cmd": "get_output", "key": key, "lines": 5})
    assert output_resp["status"] == "ok"

    session = server.sessions[key]
    flattened = list(chain.from_iterable(payloads))
    recorded = session.command_log

    assert len(recorded) == len(flattened)
    for chunk in flattened:
        assert chunk in recorded
    recent_lines = output_resp["output"]
    assert all(line.startswith("writer-") for line in recent_lines)
