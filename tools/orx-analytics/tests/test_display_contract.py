"""Display-layer contract: status mapping, identity keys, data health.

Design docs/menubar-app-design.md §2/§4 — raw ORX values are preserved
alongside a display label; unrecognized values display 未知状态 and are
never guessed into a terminal state. Identity keys are composite: short
numbers (T001/R001) are never global keys.
"""

from pathlib import Path

import pytest

from orxanalytics import display


RUN_VALUES = ["planning", "running", "blocked", "done"]
TASK_VALUES = [
    "pending", "runnable", "running", "waiting_host", "waiting_external",
    "verifying", "passed", "failed", "blocked", "cancelled",
]
GOAL_VALUES = ["active", "paused", "cancelling", "done", "cancelled"]
TERMINAL_DISPLAY = {"完成", "失败", "已取消"}


class TestStatusMapping:
    @pytest.mark.parametrize("raw", RUN_VALUES)
    def test_run_known_value_mapped(self, raw):
        m = display.run_status(raw)
        assert m["raw"] == raw
        assert m["known"] is True
        assert m["display"] != display.UNKNOWN_LABEL
        assert m["display"]

    @pytest.mark.parametrize("raw", TASK_VALUES)
    def test_task_known_value_mapped(self, raw):
        m = display.task_status(raw)
        assert m["raw"] == raw
        assert m["known"] is True
        assert m["display"] != display.UNKNOWN_LABEL

    @pytest.mark.parametrize("raw", GOAL_VALUES)
    def test_goal_known_value_mapped(self, raw):
        m = display.goal_status(raw)
        assert m["raw"] == raw
        assert m["known"] is True

    def test_goal_lifecycle_states_not_terminal(self):
        # G010 v13/v14: paused/cancelling are UNFINISHED goal states (they
        # occupy the one unfinished-Goal slot); only cancelled is terminal
        for raw in ("paused", "cancelling"):
            m = display.goal_status(raw)
            assert m["known"] is True
            assert not display.is_terminal(m)
        assert display.is_terminal(display.goal_status("cancelled"))

    def test_run_design_labels_present(self):
        # the design's §4 vocabulary must exist for known facts; G011 T002:
        # run-level blocked is 受阻 semantics — 暂停 belongs to the GOAL
        # control layer only (两个维度不共用词汇)
        labels = {display.run_status(v)["display"] for v in RUN_VALUES}
        assert {"规划中", "执行中", "受阻", "完成"} <= labels
        assert "暂停" not in labels

    def test_blocked_and_paused_are_different_dimensions(self):
        # G011 matrix §1: Run blocked = 受阻 (execution obstructed);
        # Goal paused = 已暂停 (control-level pause). Never one word for both.
        assert display.run_status("blocked")["display"] == "受阻"
        assert display.task_status("blocked")["display"] == "受阻"
        assert display.goal_status("paused")["display"] == "已暂停"
        run_labels = {display.run_status(v)["display"] for v in RUN_VALUES}
        goal_labels = {display.goal_status(v)["display"] for v in GOAL_VALUES}
        assert "已暂停" not in run_labels
        assert "受阻" not in goal_labels

    def test_task_design_labels_present(self):
        labels = {display.task_status(v)["display"] for v in TASK_VALUES}
        assert {"执行中", "等待验证", "完成", "失败"} <= labels

    @pytest.mark.parametrize("raw", ["", "hibernating", "DONE", "done ",
                                     "partially-done", "zombie"])
    def test_unknown_value_shows_unknown(self, raw):
        m = display.task_status(raw)
        assert m["display"] == display.UNKNOWN_LABEL
        assert m["known"] is False
        assert m["raw"] == raw
        # never guessed into a terminal state
        assert m["display"] not in TERMINAL_DISPLAY

    def test_unknown_run_value_shows_unknown(self):
        m = display.run_status("paused-forever")
        assert m["display"] == display.UNKNOWN_LABEL
        assert m["known"] is False

    def test_none_is_unknown_not_crash(self):
        for fn in (display.run_status, display.task_status,
                   display.goal_status):
            m = fn(None)
            assert m["display"] == display.UNKNOWN_LABEL
            assert m["known"] is False
            assert m["raw"] is None

    def test_terminal_classification(self):
        assert display.is_terminal(display.task_status("passed")) is True
        assert display.is_terminal(display.task_status("failed")) is True
        assert display.is_terminal(display.task_status("cancelled")) is True
        assert display.is_terminal(display.run_status("done")) is True
        assert display.is_terminal(display.task_status("running")) is False
        assert display.is_terminal(display.task_status("pending")) is False
        assert display.is_terminal(
            display.task_status("something-new")) is False

    def test_pending_is_not_an_attention_state(self):
        # §4: waiting on dependencies is not an anomaly — the label must be
        # informational, and pending is non-terminal
        m = display.task_status("pending")
        assert m["display"] == "等待前置"
        assert display.is_terminal(m) is False


class TestIdentityKeys:
    def test_same_run_id_different_projects_distinct(self):
        a = display.run_identity("/proj/a", "R001")
        b = display.run_identity("/proj/b", "R001")
        assert a != b

    def test_same_task_number_different_revision_distinct(self):
        a = display.task_identity("/proj/a", "R001", 1, "T001")
        b = display.task_identity("/proj/a", "R001", 2, "T001")
        assert a != b

    def test_same_task_number_different_run_distinct(self):
        a = display.task_identity("/proj/a", "R001", 1, "T001")
        b = display.task_identity("/proj/a", "R002", 1, "T001")
        assert a != b

    def test_attempt_identity_is_project_scoped(self):
        # attempt row ids come from per-project state.db files: the same
        # integer in two projects is two different attempts
        assert (display.attempt_identity("/proj/a", 7)
                != display.attempt_identity("/proj/b", 7))

    def test_short_numbers_are_not_global_keys(self):
        # T001/R001 alone must never be a key: the composite always carries
        # project (and revision for tasks)
        k = display.task_identity("/proj/a", "R001", 3, "T001")
        assert "/proj/a" in k and "R001" in k and "T001" in k and 3 in k

    def test_project_key_expands_user_like_registry(self):
        # registry.add_project resolves Path(root).expanduser().resolve();
        # the display identity must use the very same transformation
        assert (display.project_key("~/Sources")
                == str(Path("~/Sources").expanduser().resolve()))

    def test_project_key_resolves_symlinks_like_registry(self, tmp_path):
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        assert display.project_key(str(link)) == str(target.resolve())


class TestDataHealth:
    def test_ok_when_fresh_and_readable(self):
        h = display.data_health(
            source_readable=True, schema_supported=True,
            stale=False)
        assert h["health"] == display.HEALTH_OK

    def test_unreadable_source(self):
        h = display.data_health(
            source_readable=False, schema_supported=True, stale=False)
        assert h["health"] == display.HEALTH_UNREADABLE

    def test_unsupported_schema(self):
        h = display.data_health(
            source_readable=True, schema_supported=False, stale=False)
        assert h["health"] == display.HEALTH_UNSUPPORTED

    def test_stale_reports_delayed(self):
        h = display.data_health(
            source_readable=True, schema_supported=True, stale=True)
        assert h["health"] == display.HEALTH_DELAYED

    def test_unreadable_outranks_delayed(self):
        h = display.data_health(
            source_readable=False, schema_supported=False, stale=True)
        assert h["health"] == display.HEALTH_UNREADABLE

    def test_unsupported_outranks_delayed(self):
        h = display.data_health(
            source_readable=True, schema_supported=False, stale=True)
        assert h["health"] == display.HEALTH_UNSUPPORTED

    def test_unknown_facts_report_unknown_not_ok(self):
        h = display.data_health(
            source_readable=None, schema_supported=None, stale=None)
        assert h["health"] == display.HEALTH_UNKNOWN

    def test_health_is_not_an_execution_status(self):
        # the three dimensions stay separate by construction: a health
        # value is never a status label and vice versa
        statuses = {display.run_status(v)["display"] for v in RUN_VALUES}
        statuses |= {display.task_status(v)["display"] for v in TASK_VALUES}
        healths = {display.HEALTH_OK, display.HEALTH_DELAYED,
                   display.HEALTH_UNREADABLE, display.HEALTH_UNSUPPORTED,
                   display.HEALTH_UNKNOWN}
        assert statuses & healths == set()


class TestLifecycleView:
    """G011 T002 lifecycle matrix (docs/diagnostics/g011/lifecycle-matrix.md):
    the Goal control layer and the Run execution layer combine into one
    overview bucket + attention eligibility. `lifecycle_view` is the ONE
    classification point — it never rewrites either raw status."""

    def view(self, goal_raw, run_raw):
        goal = display.goal_status(goal_raw) if goal_raw is not None else None
        return display.lifecycle_view(goal, display.run_status(run_raw))

    def test_matrix_buckets(self):
        # (goal control, run facts) -> bucket; H == historical (query-only)
        M = {
            ("active", "planning"): "active",
            ("active", "running"): "active",
            ("active", "blocked"): "active",
            ("active", "done"): "recent_completed",
            ("paused", "running"): "active",
            ("paused", "done"): "recent_completed",
            ("cancelling", "running"): "active",
            ("cancelling", "blocked"): "active",
            ("cancelling", "done"): "historical",
            ("cancelled", "running"): "historical",
            ("cancelled", "blocked"): "historical",
            ("cancelled", "done"): "historical",
            ("cancelled", "planning"): "historical",
            ("done", "done"): "recent_completed",
            ("done", "running"): "active",
        }
        for (goal_raw, run_raw), bucket in M.items():
            v = self.view(goal_raw, run_raw)
            assert v["bucket"] == bucket, (goal_raw, run_raw, v)

    def test_cancelled_goal_never_earns_attention(self):
        # 已取消不进入失败关注：every execution fact under a KNOWN
        # cancelled goal is history — not a current anomaly
        for run_raw in ("blocked", "running", "done", "weird"):
            assert self.view("cancelled", run_raw)["attention"] is False

    def test_unfinished_control_states_keep_attention(self):
        # paused / cancelling / done / active control states do not change
        # the attention caliber: a blocked run or failed task is still a
        # current execution fact (matrix §3)
        for goal_raw in ("active", "paused", "cancelling", "done"):
            for run_raw in ("blocked", "running", "done"):
                assert self.view(goal_raw, run_raw)["attention"] is True

    def test_paused_and_cancelling_are_unfinished_control_states(self):
        for goal_raw in ("paused", "cancelling"):
            v = self.view(goal_raw, "running")
            assert v["bucket"] == "active"
            assert v["goal_control"] == goal_raw

    def test_run_done_during_pause_is_not_goal_done(self):
        # 暂停期间 Run done：execution fact completed, control layer stays
        # paused — the view keeps both layers apart
        v = self.view("paused", "done")
        assert v["bucket"] == "recent_completed"
        assert v["goal_control"] == "paused"

    def test_cancelling_keeps_unclosed_execution(self):
        # 取消中保留尚未关闭执行的事实：a still-running run stays in the
        # active bucket with the cancelling control state
        v = self.view("cancelling", "running")
        assert v["bucket"] == "active"
        assert v["goal_control"] == "cancelling"

    def test_unknown_never_guessed_terminal(self):
        # unknown control or unknown execution values fall back to the
        # execution facts and are never terminal (matrix §5)
        for run_raw in ("running", "weird", ""):
            v = self.view("weird-goal", run_raw)
            assert v["bucket"] == "active"
            assert v["goal_control"] == "unknown"
            assert v["attention"] is True
        v = self.view("active", "weird")
        assert v["bucket"] == "active"
        assert v["goal_control"] == "active"

    def test_missing_goal_row_falls_back_to_execution_facts(self):
        v = display.lifecycle_view(None, display.run_status("blocked"))
        assert v["bucket"] == "active"
        assert v["attention"] is True
        assert v["goal_control"] is None
        done = display.lifecycle_view(None, display.run_status("done"))
        assert done["bucket"] == "recent_completed"

    def test_view_never_rewrites_raw_statuses(self):
        goal = display.goal_status("paused")
        run = display.run_status("running")
        before = (goal["raw"], run["raw"])
        display.lifecycle_view(goal, run)
        assert (goal["raw"], run["raw"]) == before
