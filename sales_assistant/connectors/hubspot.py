"""HubSpot CRM connector (private app token, CRM v3/v4 REST API)."""

from __future__ import annotations

from datetime import datetime, timezone

import requests

BASE = "https://api.hubapi.com"
DEAL_PROPS = [
    "dealname", "amount", "dealstage", "pipeline", "closedate", "createdate",
    "hubspot_owner_id", "hs_next_step", "notes_last_updated", "notes_last_contacted",
    "hs_lastmodifieddate",
]
# HubSpot-defined association type ids.
NOTE_TO_DEAL = 214
TASK_TO_DEAL = 216
MAX_DEALS = 100


class HubSpotCRM:
    name = "HubSpot"

    def __init__(self, token: str, owner_id: str = "") -> None:
        self._owner_id = owner_id
        self._http = requests.Session()
        self._http.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self._stages: dict[str, str] | None = None

    # -- low level ---------------------------------------------------------
    def _call(self, method: str, path: str, **kwargs) -> dict:
        resp = self._http.request(method, BASE + path, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"HubSpot {method} {path} failed: {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def _stage_label(self, stage_id: str | None) -> str:
        if self._stages is None:
            self._stages = {}
            for pipeline in self._call("GET", "/crm/v3/pipelines/deals").get("results", []):
                for stage in pipeline.get("stages", []):
                    self._stages[stage["id"]] = stage["label"]
        return self._stages.get(stage_id or "", stage_id or "")

    def _associated_ids(self, to_type: str, deal_ids: list[str]) -> dict[str, list[str]]:
        """deal id -> ids of associated objects of `to_type`."""
        out: dict[str, list[str]] = {d: [] for d in deal_ids}
        for i in range(0, len(deal_ids), 100):
            body = {"inputs": [{"id": d} for d in deal_ids[i : i + 100]]}
            data = self._call("POST", f"/crm/v4/associations/deals/{to_type}/batch/read", json=body)
            for row in data.get("results", []):
                out[str(row["from"]["id"])] = [str(t["toObjectId"]) for t in row.get("to", [])]
        return out

    def _batch_read(self, object_type: str, ids: list[str], properties: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        unique = sorted(set(ids))
        for i in range(0, len(unique), 100):
            body = {"properties": properties, "inputs": [{"id": x} for x in unique[i : i + 100]]}
            data = self._call("POST", f"/crm/v3/objects/{object_type}/batch/read", json=body)
            for row in data.get("results", []):
                out[str(row["id"])] = row.get("properties", {})
        return out

    def _normalise(self, deal_id: str, p: dict, contacts: list[dict], company: dict | None) -> dict:
        last = max(filter(None, [p.get("notes_last_updated"), p.get("notes_last_contacted")]), default=None)
        return {
            "id": deal_id,
            "name": p.get("dealname"),
            "amount": float(p["amount"]) if p.get("amount") else None,
            "stage": self._stage_label(p.get("dealstage")),
            "close_date": (p.get("closedate") or "")[:10] or None,
            "created_at": (p.get("createdate") or "")[:10] or None,
            "last_activity_at": (last or "")[:10] or None,
            "next_step": p.get("hs_next_step") or "",
            "company": company or {},
            "contacts": contacts,
        }

    def _hydrate(self, raw: dict[str, dict]) -> list[dict]:
        deal_ids = list(raw)
        contact_map = self._associated_ids("contacts", deal_ids)
        company_map = self._associated_ids("companies", deal_ids)
        contacts = self._batch_read(
            "contacts", [c for ids in contact_map.values() for c in ids], ["firstname", "lastname", "email", "jobtitle"]
        )
        companies = self._batch_read("companies", [c for ids in company_map.values() for c in ids], ["name", "domain"])
        deals = []
        for deal_id, props in raw.items():
            people = [
                {
                    "id": cid,
                    "name": " ".join(filter(None, [contacts[cid].get("firstname"), contacts[cid].get("lastname")])),
                    "title": contacts[cid].get("jobtitle") or "",
                    "email": contacts[cid].get("email") or "",
                }
                for cid in contact_map[deal_id]
                if cid in contacts
            ]
            comp_ids = [c for c in company_map[deal_id] if c in companies]
            company = (
                {"name": companies[comp_ids[0]].get("name"), "domain": companies[comp_ids[0]].get("domain")}
                if comp_ids
                else None
            )
            deals.append(self._normalise(deal_id, props, people, company))
        return deals

    # -- connector interface ----------------------------------------------
    def list_open_deals(self) -> list[dict]:
        filters = [{"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}]
        if self._owner_id:
            filters.append({"propertyName": "hubspot_owner_id", "operator": "EQ", "value": self._owner_id})
        raw: dict[str, dict] = {}
        after = None
        while len(raw) < MAX_DEALS:
            body = {"filterGroups": [{"filters": filters}], "properties": DEAL_PROPS, "limit": 100}
            if after:
                body["after"] = after
            data = self._call("POST", "/crm/v3/objects/deals/search", json=body)
            for row in data.get("results", []):
                raw[str(row["id"])] = row.get("properties", {})
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return self._hydrate(raw) if raw else []

    def get_deal(self, deal_id: str) -> dict:
        data = self._call("GET", f"/crm/v3/objects/deals/{deal_id}", params={"properties": ",".join(DEAL_PROPS)})
        deal = self._hydrate({str(data["id"]): data.get("properties", {})})[0]
        note_ids = self._associated_ids("notes", [deal_id])[deal_id][-25:]
        notes = self._batch_read("notes", note_ids, ["hs_note_body", "hs_timestamp"]) if note_ids else {}
        deal["notes"] = sorted(
            ({"date": (n.get("hs_timestamp") or "")[:10], "body": n.get("hs_note_body") or ""} for n in notes.values()),
            key=lambda n: n["date"],
            reverse=True,
        )
        return deal

    def add_note(self, deal_id: str, body: str) -> dict:
        payload = {
            "properties": {"hs_note_body": body, "hs_timestamp": datetime.now(timezone.utc).isoformat()},
            "associations": [
                {"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_DEAL}]}
            ],
        }
        data = self._call("POST", "/crm/v3/objects/notes", json=payload)
        return {"status": "created", "id": data.get("id")}

    def create_task(self, deal_id: str, title: str, due_date: str, notes: str) -> dict:
        due = datetime.fromisoformat(due_date).replace(hour=9, tzinfo=timezone.utc)
        payload = {
            "properties": {
                "hs_task_subject": title,
                "hs_task_body": notes,
                "hs_timestamp": due.isoformat(),
                "hs_task_status": "NOT_STARTED",
                "hs_task_type": "TODO",
                **({"hubspot_owner_id": self._owner_id} if self._owner_id else {}),
            },
            "associations": [
                {"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": TASK_TO_DEAL}]}
            ],
        }
        data = self._call("POST", "/crm/v3/objects/tasks", json=payload)
        return {"status": "created", "id": data.get("id")}
