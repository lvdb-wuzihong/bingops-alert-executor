"""凭据解引用：NO_AUTH 语义 + env fail fast（红线 4）。"""

from __future__ import annotations

import pytest

from alert_executor.credentials import NO_AUTH, resolve_credential


def test_no_auth_returns_none(monkeypatch):
    monkeypatch.delenv("CH_PW", raising=False)
    assert resolve_credential(NO_AUTH) is None


def test_env_ref_resolves(monkeypatch):
    monkeypatch.setenv("CH_PW", "s3cret")
    assert resolve_credential("CH_PW") == "s3cret"


def test_missing_env_fails_fast(monkeypatch):
    monkeypatch.delenv("NOT_SET_REF", raising=False)
    with pytest.raises(RuntimeError, match="NOT_SET_REF"):
        resolve_credential("NOT_SET_REF")
