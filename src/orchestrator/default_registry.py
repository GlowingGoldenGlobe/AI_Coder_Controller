from __future__ import annotations

from .registry import Registry
from .modules import (
    BestTemplateMatchModule,
    ClickMatchModule,
    CounterCapture,
    DoublerAnalyze,
    PrintAct,
    ScreenRecordModule,
    ScreenshotCaptureModule,
    TemplateMatchModule,
    VerifyAfterClickModule,
)


def build_default_registry() -> Registry:
    """Default registry containing safe demo modules + a few real wrappers."""

    reg = Registry()

    # Demo modules (cross-platform, safe)
    reg.register("capture_counter", lambda: CounterCapture())
    reg.register("analyze_double", lambda: DoublerAnalyze())
    reg.register("act_print", lambda: PrintAct())

    # Real module wrappers (Windows-first)
    reg.register("capture_record", lambda: ScreenRecordModule())

    # Vision -> action modules (dry-run safe; actions gated by controls state)
    reg.register("capture_screenshot", lambda: ScreenshotCaptureModule())
    reg.register("match_template", lambda: TemplateMatchModule())
    reg.register("match_best_template", lambda: BestTemplateMatchModule())
    reg.register("act_click", lambda: ClickMatchModule())
    reg.register("verify_after_click", lambda: VerifyAfterClickModule())

    return reg
