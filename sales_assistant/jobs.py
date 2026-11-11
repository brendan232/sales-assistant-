"""What the assistant does on its own: the morning brief and post-call follow-up.

`run_due` is the single entry point the scheduler calls every few minutes. It
is safe to call as often as you like: state on disk records what has already
been done, and a lock stops two runs overlapping.
"""

from __future__ import annotations

import fcntl
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .agent import SalesAgent, build_connectors, describe_error
from .config import ROOT, Settings, get_settings
from .prompts import BRIEF, BRIEF_DRAFTS, POST_CALL
from .signals import parse_day
from .state import Store
from .tools import Connectors, QueueApprover

MAX_CALLS_PER_RUN = 5
MAX_ATTEMPTS = 2
CALL_LOOKBACK_DAYS = 3


@dataclass
class Context:
    settings: Settings
    connectors: Connectors
    store: Store
    client: object = None
    effort: str = "high"
    web_search: bool = True

    @classmethod
    def create(cls, **overrides) -> "Context":
        settings = overrides.pop("settings", None) or get_settings()
        return cls(settings=settings, connectors=build_connectors(settings), store=overrides.pop("store", None) or Store(), **overrides)

    def agent(self, source: str, allow_drafts: bool) -> SalesAgent:
        return SalesAgent(
            self.settings, self.connectors, QueueApprover(self.store, source, allow_drafts=allow_drafts),
            client=self.client, effort=self.effort, web_search=self.web_search, store=self.store,
            on_progress=lambda line: print(line, file=sys.stderr),
        )


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", file=sys.stderr, flush=True)


# -- morning brief -----------------------------------------------------------
def run_brief(ctx: Context, *, email: bool | None = None, create_drafts: bool | None = None) -> dict:
    """Write the brief, store it as an update, save it under briefs/, and optionally email it to the rep."""
    email = ctx.settings.brief_email if email is None else email
    create_drafts = ctx.settings.auto_drafts if create_drafts is None else create_drafts
    today = date.today()

    agent = ctx.agent(source=f"Daily brief {today.isoformat()}", allow_drafts=create_drafts)
    body = agent.ask(BRIEF + (BRIEF_DRAFTS if create_drafts else ""))

    title = f"Pipeline brief, {today:%A %-d %B}"
    update = ctx.store.add_update("brief", title, body)
    out_dir = ROOT / "briefs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{today.isoformat()}.md").write_text(f"# {title}\n\n{body}\n")

    if email:
        try:
            ctx.connectors.workspace.send_to_self(title, body)
            log("Brief emailed to you.")
        except Exception as exc:
            log(f"Brief written but could not be emailed: {type(exc).__name__}: {exc}")
    return update


def brief_is_due(ctx: Context, now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hour, _, minute = ctx.settings.brief_time.partition(":")
    due_at = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
    return now >= due_at and ctx.store.read().get("last_brief") != now.date().isoformat()


# -- post-call follow-up -----------------------------------------------------
def _external_attendees(meeting: dict, rep_email: str) -> list[dict]:
    own_domain = rep_email.split("@")[-1].lower() if "@" in rep_email else ""
    people = [a for a in meeting.get("attendees", []) if a.get("email")]
    if not own_domain:
        return people
    return [a for a in people if not a["email"].lower().endswith("@" + own_domain)]


def new_calls(ctx: Context, today: date) -> list[dict]:
    """Recorded calls not yet followed up. The first run only looks back one day, so it does not flood you."""
    meetings = ctx.connectors.meetings.list_meetings("", CALL_LOOKBACK_DAYS)
    state = ctx.store.read()
    if not state.get("calls_initialised"):
        cutoff = today - timedelta(days=1)
        old = [m["id"] for m in meetings if (parse_day(m.get("date")) or today) < cutoff]

        def init(s: dict) -> None:
            s["calls_initialised"] = True
            s.setdefault("seen_calls", {}).update({mid: "skipped (before first run)" for mid in old})

        ctx.store.change(init)
        state = ctx.store.read()
    seen = state.get("seen_calls", {})
    attempts = state.get("call_attempts", {})
    return [m for m in meetings if m["id"] not in seen and attempts.get(m["id"], 0) < MAX_ATTEMPTS]


def run_post_call(ctx: Context, meeting: dict) -> dict | None:
    """Follow up one recorded call. Returns the stored update, or None if the call was internal."""
    rep_email = ctx.settings.rep_email or ctx.connectors.workspace.my_address()
    external = _external_attendees(meeting, rep_email)
    if meeting.get("attendees") and not external:
        ctx.store.change(lambda s: s.setdefault("seen_calls", {}).__setitem__(meeting["id"], "internal"))
        return None

    agent = ctx.agent(source=f"Call: {meeting['title']} ({meeting['date']})", allow_drafts=ctx.settings.auto_drafts)
    body = agent.ask(
        POST_CALL.format(
            title=meeting["title"], date=meeting["date"], meeting_id=meeting["id"],
            attendees=", ".join(f"{a.get('name') or a['email']} <{a['email']}>" for a in external) or "not recorded",
        )
    )
    update = ctx.store.add_update("post_call", f"After your call: {meeting['title']}", body, meeting_id=meeting["id"])
    ctx.store.change(lambda s: s.setdefault("seen_calls", {}).__setitem__(meeting["id"], update["id"]))
    return update


# -- scheduler entry point ---------------------------------------------------
def run_due(ctx: Context, now: datetime | None = None, *, force_brief: bool = False) -> list[dict]:
    """Do whatever is due right now. Returns the updates produced."""
    now = now or datetime.now()
    lock_path = ctx.store.dir / ".jobs.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another run is still in progress; skipping this one.")
            return []
        return _run_due(ctx, now, force_brief)


def _run_due(ctx: Context, now: datetime, force_brief: bool) -> list[dict]:
    produced: list[dict] = []

    try:
        calls = new_calls(ctx, now.date())
    except Exception as exc:
        log(f"Could not check for new calls: {type(exc).__name__}: {exc}")
        calls = []
    for meeting in calls[:MAX_CALLS_PER_RUN]:
        log(f"Following up call: {meeting['title']}")
        outcome = _attempt(lambda: run_post_call(ctx, meeting), produced)
        if outcome == "claude_unavailable":
            return produced  # will hit every job; stop and try again next run
        if outcome == "failed":
            # Something about this call itself went wrong. Count it so a bad recording is not retried forever.
            ctx.store.change(lambda s, mid=meeting["id"]: s.setdefault("call_attempts", {}).__setitem__(mid, s.get("call_attempts", {}).get(mid, 0) + 1))

    if force_brief or brief_is_due(ctx, now):
        log("Writing the daily brief.")
        if _attempt(lambda: run_brief(ctx), produced) == "ok":
            ctx.store.change(lambda s: s.__setitem__("last_brief", now.date().isoformat()))

    if not produced:
        log("Nothing due.")
    return produced


def _attempt(job, produced: list[dict]) -> str:
    """Run one job. Returns "ok", "claude_unavailable" (credentials, rate limit, network) or "failed"."""
    try:
        update = job()
    except Exception as exc:
        message = describe_error(exc)
        log(message or f"Job failed: {type(exc).__name__}: {exc}")
        return "claude_unavailable" if message else "failed"
    if update:
        produced.append(update)
        log(f"Done: {update['title']}")
    return "ok"
