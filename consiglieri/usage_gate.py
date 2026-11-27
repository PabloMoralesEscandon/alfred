"""Usage conditions on scheduled items: checked just before each delivery, so a reminder or a task is held
back when the plan limits are nearly used up (`--skip-if-low`, `--skip-if`), or delivered only when a
condition holds (`--only-if`, `--only-if-waste`).

The check reads the usage the way `alfred usage check` does (a reading at most a few minutes old, `/usage`
otherwise, never the model), so a held-back item costs no quota: the bot is not woken at all. The conditions
are the usage-alert language; the verdict, not the numbers, decides. A bot needs the usage permission to put
a condition on an item, and the item is checked as if unread once the bot no longer has it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from . import usage_alerts, usage_forecast

log = logging.getLogger("consiglieri.usage_gate")

RUN = "run"
SKIP = "skip"
UNREAD_POLICIES = (RUN, SKIP)


class GateError(ValueError):
    """A condition, the hours or the policy can't be read."""


@dataclass
class Gate:
    skip_if: str = ""  # hold the delivery back when this holds
    only_if: str = ""  # deliver only when this holds
    hours: str = ""  # active hours for the forecast values (empty: the user's default)
    if_unread: str = ""  # when the usage can't be read: run or skip; empty means run, or skip with only_if
    label: str = ""  # how it was asked, for `schedule list` (`--skip-if-low week`)

    @property
    def unread_policy(self) -> str:
        return self.if_unread or (SKIP if self.only_if else RUN)

    def validate(self) -> "Gate":
        if not (self.skip_if or self.only_if):
            raise GateError("a usage condition needs --skip-if or --only-if (or one of their shortcuts)")
        for name, text in (("skip-if", self.skip_if), ("only-if", self.only_if)):
            if text:
                try:
                    usage_alerts.parse(text)
                except usage_alerts.ConditionError as exc:
                    raise GateError("bad --%s condition %r: %s" % (name, text, exc)) from None
        if self.hours:
            try:
                usage_forecast.parse_hours(self.hours)
            except usage_forecast.HoursError as exc:
                raise GateError(str(exc)) from None
        if self.if_unread and self.if_unread not in UNREAD_POLICIES:
            raise GateError("--if-unread is run or skip")
        return self

    def to_text(self) -> str:
        return json.dumps({key: value for key, value in asdict(self).items() if value}, sort_keys=True)

    @classmethod
    def from_text(cls, text: Optional[str]) -> Optional["Gate"]:
        """The gate stored on an item, or None when it has none. Raises GateError if it can't be read."""
        if not text:
            return None
        try:
            data = json.loads(text)
            return cls(**{key: str(data.get(key) or "") for key in cls.__dataclass_fields__}).validate()
        except (ValueError, TypeError, AttributeError) as exc:
            raise GateError("the item's usage condition can't be read: %s" % exc) from None

    def describe(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {key: value for key, value in asdict(self).items() if value and key != "if_unread"}
        data["if_unread"] = self.unread_policy
        return data


@dataclass
class Decision:
    deliver: bool
    reason: str
    unread: bool = False


def _unread(gate: Optional[Gate], why: str) -> Decision:
    policy = gate.unread_policy if gate is not None else RUN
    if policy == SKIP:
        return Decision(False, "the usage could not be checked (%s), and this item is skipped then" % why, True)
    return Decision(True, "the usage could not be checked (%s); delivered anyway" % why, True)


def evaluate(gate: Gate, evaluation: Any) -> Decision:
    """The decision for one reading (a usage_monitor.Evaluation): pure, reads nothing."""
    hours = gate.hours or None
    if gate.skip_if:
        node = usage_alerts.parse(gate.skip_if)
        result, reason = usage_alerts.verdict(node, evaluation.values(node, hours)[0])
        if result:
            return Decision(False, reason)
    if gate.only_if:
        node = usage_alerts.parse(gate.only_if)
        result, reason = usage_alerts.verdict(node, evaluation.values(node, hours)[0])
        if not result:
            return Decision(False, "only when %s, and now %s" % (gate.only_if, reason or "it does not hold"))
        return Decision(True, reason)
    return Decision(True, "")


def check(config: Any, text: Optional[str], allowed: bool = True, source: Any = None,
          now: Optional[datetime] = None) -> Decision:
    """Whether to deliver an item with this stored gate now. Blocking (it may run `/usage` and waits for the
    other processes reading it): the service calls it in a thread."""
    from . import usage_monitor

    try:
        gate = Gate.from_text(text)
    except GateError as exc:
        return _unread(None, str(exc))
    if gate is None:
        return Decision(True, "")
    if not allowed:
        return _unread(gate, "the bot no longer has the usage permission")
    settings = usage_monitor.MonitorSettings.from_config(config)
    source = source or usage_monitor.ClaudeUsageSource(config, dict(os.environ))
    now = now or usage_monitor.utc_now()
    try:
        reading, _ = asyncio.run(usage_monitor.read_on_demand(settings, source, now))
    except Exception as exc:  # a broken probe must not lose the item
        log.exception("reading the usage for a scheduled item")
        return _unread(gate, str(exc) or exc.__class__.__name__)
    if not reading.ok:
        return _unread(gate, reading.error)
    history = usage_monitor.UsageHistory(settings.history_path).readings()
    return evaluate(gate, usage_monitor.Evaluation(settings, reading, now, history))
