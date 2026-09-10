import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "work_memory_probe", Path(__file__).parents[1] / "scripts/work_memory_probe.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Client:
    def __init__(self, *, foreign=0, limit=1024, fail=None):
        self.foreign, self.limit, self.fail = foreign, limit, fail
        self.owned, self.calls, self.counter = {}, [], 0

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        value = {"status": "ok"}
        if name == "browser_status":
            value.update(
                scheduler={"active_sessions": self.foreign + len(self.owned)},
                resources={
                    "cgroup_limit_mb": self.limit,
                    "cgroup_used_mb": 100 + len(self.owned) * 200,
                },
            )
        elif name == "browser_open":
            self.counter += 1
            sid = f"private_session_{self.counter}"
            self.owned[sid] = f"secret_lease_{self.counter}"
            value.update(session_id=sid, lease_id=self.owned[sid], tab_id=f"tab_{self.counter}")
            if self.fail == "partial_open" and self.counter == 2:
                value.update(status="error", error={"code": "RESOURCE_PRESSURE"})
            if self.fail == "foreign_arrival":
                self.foreign = 1
        elif name == "browser_close":
            assert self.owned[args["session_id"]] == args["lease_id"]
            if self.fail == "first_close" and args["session_id"].endswith("2"):
                raise RuntimeError("must-not-leak-secret")
            del self.owned[args["session_id"]]
        elif name == "browser_observe" and self.fail == "capture" and args["mode"] == "visual":
            value.update(status="error", error={"code": "RESOURCE_PRESSURE"})
        images = (
            [SimpleNamespace(type="image")]
            if name == "browser_observe" and args["mode"] == "visual" and value["status"] == "ok"
            else []
        )
        return SimpleNamespace(structured_content=value, content=images)


async def run(client, mode="dual"):
    return await module.probe(
        client, mode=mode, url="https://example.com/private-query", settle_seconds=0
    )


@pytest.mark.parametrize("mode,total", [("single", 1), ("dual", 2)])
async def test_independent_workloads_cleanup_and_public_safe_report(mode, total):
    client = Client()
    result = await run(client, mode)
    assert result["result"] == "completed" and not client.owned
    assert client.counter == total
    assert not result["cleanup_errors"]
    output = json.dumps(result)
    assert all(
        secret not in output for secret in ("private_session", "secret_lease", "private-query")
    )


@pytest.mark.parametrize(
    "options,reason",
    [({"foreign": 1}, "FOREIGN_OR_UNACCOUNTED_WORK"), ({"limit": 1400}, "BUDGET_MISMATCH")],
)
async def test_probe_refuses_foreign_work_or_unlike_budget(options, reason):
    client = Client(**options)
    result = await run(client)
    assert result["result"] == "stopped" and result["reason"] == reason
    assert all(name == "browser_status" for name, _ in client.calls)


@pytest.mark.parametrize("failure", ["capture", "partial_open", "foreign_arrival"])
async def test_failure_cleans_only_created_work_without_retry(failure):
    client = Client(fail=failure)
    result = await run(client)
    assert result["result"] == "stopped" and not client.owned
    assert client.foreign == (1 if failure == "foreign_arrival" else 0)
    assert (
        sum(name == "browser_observe" and args["mode"] == "visual" for name, args in client.calls)
        <= 1
    )


async def test_one_cleanup_failure_does_not_skip_another_work():
    client = Client(fail="first_close")
    result = await run(client)
    assert result["result"] == "cleanup_required"
    assert len(client.owned) == 1 and "private_session_2" in client.owned
    assert len([x for x in client.calls if x[0] == "browser_close"]) == 2
    assert "must-not-leak-secret" not in json.dumps(result)
