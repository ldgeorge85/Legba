# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-1 — the single alert path, the parts that need no database.

* the closed list of kinds: every kind has a topic and a subject kind; every
  kind a producer names (trigger classes, watchdog channels, the escalation
  edge, hard fails, forecasts, the host scripts) is in it; a kind outside it is
  refused by ``raise_alert`` before anything is written, with a clear error;
* the page rule: the shipped file loads, refuses a typo, matches by kind,
  severity and subject; ``volume_surge`` is not paged;
* the decision: new episode, update, severity rise, critical re-page, recovery,
  held;
* THE GUARD: no module outside ``raise_alert`` hands an alert to the delivery
  plane (``.fan_out(``), no module outside the alert package calls a sink's
  ``deliver``, and every host script that pushes to ntfy raises through
  ``scripts/lib/legba_alert.sh`` first (the direct push is only the fallback);
* the registry route imports in the slim image.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from legba.data.alerts import kinds as K
from legba.data.alerts import page_rule as PR
from legba.data.alerts import topics as T
from legba.data.alerts.raise_alert import decide, raise_alert

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "legba"
NOW = datetime(2026, 11, 13, 22, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The closed list
# ---------------------------------------------------------------------------


def test_every_kind_has_a_topic_a_subject_kind_and_a_lifecycle() -> None:
    for name, kind in K.KINDS.items():
        assert name in T.TOPIC_BY_KIND, name
        assert kind.subject_kind in K.SUBJECT_KINDS, name
        assert kind.lifecycle in ("condition", "event"), name
        assert kind.summary and kind.group, name
        if kind.lifecycle == "event":
            assert kind.quiet_close_hours, f"{name}: an event kind must close itself"


def test_every_kind_a_producer_names_is_in_the_closed_list() -> None:
    from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
    from legba.data.analysts.deterministic_handlers import forecast_resolver
    from legba.runtime import hard_fail_alert, liveness_watchdog as lw, volume_surge_watch

    named = (
        set(ats.TRIGGER_CLASSES) | {"rollup"} | set(lw._TRANSITION_CHANNELS)
        | {lw.ALERT_CHANNEL_GLOBAL, "escalations", forecast_resolver.ALERT_CHANNEL,
           hard_fail_alert.ALERT_CHANNEL_ANALYST_HARD_FAIL, volume_surge_watch.KIND,
           "reminder_orphan_collected", "analyst_alert_output"}
    )
    # the literal kinds every raise_alert call / host script passes
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "raise_alert(" not in text:
            continue
        named |= set(re.findall(r'kind="([a-z_]+)"', text)) & set(
            re.findall(r'raise_alert\([^)]*?kind="([a-z_]+)"', text, flags=re.S)
        )
    for script in (REPO / "scripts").glob("*.sh"):
        named |= set(re.findall(r"legba_raise\s+([a-z_]+)\s", script.read_text()))
    named.discard("kind")
    missing = sorted(n for n in named if n not in K.KINDS)
    assert not missing, f"producers name kinds outside the closed list: {missing}"


def test_unknown_kind_is_refused_with_a_clear_error() -> None:
    with pytest.raises(K.UnknownAlertKind) as err:
        K.require_kind("disk_full")
    assert "closed list" in str(err.value) and "host_disk" in str(err.value)


class _RecordingPool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetchrow(self, sql, *a):  # noqa: ANN001
        self.calls.append(sql)
        raise AssertionError("no write may happen for a refused kind")

    async def execute(self, sql, *a):  # noqa: ANN001
        self.calls.append(sql)
        raise AssertionError("no write may happen for a refused kind")


@pytest.mark.asyncio
async def test_raise_alert_refuses_an_unknown_kind_before_any_write() -> None:
    pool = _RecordingPool()
    with pytest.raises(K.UnknownAlertKind):
        await raise_alert(pool, kind="pipeline_stall", severity="high",
                          summary="x", subject_id="y", raised_by="test")
    assert pool.calls == []


# ---------------------------------------------------------------------------
# The page rule
# ---------------------------------------------------------------------------


def test_the_shipped_rule_loads_and_names_only_real_kinds() -> None:
    rule = PR.load_rule()
    assert rule.version and rule.entries
    assert {e.kind for e in rule.entries} <= set(K.KINDS)
    # 2026-11-14 (the owner, "surge then"): a surge pages at medium; a baseline
    # deviation is logged only, at any severity.
    assert rule.match("volume_surge", "medium", "US") is not None
    assert rule.match("volume_surge", "low", "US") is None
    for sev in ("info", "low", "medium", "high", "critical"):
        assert rule.match("baseline_deviation", sev, "MX") is None
    assert rule.match("escalations", "medium", "UA") is not None
    assert rule.match("escalations", "low", "UA") is None
    assert rule.match("source_degraded", "medium", "source.x") is None  # 5xx stalls
    assert rule.match("source_degraded", "high", "source.x") is not None
    assert rule.match("nightly_suite", "low", "nightly") is not None
    assert rule.match("reminder_orphan_collected", "critical", "x") is None


@pytest.mark.parametrize("doc,needle", [
    ({"page": [{"kind": "disk_full", "min_severity": "high"}]}, "closed list"),
    ({"page": [{"kind": "host_disk", "min_severity": "urgent"}]}, "ladder"),
    ({"page": [{"kind": "host_disk", "min_severity": "high", "escalate": 1}]}, "unknown keys"),
    ({"pages": []}, "'page' must be a list"),
])
def test_a_rule_typo_is_refused_at_load(doc, needle) -> None:  # noqa: ANN001
    with pytest.raises(PR.PageRuleError) as err:
        PR.parse_rule(doc)
    assert needle in str(err.value)


def test_subject_filter_matches_ids_and_patterns() -> None:
    rule = PR.parse_rule({"page": [
        {"kind": "volume_surge", "min_severity": "medium", "subject": "US"},
        {"kind": "source_degraded", "min_severity": "high", "subject": "source.reuters.*"},
    ]})
    assert rule.match("volume_surge", "medium", "US").describe() == "volume_surge >= medium (subject US)"
    assert rule.match("volume_surge", "high", "FR") is None
    assert rule.match("source_degraded", "high", "source.reuters.world") is not None
    assert rule.match("source_degraded", "high", "source.bbc.world") is None


def test_the_env_path_overrides_the_shipped_rule(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    p = tmp_path / "rule.yaml"
    p.write_text("version: test\npage:\n  - {kind: volume_surge, min_severity: medium}\n")
    monkeypatch.setenv(PR.ENV_PAGE_RULE_PATH, str(p))
    PR.reset_cache()
    try:
        rule = PR.current_rule()
        assert rule.version == "test" and rule.match("volume_surge", "medium", "US")
    finally:
        monkeypatch.delenv(PR.ENV_PAGE_RULE_PATH)
        PR.reset_cache()


# ---------------------------------------------------------------------------
# The decision (pure)
# ---------------------------------------------------------------------------


def _decide(action, severity="high", prior=None, paged_at=None, hold=None, kind="source_degraded"):  # noqa: ANN001
    return decide(rule=PR.load_rule(), kind=kind, severity=severity, subject_id="s",
                  action=action, hold=hold, prior_paged_severity=prior,
                  prior_last_paged_at=paged_at, now=NOW)


def test_decisions() -> None:
    assert _decide("opened")[0] == "paged"
    assert _decide("opened", severity="medium")[0] == "logged"
    assert _decide("updated", prior="high")[0] == "deduped"
    assert _decide("updated", severity="critical", prior="high")[0] == "paged"
    assert _decide("updated", prior=None)[0] == "paged"  # never paged yet
    assert _decide("updated", severity="critical", prior="critical",
                   paged_at=NOW - timedelta(hours=7))[0] == "paged"
    assert _decide("updated", severity="critical", prior="critical",
                   paged_at=NOW - timedelta(hours=1))[0] == "deduped"
    assert _decide("closed", severity="info", prior="high")[0] == "logged"  # rule: high
    assert _decide("closed", severity="low", prior="medium", kind="host_disk")[0] == "paged"
    assert _decide("closed", severity="low", prior=None, kind="host_disk")[0] == "logged"
    held = _decide("opened", hold="steady-state guard: steady")
    assert held[0] == "held" and "steady-state" in held[1]
    paged = _decide("opened")
    assert paged[2] == "source_degraded >= high"


# ---------------------------------------------------------------------------
# THE GUARD — one function hands alerts to the delivery plane
# ---------------------------------------------------------------------------

#: The only modules allowed to call ``.fan_out(``: the function itself, and
#: the dispatcher's own definition.
_FAN_OUT_ALLOWED = {"data/alerts/raise_alert.py", "data/alerts/sinks.py"}

#: The only modules allowed to call a sink's ``.deliver(``: the dispatcher, and
#: the first-generation ``alert`` output kind's legacy transports (dormant since
#: 2026-07-01; they raise on the alert log first — see docs/ALERTING.md).
_DELIVER_ALLOWED = {"data/alerts/sinks.py", "data/outputs/alert.py"}


def _code_lines(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith(("``", "*", "-")):
            continue
        out.append(line)
    return out


def test_no_direct_fan_out_outside_the_one_function() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in _FAN_OUT_ALLOWED:
            continue
        for n, line in enumerate(_code_lines(path), 1):
            if re.search(r"\.fan_out\(", line) and "def fan_out" not in line:
                offenders.append(f"{rel}: {line.strip()}")
    assert not offenders, (
        "alerts must be raised through legba.data.alerts.raise_alert.raise_alert, "
        f"never handed to the dispatcher directly: {offenders}"
    )


def test_no_direct_sink_deliver_outside_the_dispatcher() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in _DELIVER_ALLOWED or rel.startswith("data/alerts/"):
            continue
        for line in _code_lines(path):
            if re.search(r"\b(sink|_sink|alert_sink)\w*\.deliver\(", line):
                offenders.append(f"{rel}: {line.strip()}")
    assert not offenders, offenders


#: Scripts that run on ANOTHER host (the models host) and cannot reach the
#: runtime container; documented in docs/ALERTING.md "Not yet".
_SCRIPTS_OFF_HOST = {"models_host_vllm_watchdog.sh", "models_host_saturation_watch.sh"}


def test_every_host_script_that_pushes_raises_through_the_alert_path_first() -> None:
    offenders = []
    for script in sorted((REPO / "scripts").glob("*.sh")):
        text = script.read_text(encoding="utf-8")
        pushes = re.search(r"curl[^\n]*(NTFY|ntfy)", text) or re.search(
            r"\"\$(ENGINE_NTFY_URL|NTFY_URL)\"", text)
        if not pushes or script.name in _SCRIPTS_OFF_HOST:
            continue
        if "lib/legba_alert.sh" not in text or "legba_raise " not in text:
            offenders.append(script.name)
    assert not offenders, f"host scripts that push to ntfy without the alert path: {offenders}"


# ---------------------------------------------------------------------------
# Registry-slim
# ---------------------------------------------------------------------------


def test_the_alert_log_route_imports_in_the_slim_image() -> None:
    code = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.alert_log_api as api
        import legba.data.registry.v3_api as v3
        assert callable(api.register_alert_log_routes)
        bad = [m for m in sys.modules
               if m.startswith(("legba.runtime", "legba.data.analysts"))]
        assert not bad, bad
        print("OK")
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        p for p in (str(REPO / "src"), os.environ.get("PYTHONPATH", "")) if p)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=env, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr


# ---------------------------------------------------------------------------
# The CLI's refusal needs no database
# ---------------------------------------------------------------------------


def test_cli_refuses_an_unknown_kind_with_exit_2(capsys) -> None:  # noqa: ANN001
    from legba.data.alerts import cli

    code = cli.main(["raise", "--kind", "disk_full", "--severity", "high",
                     "--subject", "/", "--summary", "x", "--raised-by", "test"])
    assert code == cli.EXIT_REFUSED
    assert "closed list" in capsys.readouterr().out
