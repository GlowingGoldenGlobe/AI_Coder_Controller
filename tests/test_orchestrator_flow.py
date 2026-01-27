from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, MutableMapping

import pytest

from src.orchestrator import Module, ModuleError, Registry, RunContext, init_all, run_once, shutdown_all

from src.orchestrator.modules import ScreenRecordModule
from src.orchestrator.modules import ClickMatchModule, ScreenshotCaptureModule, TemplateMatchModule
from src.orchestrator.modules import BestTemplateMatchModule
from src.orchestrator.modules import VerifyAfterClickModule


@dataclass
class A(Module):
    name: str = "a"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        return {"status": "ok", "payload": {"x": 1}, "meta": {}}

    def shutdown(self, ctx: RunContext) -> None:
        return None


@dataclass
class B(Module):
    name: str = "b"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        return {"status": "ok", "payload": {"y": data["x"] + 1}, "meta": {}}

    def shutdown(self, ctx: RunContext) -> None:
        return None


@dataclass
class Boom(Module):
    name: str = "boom"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        raise ModuleError(self.name, code="expected", message="boom")

    def shutdown(self, ctx: RunContext) -> None:
        return None


def test_registry_create_many() -> None:
    reg = Registry()
    reg.register("a", lambda: A())
    reg.register("b", lambda: B())

    mods = reg.create_many(["a", "b"])
    assert [m.name for m in mods] == ["a", "b"]


def test_run_once_dataflow_ok() -> None:
    mods = [A(), B()]
    ctx = RunContext(dry_run=True)

    init_all(mods, ctx)
    try:
        res = run_once(mods, ctx)
        assert res.ok is True
        assert res.data["x"] == 1
        assert res.data["y"] == 2
        assert len(res.module_results) == 2
        assert res.module_results[0]["status"] == "ok"
    finally:
        shutdown_all(mods, ctx)


def test_run_once_stops_on_module_error() -> None:
    mods = [A(), Boom(), B()]
    ctx = RunContext(dry_run=True)

    init_all(mods, ctx)
    try:
        res = run_once(mods, ctx)
        assert res.ok is False
        # B never runs
        assert "y" not in res.data
        assert res.module_results[-1]["status"] == "error"
        assert res.module_results[-1]["meta"]["error"]["code"] == "expected"
    finally:
        shutdown_all(mods, ctx)


def test_invalid_result_shape_raises() -> None:
    @dataclass
    class Bad(Module):
        name: str = "bad"

        def init(self, ctx: RunContext) -> None:
            return None

        def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
            return {"status": "weird"}

        def shutdown(self, ctx: RunContext) -> None:
            return None

    mods = [Bad()]
    ctx = RunContext(dry_run=True)

    init_all(mods, ctx)
    try:
        with pytest.raises(ValueError):
            run_once(mods, ctx)
    finally:
        shutdown_all(mods, ctx)


def test_screen_record_module_dry_run_is_safe() -> None:
    mod = ScreenRecordModule()
    ctx = RunContext(dry_run=True, config={"root": "."})

    init_all([mod], ctx)
    try:
        res = run_once([mod], ctx)
        assert res.ok is True
        assert res.data["capture"]["mode"] == "dry_run"
        assert res.data["capture"]["recording"] is False
    finally:
        shutdown_all([mod], ctx)


def test_vision_action_modules_dry_run_safe() -> None:
    # This test must be safe on machines without OCR/GUI deps.
    ctx = RunContext(
        dry_run=True,
        config={
            "capture_screenshot": {"enabled": True, "allow_in_dry_run": False},
            "match_template": {"enabled": True, "template_path": "assets/ui_templates/auto_template_1.png"},
            "act_click": {"enabled": True},
        },
    )

    mods = [ScreenshotCaptureModule(), TemplateMatchModule(), ClickMatchModule()]
    init_all(mods, ctx)
    try:
        res = run_once(mods, ctx)
        # Capture is disabled in dry-run for this test, so template/click should skip.
        assert res.ok is True
    finally:
        shutdown_all(mods, ctx)


def test_best_template_match_skips_without_screenshot() -> None:
    mod = BestTemplateMatchModule()
    ctx = RunContext(dry_run=True, config={"match_best_template": {"enabled": True}})
    init_all([mod], ctx)
    try:
        res = run_once([mod], ctx)
        assert res.ok is True
        assert res.module_results[0]["status"] == "skip"
        assert res.module_results[0]["meta"]["reason"] == "no_screenshot"
    finally:
        shutdown_all([mod], ctx)


def test_verify_after_click_skips_in_dry_run() -> None:
    mod = VerifyAfterClickModule()
    ctx = RunContext(dry_run=True, config={"verify_after_click": {"enabled": True}})
    init_all([mod], ctx)
    try:
        res = run_once([mod], ctx)
        assert res.ok is True
        assert res.module_results[0]["status"] == "skip"
        assert res.module_results[0]["meta"]["reason"] == "dry_run"
    finally:
        shutdown_all([mod], ctx)
