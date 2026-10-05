import importlib.util
from pathlib import Path
from types import SimpleNamespace

import psutil

spec = importlib.util.spec_from_file_location(
    "worker_idle_benchmark", Path(__file__).parents[1] / "scripts" / "worker_idle_benchmark.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_cleared_worker_handle_does_not_hide_surviving_process():
    survivor = SimpleNamespace(
        is_running=lambda: True,
        memory_full_info=lambda: SimpleNamespace(rss=100, uss=40),
    )
    result = benchmark.sample(SimpleNamespace(process=None), {(1, 1): survivor})
    assert result["process_count"] == 1
    assert result["uss_bytes"] == 40
    assert result["pss_bytes"] is None


def test_unreadable_process_is_unknown_not_zero():
    def denied():
        raise psutil.AccessDenied()

    unknown = SimpleNamespace(is_running=lambda: True, memory_full_info=denied)
    result = benchmark.sample(SimpleNamespace(process=None), {(1, 1): unknown})
    assert result["inspection_errors"] == 1
    assert result["uss_bytes"] is None
    assert result["rss_bytes"] is None


def test_exited_process_is_not_counted():
    result = benchmark.sample(
        SimpleNamespace(process=None), {(1, 1): SimpleNamespace(is_running=lambda: False)}
    )
    assert result["process_count"] == 0
    assert result["inspection_errors"] == 0
    assert result["uss_bytes"] == 0
