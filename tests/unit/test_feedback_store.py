"""Unit tests for invest-radar feedback_store.py"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / ".claude/skills/invest-radar/scripts"))

import feedback_store
from feedback_store import ContrarianLog, DirectiveStore, FactorHistory


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path):
    """Patch each class's _FILE to use a temp directory."""
    orig_directive_file = DirectiveStore._FILE
    orig_factor_file = FactorHistory._FILE
    orig_contrarian_file = ContrarianLog._FILE

    DirectiveStore._FILE = tmp_path / "directives.json"
    FactorHistory._FILE = tmp_path / "factor_history.json"
    ContrarianLog._FILE = tmp_path / "contrarian_log.json"

    yield tmp_path

    DirectiveStore._FILE = orig_directive_file
    FactorHistory._FILE = orig_factor_file
    ContrarianLog._FILE = orig_contrarian_file


# ---------------------------------------------------------------------------
# DirectiveStore
# ---------------------------------------------------------------------------

class TestDirectiveStoreAdd:
    def test_creates_directive_with_correct_fields(self):
        store = DirectiveStore()
        d = store.add(type="skepticism", content="Be skeptical of momentum signals", source="weekly_review")

        assert d["type"] == "skepticism"
        assert d["content"] == "Be skeptical of momentum signals"
        assert d["source"] == "weekly_review"
        assert d["status"] == "active"
        assert d["auto_generated"] is False
        assert d["validation"] is None
        assert "id" in d
        assert "created_at" in d
        assert "expires_at" in d

    def test_auto_generates_id(self):
        store = DirectiveStore()
        today = datetime.now().strftime("%Y%m%d")
        d = store.add(type="foo", content="bar", source="test")
        assert d["id"].startswith(f"dir_{today}_")

    def test_sequential_ids_same_day(self):
        store = DirectiveStore()
        d1 = store.add(type="t1", content="c1", source="s")
        d2 = store.add(type="t2", content="c2", source="s")
        assert d1["id"].endswith("_001")
        assert d2["id"].endswith("_002")

    def test_expiry_calculated_from_expires_weeks(self):
        store = DirectiveStore()
        d = store.add(type="t", content="c", source="s", expires_weeks=2)
        expected = (datetime.now() + timedelta(weeks=2)).strftime("%Y-%m-%d")
        assert d["expires_at"] == expected

    def test_auto_generated_flag(self):
        store = DirectiveStore()
        d = store.add(type="t", content="c", source="s", auto_generated=True)
        assert d["auto_generated"] is True

    def test_max_active_evicts_oldest_on_sixth_add(self):
        store = DirectiveStore()
        added = []
        for i in range(DirectiveStore.MAX_ACTIVE):
            d = store.add(type="t", content=f"content_{i}", source="s")
            added.append(d["id"])

        assert len(store.get_active()) == DirectiveStore.MAX_ACTIVE

        # Adding a 6th should evict the oldest
        store.add(type="t", content="content_new", source="s")
        active = store.get_active()
        assert len(active) == DirectiveStore.MAX_ACTIVE

        active_ids = {d["id"] for d in active}
        assert added[0] not in active_ids  # oldest evicted

    def test_evicted_directive_is_expired_not_deleted(self):
        store = DirectiveStore()
        added = []
        for i in range(DirectiveStore.MAX_ACTIVE):
            d = store.add(type="t", content=f"c{i}", source="s")
            added.append(d["id"])

        store.add(type="t", content="new", source="s")

        all_directives = store.list_all()
        evicted = next(d for d in all_directives if d["id"] == added[0])
        assert evicted["status"] == "expired"


class TestDirectiveStoreGetActive:
    def test_returns_only_active_directives(self):
        store = DirectiveStore()
        d1 = store.add(type="t", content="active one", source="s")
        d2 = store.add(type="t", content="active two", source="s")
        store.expire(d1["id"])

        active = store.get_active()
        ids = {d["id"] for d in active}
        assert d1["id"] not in ids
        assert d2["id"] in ids

    def test_auto_expires_past_due_directives(self):
        store = DirectiveStore()
        d = store.add(type="t", content="will expire", source="s", expires_weeks=1)

        # Manually backdate expires_at to yesterday
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        for directive in store._directives:
            if directive["id"] == d["id"]:
                directive["expires_at"] = yesterday
        store._save()

        # Reload fresh instance to confirm persistence
        store2 = DirectiveStore()
        active = store2.get_active()
        ids = {x["id"] for x in active}
        assert d["id"] not in ids

        # Confirm the directive is now marked expired
        all_d = store2.list_all()
        expired = next(x for x in all_d if x["id"] == d["id"])
        assert expired["status"] == "expired"

    def test_non_expired_directives_not_touched(self):
        store = DirectiveStore()
        d = store.add(type="t", content="stays active", source="s", expires_weeks=4)
        active = store.get_active()
        assert any(x["id"] == d["id"] for x in active)


class TestDirectiveStoreExpire:
    def test_manually_expire_directive(self):
        store = DirectiveStore()
        d = store.add(type="t", content="c", source="s")
        result = store.expire(d["id"])
        assert result is True

        all_d = store.list_all()
        found = next(x for x in all_d if x["id"] == d["id"])
        assert found["status"] == "expired"

    def test_expire_nonexistent_returns_false(self):
        store = DirectiveStore()
        assert store.expire("dir_99999999_999") is False


class TestDirectiveStoreValidate:
    def test_validate_marks_directive_with_notes(self):
        store = DirectiveStore()
        d = store.add(type="t", content="c", source="s")
        result = store.validate(d["id"], success=True, notes="worked well")
        assert result is True

        all_d = store.list_all()
        found = next(x for x in all_d if x["id"] == d["id"])
        assert found["status"] == "validated"
        assert found["validation"]["success"] is True
        assert found["validation"]["notes"] == "worked well"
        assert "validated_at" in found["validation"]

    def test_validate_failure(self):
        store = DirectiveStore()
        d = store.add(type="t", content="c", source="s")
        store.validate(d["id"], success=False, notes="did not work")

        all_d = store.list_all()
        found = next(x for x in all_d if x["id"] == d["id"])
        assert found["status"] == "validated"
        assert found["validation"]["success"] is False

    def test_validate_nonexistent_returns_false(self):
        store = DirectiveStore()
        assert store.validate("dir_00000000_999", success=True) is False


class TestDirectiveStoreListAll:
    def test_list_all_no_filter(self):
        store = DirectiveStore()
        store.add(type="t", content="c1", source="s")
        d2 = store.add(type="t", content="c2", source="s")
        store.expire(d2["id"])

        all_d = store.list_all()
        assert len(all_d) == 2

    def test_list_all_filter_by_status_active(self):
        store = DirectiveStore()
        store.add(type="t", content="c1", source="s")
        d2 = store.add(type="t", content="c2", source="s")
        store.expire(d2["id"])

        active = store.list_all(status="active")
        assert len(active) == 1
        assert all(d["status"] == "active" for d in active)

    def test_list_all_filter_by_status_expired(self):
        store = DirectiveStore()
        d1 = store.add(type="t", content="c1", source="s")
        store.add(type="t", content="c2", source="s")
        store.expire(d1["id"])

        expired = store.list_all(status="expired")
        assert len(expired) == 1
        assert expired[0]["id"] == d1["id"]

    def test_list_all_empty_store(self):
        store = DirectiveStore()
        assert store.list_all() == []


# ---------------------------------------------------------------------------
# FactorHistory
# ---------------------------------------------------------------------------

class TestFactorHistorySaveWeek:
    def test_saves_and_retrieves_correctly(self):
        fh = FactorHistory()
        factors = {"momentum": {"effectiveness": 0.7}, "value": {"effectiveness": 0.5}}
        fh.save_week(week="2025-W01", date="2025-01-06", factors=factors)

        history = fh.get_history()
        assert len(history) == 1
        assert history[0]["week"] == "2025-W01"
        assert history[0]["date"] == "2025-01-06"
        assert history[0]["factors"]["momentum"]["effectiveness"] == 0.7

    def test_upsert_overwrites_existing_week(self):
        fh = FactorHistory()
        fh.save_week(week="2025-W01", date="2025-01-06", factors={"f": {"effectiveness": 0.5}})
        fh.save_week(week="2025-W01", date="2025-01-06", factors={"f": {"effectiveness": 0.9}})

        history = fh.get_history()
        assert len(history) == 1
        assert history[0]["factors"]["f"]["effectiveness"] == 0.9

    def test_keeps_max_52_weeks(self):
        fh = FactorHistory()
        for i in range(60):
            week = f"2024-W{i+1:02d}"
            fh.save_week(week=week, date="2024-01-01", factors={"f": {"effectiveness": i * 0.01}})

        history = fh.get_history(weeks=100)
        assert len(history) == FactorHistory.MAX_WEEKS

    def test_oldest_entries_dropped_when_over_limit(self):
        fh = FactorHistory()
        for i in range(54):
            week = f"2024-W{i+1:02d}"
            fh.save_week(week=week, date="2024-01-01", factors={"f": {"effectiveness": 0.1}})

        history = fh.get_history(weeks=100)
        weeks_present = {e["week"] for e in history}
        # Oldest two should have been trimmed
        assert "2024-W01" not in weeks_present
        assert "2024-W02" not in weeks_present


class TestFactorHistoryGetHistory:
    def test_returns_last_n_weeks(self):
        fh = FactorHistory()
        for i in range(10):
            fh.save_week(week=f"2025-W{i+1:02d}", date="2025-01-01", factors={"f": {"effectiveness": float(i)}})

        history = fh.get_history(weeks=3)
        assert len(history) == 3

    def test_returns_sorted_ascending(self):
        fh = FactorHistory()
        fh.save_week(week="2025-W03", date="2025-01-20", factors={})
        fh.save_week(week="2025-W01", date="2025-01-06", factors={})
        fh.save_week(week="2025-W02", date="2025-01-13", factors={})

        history = fh.get_history()
        weeks = [e["week"] for e in history]
        assert weeks == sorted(weeks)

    def test_returns_empty_for_empty_store(self):
        fh = FactorHistory()
        assert fh.get_history() == []


class TestFactorHistoryDetectTrend:
    def test_detects_declining_trend(self):
        fh = FactorHistory()
        # 4 weeks of strictly declining effectiveness
        for i, val in enumerate([0.8, 0.6, 0.4, 0.2]):
            fh.save_week(
                week=f"2025-W{i+1:02d}",
                date="2025-01-01",
                factors={"momentum": {"effectiveness": val}},
            )

        trend = fh.detect_trend("momentum", window=4)
        assert trend is not None
        assert trend["direction"] == "declining"
        assert trend["factor"] == "momentum"
        assert trend["values"] == [0.8, 0.6, 0.4, 0.2]

    def test_detects_improving_trend(self):
        fh = FactorHistory()
        for i, val in enumerate([0.2, 0.4, 0.6, 0.8]):
            fh.save_week(
                week=f"2025-W{i+1:02d}",
                date="2025-01-01",
                factors={"momentum": {"effectiveness": val}},
            )

        trend = fh.detect_trend("momentum", window=4)
        assert trend is not None
        assert trend["direction"] == "improving"

    def test_returns_none_when_no_clear_trend(self):
        fh = FactorHistory()
        # Non-monotonic values
        for i, val in enumerate([0.8, 0.3, 0.7, 0.5]):
            fh.save_week(
                week=f"2025-W{i+1:02d}",
                date="2025-01-01",
                factors={"momentum": {"effectiveness": val}},
            )

        trend = fh.detect_trend("momentum", window=4)
        assert trend is None

    def test_returns_none_when_insufficient_data(self):
        fh = FactorHistory()
        fh.save_week(week="2025-W01", date="2025-01-06", factors={"momentum": {"effectiveness": 0.9}})
        fh.save_week(week="2025-W02", date="2025-01-13", factors={"momentum": {"effectiveness": 0.7}})

        # window=4, only 2 entries
        trend = fh.detect_trend("momentum", window=4)
        assert trend is None

    def test_returns_none_when_factor_missing(self):
        fh = FactorHistory()
        for i in range(4):
            fh.save_week(
                week=f"2025-W{i+1:02d}",
                date="2025-01-01",
                factors={"other_factor": {"effectiveness": float(i)}},
            )

        trend = fh.detect_trend("momentum", window=4)
        assert trend is None


# ---------------------------------------------------------------------------
# ContrarianLog
# ---------------------------------------------------------------------------

class TestContrarianLogAdd:
    def test_creates_signal_with_correct_fields(self):
        log = ContrarianLog()
        s = log.add(
            signal="Market will correct 20% within 3 months",
            held_by="bear_model",
            confidence="high",
            potential_alpha="significant downside protection",
        )

        assert s["signal"] == "Market will correct 20% within 3 months"
        assert s["held_by"] == "bear_model"
        assert s["confidence"] == "high"
        assert s["potential_alpha"] == "significant downside protection"
        assert s["status"] == "pending"
        assert s["validation"] is None
        assert "id" in s
        assert "created_at" in s
        assert "validate_after" in s

    def test_auto_generates_id(self):
        log = ContrarianLog()
        today = datetime.now().strftime("%Y%m%d")
        s = log.add(signal="x", held_by="m", confidence="low", potential_alpha="y")
        assert s["id"].startswith(f"ctr_{today}_")

    def test_validate_after_calculated_from_weeks(self):
        log = ContrarianLog()
        s = log.add(signal="x", held_by="m", confidence="low", potential_alpha="y", validate_after_weeks=2)
        expected = (datetime.now() + timedelta(weeks=2)).strftime("%Y-%m-%d")
        assert s["validate_after"] == expected


class TestContrarianLogGetPending:
    def test_returns_signals_ready_for_validation(self):
        log = ContrarianLog()
        s = log.add(signal="x", held_by="m", confidence="low", potential_alpha="y", validate_after_weeks=1)

        # Signal not ready yet (validate_after is 1 week from now)
        today = datetime.now().strftime("%Y-%m-%d")
        pending_today = log.get_pending(as_of=today)
        assert all(p["id"] != s["id"] for p in pending_today)

        # Signal is ready as of 2 weeks from now
        future = (datetime.now() + timedelta(weeks=2)).strftime("%Y-%m-%d")
        pending_future = log.get_pending(as_of=future)
        assert any(p["id"] == s["id"] for p in pending_future)

    def test_does_not_return_validated_signals(self):
        log = ContrarianLog()
        s = log.add(signal="x", held_by="m", confidence="low", potential_alpha="y", validate_after_weeks=0)
        log.validate(s["id"], success=True)

        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        pending = log.get_pending(as_of=past)
        # Should not include validated signal
        assert all(p["id"] != s["id"] for p in pending)


class TestContrarianLogValidate:
    def test_marks_signal_as_validated(self):
        log = ContrarianLog()
        s = log.add(signal="x", held_by="m", confidence="low", potential_alpha="y")
        result = log.validate(s["id"], success=True, notes="signal played out")
        assert result is True

        all_s = log.list_all()
        found = next(x for x in all_s if x["id"] == s["id"])
        assert found["status"] == "validated"
        assert found["validation"]["success"] is True
        assert found["validation"]["notes"] == "signal played out"
        assert "validated_at" in found["validation"]

    def test_marks_signal_as_invalidated(self):
        log = ContrarianLog()
        s = log.add(signal="x", held_by="m", confidence="low", potential_alpha="y")
        log.validate(s["id"], success=False, notes="was wrong")

        all_s = log.list_all()
        found = next(x for x in all_s if x["id"] == s["id"])
        assert found["status"] == "invalidated"
        assert found["validation"]["success"] is False

    def test_validate_nonexistent_returns_false(self):
        log = ContrarianLog()
        assert log.validate("ctr_00000000_999", success=True) is False


class TestContrarianLogGetStats:
    def test_correct_counts_and_rates(self):
        log = ContrarianLog()
        s1 = log.add(signal="a", held_by="model_a", confidence="high", potential_alpha="x")
        s2 = log.add(signal="b", held_by="model_a", confidence="low", potential_alpha="y")
        s3 = log.add(signal="c", held_by="model_b", confidence="med", potential_alpha="z")

        log.validate(s1["id"], success=True)
        log.validate(s2["id"], success=False)
        # s3 remains pending

        stats = log.get_stats()

        assert stats["total"] == 3
        assert stats["validated_count"] == 2
        assert stats["success_rate"] == 0.5

    def test_by_model_breakdown(self):
        log = ContrarianLog()
        s1 = log.add(signal="a", held_by="model_a", confidence="high", potential_alpha="x")
        s2 = log.add(signal="b", held_by="model_a", confidence="high", potential_alpha="x")
        s3 = log.add(signal="c", held_by="model_b", confidence="low", potential_alpha="y")

        log.validate(s1["id"], success=True)
        log.validate(s2["id"], success=True)
        log.validate(s3["id"], success=False)

        stats = log.get_stats()

        assert stats["by_model"]["model_a"]["total"] == 2
        assert stats["by_model"]["model_a"]["success_rate"] == 1.0
        assert stats["by_model"]["model_b"]["total"] == 1
        assert stats["by_model"]["model_b"]["success_rate"] == 0.0

    def test_empty_store_returns_zero_rates(self):
        log = ContrarianLog()
        stats = log.get_stats()
        assert stats["total"] == 0
        assert stats["validated_count"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["by_model"] == {}

    def test_pending_signals_do_not_affect_success_rate(self):
        log = ContrarianLog()
        s1 = log.add(signal="a", held_by="m", confidence="high", potential_alpha="x")
        log.add(signal="b", held_by="m", confidence="high", potential_alpha="x")  # stays pending
        log.validate(s1["id"], success=True)

        stats = log.get_stats()
        # Only s1 is validated, s2 is pending
        assert stats["validated_count"] == 1
        assert stats["success_rate"] == 1.0
