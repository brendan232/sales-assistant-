"""Local web interface: pipeline on one side, chat on the other.

Single user, single conversation, served on 127.0.0.1 only. A turn runs in a
background thread and reports through an event log that the page polls, so
tool progress and approval requests show up while the agent is still working.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from .agent import SalesAgent, build_connectors, describe_error
from .config import Settings, get_settings
from .prompts import BRIEF
from .tools import Approver, pipeline_snapshot

STATIC = Path(__file__).parent / "static"
APPROVAL_TIMEOUT_S = 15 * 60
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class Conversation:
    """Event log plus the state needed to run one turn at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict] = []
        self.epoch = uuid.uuid4().hex  # changes on reset so the page knows to clear itself
        self.busy = False
        self._pending: dict[str, dict] = {}

    def emit(self, event_type: str, /, **data) -> dict:
        with self._lock:
            event = {"seq": len(self.events) + 1, "type": event_type, **data}
            self.events.append(event)
            return event

    def since(self, after: int) -> list[dict]:
        with self._lock:
            return self.events[after:]

    def try_start(self) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            return True

    def finish(self) -> None:
        with self._lock:
            self.busy = False

    def reset(self) -> None:
        with self._lock:
            self.events = []
            self.epoch = uuid.uuid4().hex

    # -- approvals ---------------------------------------------------------
    def request_approval(self, kind: str, preview: str) -> bool:
        approval_id = uuid.uuid4().hex
        entry = {"done": threading.Event(), "approved": False}
        with self._lock:
            self._pending[approval_id] = entry
        self.emit("approval", id=approval_id, kind=kind, preview=preview)
        answered = entry["done"].wait(APPROVAL_TIMEOUT_S)
        with self._lock:
            self._pending.pop(approval_id, None)
        approved = answered and entry["approved"]
        self.emit("approval_resolved", id=approval_id, approved=approved, timed_out=not answered)
        return approved

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        with self._lock:
            entry = self._pending.get(approval_id)
        if entry is None:
            return False
        entry["approved"] = approved
        entry["done"].set()
        return True


class WebApprover(Approver):
    """Asks the rep in the browser and blocks the tool call until they answer."""

    def __init__(self, conversation: Conversation) -> None:
        super().__init__("ask")
        self._conversation = conversation

    def check(self, kind: str, preview: str) -> str | None:
        if self._conversation.request_approval(kind, preview):
            return None
        return "The rep declined this action. Do not retry it; ask what they would like changed."


def create_app(
    settings: Settings | None = None, *, client=None, effort: str = "high", web_search: bool = True
) -> Flask:
    settings = settings or get_settings()
    connectors = build_connectors(settings)
    conversation = Conversation()
    state: dict = {}

    def new_agent() -> SalesAgent:
        return SalesAgent(
            settings, connectors, WebApprover(conversation), client=client, effort=effort, web_search=web_search,
            on_progress=lambda line: conversation.emit("progress", text=line.strip(" ·")),
        )

    state["agent"] = new_agent()
    app = Flask(__name__, static_folder=None)

    @app.before_request
    def local_only():
        # Bound to loopback already; these checks stop other websites open in the browser from driving the agent.
        if request.host.split(":")[0] not in LOCAL_HOSTS:
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                abort(403)
            if not request.is_json:
                abort(415)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/api/status")
    def status():
        return jsonify(
            rep=settings.rep_name,
            connectors=[
                {"name": c.name, "sample": "sample data" in c.name}
                for c in (connectors.crm, connectors.meetings, connectors.workspace)
            ],
        )

    @app.get("/api/pipeline")
    def pipeline():
        try:
            return jsonify(pipeline_snapshot(connectors))
        except Exception as exc:
            return jsonify(error=f"{type(exc).__name__}: {exc}"), 502

    @app.get("/api/events")
    def events():
        after = request.args.get("after", 0, type=int)
        if request.args.get("epoch") not in (None, "", conversation.epoch):
            after = 0
        return jsonify(epoch=conversation.epoch, busy=conversation.busy, events=conversation.since(after))

    def run_turn(agent: SalesAgent, prompt: str) -> None:
        try:
            conversation.emit("answer", text=agent.ask(prompt))
        except Exception as exc:
            conversation.emit("error", text=describe_error(exc) or f"Something went wrong: {type(exc).__name__}: {exc}")
        finally:
            conversation.finish()

    def start_turn(shown: str, prompt: str):
        if not conversation.try_start():
            return jsonify(error="The assistant is still working on the previous message."), 409
        conversation.emit("user", text=shown)
        threading.Thread(target=run_turn, args=(state["agent"], prompt), daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/chat")
    def chat():
        text = str(request.get_json().get("text", "")).strip()
        if not text:
            return jsonify(error="Empty message."), 400
        return start_turn(text, text)

    @app.post("/api/brief")
    def brief():
        return start_turn("Prepare my daily pipeline brief.", BRIEF)

    @app.post("/api/approval")
    def approval():
        body = request.get_json()
        if not conversation.resolve_approval(str(body.get("id", "")), bool(body.get("approve"))):
            return jsonify(error="That approval is no longer pending."), 404
        return jsonify(ok=True)

    @app.post("/api/reset")
    def reset():
        if conversation.busy:
            return jsonify(error="Wait for the current answer before starting a new conversation."), 409
        state["agent"] = new_agent()
        conversation.reset()
        return jsonify(ok=True)

    return app
