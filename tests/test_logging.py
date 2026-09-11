"""文件日志：LOG_FILE 轮转 handler 挂载、{hostname} 占位、不可写降级。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import pytest

from alert_executor.__main__ import _setup_logging

FMT_PROBE = "probe-log-test"


@pytest.fixture(autouse=True)
def _clean_root_handlers():
    """隔离 basicConfig 对 root logger 的全局副作用。"""
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    yield
    root.handlers[:] = saved
    root.level = saved_level


def _file_handlers():
    return [h for h in logging.getLogger().handlers
            if isinstance(h, RotatingFileHandler)]


def test_no_log_file_means_stdout_only(monkeypatch):
    monkeypatch.delenv("LOG_FILE", raising=False)
    _setup_logging(verbose=False)
    assert _file_handlers() == []
    assert logging.getLogger().handlers  # stdout handler 恒有


def test_log_file_created_with_hostname_placeholder(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "executor-{hostname}.log"))
    monkeypatch.setenv("HOSTNAME", "pod-abc")
    _setup_logging(verbose=False)
    files = _file_handlers()
    assert len(files) == 1
    logging.getLogger("probe").warning(FMT_PROBE)
    for h in files:
        h.flush()
    assert (tmp_path / "executor-pod-abc.log").exists()


def test_unwritable_log_file_degrades(monkeypatch, tmp_path):
    # 父目录不存在且无法创建（路径含文件）→ OSError → 仅 stdout，不抛
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LOG_FILE", str(blocker / "sub" / "executor.log"))
    _setup_logging(verbose=False)  # 不应抛出
    assert _file_handlers() == []
