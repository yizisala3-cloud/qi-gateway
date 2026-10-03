"""Phase 1A planning identity and time semantics.

These pure values do not generate occurrences or perform lifecycle transitions.
Legacy ``for_date`` and ``next_due`` are intentionally not used to infer identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from hashlib import md5
import re
from uuid import UUID
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_REFRESH_BOUNDARY = time(6, 0)
REFRESH_MODES = frozenset({
    "daily", "fixed_interval", "fixed_weekday", "fixed_monthday",
    "after_completion", "none",
})
TIME_SOURCES = frozenset({"unassigned", "rule", "automatic", "manual"})
DISPLAY_REASONS = frozenset({"initial", "carryover", "manual_defer"})
FIXED_REFRESH_MODES = frozenset({"daily", "fixed_interval", "fixed_weekday", "fixed_monthday"})
CALENDAR_FIXED_MODES = frozenset({"daily", "fixed_weekday", "fixed_monthday"})
EARLY_CAPABLE_MODES = frozenset({
    "fixed_interval", "fixed_weekday", "fixed_monthday", "after_completion",
})
TASK_REFRESH_MODES = {
    "daily": frozenset({"daily"}),
    "interval": frozenset({"fixed_interval", "after_completion"}),
    "weekly": frozenset({"fixed_weekday"}),
    "monthly": frozenset({"fixed_monthday"}),
    "once": frozenset({"none"}),
    "idle": frozenset({"none"}),
}
# fixed keys derive from the due event itself, so their date segment is the due
# calendar date and need not equal the schedule (creation cycle) date.
_TIMED_ROUND_RE = re.compile(r"^(fixed|handled|early):(\d{4}-\d{2}-\d{2}):([0-9a-f]{32})$")


def parse_refresh_boundary(value: str | time) -> time:
    """Parse a minute-precision wall-clock boundary in the business timezone."""
    if isinstance(value, time):
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("refresh boundary must be a local HH:MM time")
        return value
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("refresh boundary must be HH:MM")
    hour, minute = value[:2], value[3:]
    if not hour.isdigit() or not minute.isdigit():
        raise ValueError("refresh boundary must be HH:MM")
    if int(hour) > 23 or int(minute) > 59:
        raise ValueError("refresh boundary must be within 24 hours")
    return time(int(hour), int(minute))


@dataclass(frozen=True)
class PlanningCycle:
    """A cycle key is its start date in Asia/Shanghai, independent of execution."""

    key: date
    start: datetime
    end: datetime

    @classmethod
    def at(cls, instant: datetime, boundary: time = DEFAULT_REFRESH_BOUNDARY) -> PlanningCycle:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("planning instant must have a timezone")
        boundary = parse_refresh_boundary(boundary)
        local = instant.astimezone(BUSINESS_TIMEZONE)
        key = local.date()
        start = datetime.combine(key, boundary, BUSINESS_TIMEZONE)
        if local < start:
            key -= timedelta(days=1)
            start = datetime.combine(key, boundary, BUSINESS_TIMEZONE)
        end = datetime.combine(key + timedelta(days=1), boundary, BUSINESS_TIMEZONE)
        return cls(key, start, end)

    @classmethod
    def for_key(cls, key: date, boundary: time = DEFAULT_REFRESH_BOUNDARY) -> PlanningCycle:
        boundary = parse_refresh_boundary(boundary)
        return cls(
            key,
            datetime.combine(key, boundary, BUSINESS_TIMEZONE),
            datetime.combine(key + timedelta(days=1), boundary, BUSINESS_TIMEZONE),
        )


def calendar_round_key(cycle_key: date) -> str:
    """Stable key for a daily, weekday, month-day, or fixed-interval round."""
    return f"cycle:{cycle_key.isoformat()}"


def timed_round_key(kind: str, cycle_key: date, token: str) -> str:
    """Stable identity from a due baseline or a distinct early action token."""
    if kind not in ("fixed", "handled", "early") or not token:
        raise ValueError("invalid timed round")
    digest = md5(token.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{kind}:{cycle_key.isoformat()}:{digest}"


def fixed_round_key(due_at: datetime) -> str:
    """Fixed-interval round identity derived from its due event.

    The key never depends on a refresh boundary, so the same due event maps to
    the same round across boundary changes and lost progress writes. The due
    instant is canonicalized to the business timezone with whole-second
    precision first: the same instant expressed in UTC, Beijing time or with
    different ISO spellings always yields the same identity, and the identity
    never depends on the caller having converted zones.
    """
    if not isinstance(due_at, datetime) or due_at.tzinfo is None or due_at.utcoffset() is None:
        raise ValueError("fixed due event must be a timezone-aware datetime")
    canonical = due_at.astimezone(BUSINESS_TIMEZONE).replace(microsecond=0)
    return timed_round_key("fixed", canonical.date(), canonical.isoformat())


def round_phase_group(task_id: int, round_key: str) -> UUID:
    """Both phases of one round derive the same group without a second allocator."""
    return UUID(md5(f"{task_id}:{round_key}".encode("utf-8"), usedforsecurity=False).hexdigest())


def validate_round_key(round_key: str, schedule_date: date) -> None:
    if round_key == "once" or round_key == calendar_round_key(schedule_date):
        return
    match = _TIMED_ROUND_RE.fullmatch(round_key)
    if not match:
        raise ValueError("round key does not match its original cycle")
    kind, key_date = match.group(1), date.fromisoformat(match.group(2))
    # fixed rounds are identified by their due event, not by a boundary-derived
    # cycle date; handled/early rounds still must agree with their schedule date.
    if kind in ("handled", "early") and key_date != schedule_date:
        raise ValueError("round key does not match its original cycle")


@dataclass(frozen=True)
class BoundaryTransition:
    """A boundary change takes effect from the next planning cycle.

    The cycle already in progress at the first change is **frozen**: its key
    (``spanning_key``) and the boundary it started on (``spanning_boundary``)
    never change, no matter how many times the boundary is re-configured while
    the transition is pending. Each later change only recomputes
    ``effective_at`` — the first occurrence of the newly configured boundary
    after that change on a day after the spanning cycle's own date — so the
    current cycle identity survives A→B, B→C and A→B→A alike, and restarts
    reconstruct the same semantics from the persisted record.
    """

    spanning_key: date
    spanning_boundary: time
    change_at: datetime
    effective_at: datetime

    def __post_init__(self) -> None:
        parse_refresh_boundary(self.spanning_boundary)
        if self.change_at.tzinfo is None or self.change_at.utcoffset() is None:
            raise ValueError("boundary change instant must have a timezone")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("boundary effective instant must have a timezone")
        if self.effective_at <= self.change_at:
            raise ValueError("boundary transition must become effective after the change")

    @classmethod
    def plan_first(cls, previous: time, change_at: datetime, new_boundary: time) -> "BoundaryTransition":
        """First change: the spanning cycle is the one containing the change."""
        spanning = PlanningCycle.at(change_at, previous).key
        return cls.plan(spanning, previous, change_at, new_boundary)

    @classmethod
    def plan(
        cls, spanning_key: date, spanning_boundary: time,
        change_at: datetime, new_boundary: time,
    ) -> "BoundaryTransition":
        """Re-plan the pending transition, preserving the frozen spanning cycle.

        ``new_boundary`` may equal ``spanning_boundary`` (e.g. A→B→A): the
        current cycle then simply runs until its next natural boundary point.
        """
        parse_refresh_boundary(spanning_boundary)
        if change_at.tzinfo is None or change_at.utcoffset() is None:
            raise ValueError("boundary change instant must have a timezone")
        # The next cycle key is its start date, so the first new-boundary
        # occurrence must fall after the spanning cycle's own date; otherwise
        # two cycles would share one key.
        day = change_at.astimezone(BUSINESS_TIMEZONE).date()
        while True:
            candidate = datetime.combine(day, new_boundary, BUSINESS_TIMEZONE)
            if candidate > change_at and day > spanning_key:
                return cls(spanning_key, spanning_boundary,
                           change_at.astimezone(BUSINESS_TIMEZONE), candidate)
            day += timedelta(days=1)

    @property
    def spanning_cycle_key(self) -> date:
        return self.spanning_key

    def active_at(self, instant: datetime) -> bool:
        return instant < self.effective_at

    def absorbed_cycle_keys(self) -> list[date]:
        """Cycle keys actively skipped by this transition.

        The spanning cycle is extended to the first occurrence of the new
        boundary, so any day strictly between the spanning cycle's date and
        the effective date never names a planning cycle. These days must never
        be backfilled as missed runs later on.
        """
        days = []
        day = self.spanning_key + timedelta(days=1)
        while day < self.effective_at.date():
            days.append(day)
            day += timedelta(days=1)
        return days

    def cycle_at(self, instant: datetime, configured_boundary: time) -> PlanningCycle:
        """The planning cycle containing ``instant`` under the transition regime."""
        if not self.active_at(instant):
            return PlanningCycle.at(instant, configured_boundary)
        key = min(PlanningCycle.at(instant, self.spanning_boundary).key, self.spanning_key)
        return PlanningCycle.for_key(key, self.spanning_boundary)


def planning_cycle_at(
    instant: datetime, configured_boundary: time,
    transition: BoundaryTransition | None = None,
) -> PlanningCycle:
    """Cycle containing ``instant``; a pending transition extends the frozen
    spanning cycle until the new boundary first occurs."""
    if transition is not None and transition.active_at(instant):
        cycle = transition.cycle_at(instant, configured_boundary)
        if cycle.key == transition.spanning_key:
            return replace(cycle, end=transition.effective_at)
        return cycle
    return PlanningCycle.at(instant, configured_boundary)


def cycle_start_boundary(
    cycle_key: date, configured_boundary: time,
    transition: BoundaryTransition | None = None,
) -> time:
    """Boundary that governed the start of the cycle keyed ``cycle_key``."""
    if transition is not None and cycle_key <= transition.spanning_key:
        return transition.spanning_boundary
    return configured_boundary


@dataclass(frozen=True)
class OccurrenceIdentity:
    """One business round can have two phase rows but only one display cycle.

    A round generated under the rules of its time is a settled business fact:
    identity is frozen at creation and no later rule or boundary edit may move
    its display before its original schedule date.
    """

    task_id: int
    round_key: str
    schedule_date: date
    display_cycle_date: date
    display_reason: str = "initial"
    phase: str | None = None
    phase_group: UUID | None = None

    def __post_init__(self) -> None:
        if self.task_id < 1 or not self.round_key:
            raise ValueError("task and round identities are required")
        validate_round_key(self.round_key, self.schedule_date)
        if self.display_reason not in DISPLAY_REASONS:
            raise ValueError("invalid display reason")
        if self.display_cycle_date < self.schedule_date:
            raise ValueError("display cycle can never precede the original schedule date")
        if (self.display_reason == "initial") != (self.display_cycle_date == self.schedule_date):
            raise ValueError("display reason must agree with the display cycle")
        if self.phase not in (None, "start", "end"):
            raise ValueError("invalid occurrence phase")
        if (self.phase is None) != (self.phase_group is None):
            raise ValueError("hollow phases need one shared phase group")
        if self.phase_group is not None and self.phase_group != round_phase_group(self.task_id, self.round_key):
            raise ValueError("phase group must be derived from the business round")

    def carried_to(self, cycle_key: date, *, manual: bool = False) -> OccurrenceIdentity:
        if cycle_key <= self.display_cycle_date:
            raise ValueError("carryover must advance the display cycle")
        return replace(
            self,
            display_cycle_date=cycle_key,
            display_reason="manual_defer" if manual else "carryover",
        )


@dataclass(frozen=True)
class EstimatedTimeOwnership:
    """Persisted time and ownership are separate from round and display keys."""

    source: str = "unassigned"
    fixed_source: str | None = None
    schedule_managed: bool = True
    estimated_start: datetime | None = None
    estimated_end: datetime | None = None

    def __post_init__(self) -> None:
        if self.source not in TIME_SOURCES:
            raise ValueError("invalid estimated time source")
        if self.fixed_source not in (None, "rule", "manual"):
            raise ValueError("invalid fixed source")
        if self.fixed_source == "manual" and self.source != "manual":
            raise ValueError("manual fixed time must have manual ownership")
        if self.fixed_source == "rule" and self.source != "rule":
            raise ValueError("rule fixed time must have rule ownership")
        if self.source == "manual" and (self.fixed_source != "manual" or self.schedule_managed):
            raise ValueError("manual estimated time must be fixed and user-owned")
        if self.source != "manual" and not self.schedule_managed:
            raise ValueError("system estimated time must remain schedule-managed")
        if self.fixed_source is not None and self.estimated_start is None:
            raise ValueError("fixed time needs an estimated start anchor")
        for instant in (self.estimated_start, self.estimated_end):
            if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
                raise ValueError("estimated instants must have a timezone")
        if self.estimated_end is not None and (
            self.estimated_start is None or self.estimated_end <= self.estimated_start
        ):
            raise ValueError("estimated end must follow estimated start")
        if (self.estimated_start is None) != (self.estimated_end is None):
            raise ValueError("estimated time must be a complete interval")
        if self.source == "unassigned" and self.estimated_start is not None:
            raise ValueError("unassigned time cannot contain an estimate")
        if self.source != "unassigned" and self.estimated_start is None:
            raise ValueError("assigned time needs a complete estimate")


@dataclass(frozen=True)
class RefreshDefinition:
    """Rule-owned baseline; neither display nor estimated start advances it."""

    mode: str
    anchor_at: datetime | None = None
    last_handled_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.mode not in REFRESH_MODES:
            raise ValueError("invalid refresh mode")
        if self.mode == "fixed_interval" and self.anchor_at is None:
            raise ValueError("fixed interval needs a stable first baseline")
        if self.mode != "fixed_interval" and self.anchor_at is not None:
            raise ValueError("only fixed interval rounds have a fixed anchor")
        if self.mode != "after_completion" and self.last_handled_at is not None:
            raise ValueError("only handled rounds have a completion baseline")
        for instant in (self.anchor_at, self.last_handled_at):
            if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
                raise ValueError("refresh instants must have a timezone")


def validate_task_refresh_mode(task_type: str, mode: str) -> None:
    """Reject ambiguous legacy interval modes instead of guessing their lifecycle."""
    if mode not in TASK_REFRESH_MODES.get(task_type, ()):
        raise ValueError("refresh mode does not match task type")
