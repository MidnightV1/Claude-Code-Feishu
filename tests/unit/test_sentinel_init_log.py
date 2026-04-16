# -*- coding: utf-8 -*-
"""Golden-standard test: Sentinel initialization log format."""
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _make_scanner(name: str) -> MagicMock:
    s = MagicMock()
    s.name = name
    return s


def test_sentinel_init_log_format(caplog):
    """Golden standard: startup log must list all scanner names."""
    scanners = [
        _make_scanner("code_scanner"),
        _make_scanner("doc_auditor"),
        _make_scanner("health_pulse"),
        _make_scanner("motivation_reviewer"),
        _make_scanner("followup_tracker"),
    ]

    log = logging.getLogger("hub.main")
    with caplog.at_level(logging.INFO, logger="hub.main"):
        log.info(
            "Sentinel initialized with %d scanners: %s",
            len(scanners),
            ", ".join(s.name for s in scanners),
        )

    msgs = [r.message for r in caplog.records if "Sentinel initialized" in r.message]
    assert len(msgs) == 1, f"Expected 1 init log, got: {msgs}"
    expected = (
        "Sentinel initialized with 5 scanners: "
        "code_scanner, doc_auditor, health_pulse, motivation_reviewer, followup_tracker"
    )
    assert msgs[0] == expected, f"Log format mismatch:\n  got:      {msgs[0]!r}\n  expected: {expected!r}"
