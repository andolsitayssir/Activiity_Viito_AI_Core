"""Executor registry + Chatwoot executor tests (offline: mocked HTTP).

The live-verification runs happened manually against the running Chatwoot
container (conversation.create + message.send succeeded); this file keeps the
offline contract stable so CI/dev never depends on a live backend.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime as dt
from typing import Any

import pytest

from server.execution.executors.base import (
    ExecutionContext,
    ExecutorError,
    action_result,
    executor_result,
)
from server.execution.executors.chatwoot import ChatwootExecutor
from server.execution.executors.registry import DomainExecutorRegistry, get_registry
from server.execution.executors.stalwart import StalwartExecutor
from server.execution.executors.onlyoffice import OnlyOfficeExecutor
from server.execution.executors.vikunja import VikunjaExecutor
from server.execution.executors.telegram import TelegramExecutor, TelegramNeedsPasswordError
from server.execution.executors.file_manager import FileExecutor


class FakeHttpClient:
    """Minimal stand-in for HttpClient with canned per-path responses."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _match(self, method: str, suffix: str) -> Any:
        # suffix is like "/conversations/search" (already stripped of account).
        for k, v in self.responses.items():
            if k.startswith(suffix) or suffix.startswith(k):
                return v
        raise AssertionError(f"no canned response for {method} {suffix}")

    def get(self, path: str, **kw: Any) -> Any:
        self.calls.append(("GET", path, kw))
        return self._match("GET", path.replace("/api/v1/accounts/2", ""))

    def post(self, path: str, **kw: Any) -> Any:
        self.calls.append(("POST", path, kw))
        return self._match("POST", path.replace("/api/v1/accounts/2", ""))

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _make_executor(responses: dict[str, Any]) -> ChatwootExecutor:
    executor = ChatwootExecutor.__new__(ChatwootExecutor)
    executor.account_id = "2"
    executor._http = FakeHttpClient(responses)  # type: ignore[assignment]
    return executor


def _ctx(actions: dict[str, Any], retries: int = 0) -> ExecutionContext:
    return ExecutionContext(
        domain="messaging",
        actions=actions,
        retry_count=retries,
    )


def test_registry_unknown_domain_returns_deferred() -> None:
    registry = DomainExecutorRegistry()
    executor = registry.executor_for("crm")  # not registered yet
    assert executor.backend == "deferred"
    result = executor.execute(_ctx({"crm.lead.create": {"fields": {"name": "x"}}}))
    assert result["status"] == "success"
    assert result["results"][0]["data"]["deferred"] is True


def test_registry_registers_telegram_for_messaging() -> None:
    registry = get_registry()
    executor = registry.executor_for("messaging")
    assert executor.backend == "telegram"


def test_conversation_create_success() -> None:
    responses = {
        "/inboxes": {"payload": [{"id": 1, "name": "Viito Test Inbox"}]},
        "/contacts": {
            "payload": {
                "contact": {"id": 7, "name": "Alice Dupont"},
                "contact_inbox": {},
            }
        },
        "/conversations": {
            "id": 42,
            "conversation_id": 42,
            "meta": {"sender": {"id": 7, "name": "Alice Dupont"}},
        },
    }
    executor = _make_executor(responses)
    result = executor.execute(
        _ctx(
            {
                "messaging.conversation.create": {
                    "fields": {"customer": "Alice Dupont"}
                }
            }
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    assert result["results"][0]["tool_calls"] == ["chatwoot:messaging.conversation.create"]
    post_calls = [c for c in executor._http.calls if c[0] == "POST"]  # type: ignore[attr-defined]
    assert any("/contacts" in c[1] for c in post_calls)
    assert any("/conversations" in c[1] for c in post_calls)


def test_message_send_success() -> None:
    responses = {
        "/conversations/search": {"payload": [{"id": 5, "name": "Demo Viito"}]},
        "/conversations/5/messages": {"id": 9, "content": "hi", "status": "sent"},
    }
    executor = _make_executor(responses)
    result = executor.execute(
        _ctx(
            {
                "messaging.message.send": {
                    "fields": {"conversation": "Demo Viito", "message_text": "hi"}
                }
            }
        )
    )
    assert result["results"][0]["data"]["ok"] is True


def test_unhandled_action_is_deferred_not_failed() -> None:
    executor = _make_executor({})
    result = executor.execute(
        _ctx({"messaging.macro.create": {"fields": {"macro_name": "relance"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["deferred"] is True
    assert entry["tool_calls"] == []


def test_missing_message_text_is_honest_error() -> None:
    executor = _make_executor(
        {"/conversations/search": {"payload": [{"id": 5, "name": "Demo"}]}}
    )
    result = executor.execute(
        _ctx({"messaging.message.send": {"fields": {"conversation": "Demo"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "missing" in entry["data"]["error"]


# ---------------------------------------------------------------------------
# Odoo executor (offline: fake execute_kw proxy).
# Live verification (container): partner/lead/sale.order/hr.employee/account.move
# create + lead.write + account.move.action_post all succeeded and the invoice
# came back state=posted.
# ---------------------------------------------------------------------------


class FakeOdooProxy:
    """Fake XML-RPC object endpoint returning canned records per call."""

    def __init__(
        self,
        relations: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.relations = relations or {
            ("account.move", "partner_id"): "res.partner",
            ("crm.lead", "team_id"): "crm.team",
        }
        self.calls: list[tuple[str, str, list, dict]] = []
        self._next_id = 100
        self.moves: dict[int, dict[str, Any]] = {}
        self.calendar_events: dict[int, dict[str, Any]] = {"15": {"id": 15, "name": "TechNova meeting", "start": "2026-08-14 09:00:00", "stop": "2026-08-14 10:00:00", "location": "Salle A", "partner_ids": [], "active": True}}
        self.partner_store_mode = False
        self.partners: dict[int, dict[str, Any]] = {
            7: {"id": 7, "name": "Viito Test Partner", "email": "testyyy44@gmail.com"},
            8: {"id": 8, "name": "Newayit", "email": "tayssirandolssi@gmail.com"},
        }
        self.tags: dict[int, dict[str, Any]] = {21: {"id": 21, "name": "Vip"}}

    def execute_kw(self, db, uid, pwd, model, method, args, kwargs):
        self.calls.append((model, method, args, kwargs))
        if model == "calendar.event" and method == "create":
            self._next_id += 1
            vals = dict(args[0])
            self.calendar_events[str(self._next_id)] = {
                "id": self._next_id,
                "name": vals.get("name"),
                "start": vals.get("start"),
                "stop": vals.get("stop"),
                "location": vals.get("location"),
                "partner_ids": [],
            }
            return self._next_id
        if model == "calendar.event" and method == "search_read":
            domain = args[0] if args and args[0] else []
            out = []
            for ev in self.calendar_events.values():
                match = True
                for cond in domain:
                    if isinstance(cond, list) and len(cond) == 3 and cond[0] == "name":
                        if cond[1] == "ilike" and cond[2].lower() not in str(ev["name"]).lower():
                            match = False
                if match:
                    out.append(dict(ev))
            return out
        if model == "calendar.event" and method == "write":
            for eid in args[0]:
                if str(eid) in self.calendar_events:
                    self.calendar_events[str(eid)].update(dict(args[1]))
            return True
        if model == "calendar.event" and method == "unlink":
            for eid in args[0]:
                self.calendar_events.pop(str(eid), None)
            return True
        if model == "calendar.alarm" and method == "search_read":
            return []
        if model == "calendar.alarm" and method == "create":
            self._next_id += 1
            return self._next_id
        if model == "res.partner" and method == "search_read" and self.partner_store_mode:
            domain = args[0] if args else []
            out = []
            for rec in self.partners.values():
                match = True
                for cond in domain:
                    if isinstance(cond, list) and len(cond) == 3:
                        field, op, val = cond
                        if op == "ilike":
                            if str(val).lower() not in str(rec.get(field) or "").lower():
                                match = False
                        elif op == "=" and str(rec.get(field)) != str(val):
                            match = False
                if match:
                    out.append(dict(rec))
            return out
        if model == "account.move" and method == "create":
            self._next_id += 1
            vals = args[0]
            self.moves[self._next_id] = {
                "id": self._next_id,
                "state": "draft",
                "name": "/",
                "ref": vals.get("ref"),
                "amount_total": vals.get("amount_total", 0.0),
                "invoice_date": vals.get("invoice_date"),
                "partner_id": vals.get("partner_id"),
            }
            return self._next_id
        if model == "account.move" and method == "search_read":
            return list(self.moves.values())
        if model == "account.move" and method == "action_post":
            for mid in args[0]:
                if mid in self.moves:
                    self.moves[mid]["state"] = "posted"
                    self.moves[mid]["name"] = f"INV/2026/{mid:04d}"
            return True
        if model == "ir.model.fields" and method == "search_read":
            domain = args[0]  # the domain list: [[model, =, X], [name, =, Y]]
            conds = {
                c[0]: c[2]
                for c in domain
                if isinstance(c, list) and len(c) == 3 and isinstance(c[0], str)
            }
            rel = self.relations.get((conds.get("model", ""), conds.get("name", "")))
            if rel:
                return [{"id": 1, "relation": rel}]
            return []
        if model == "res.partner.category" and method == "search_read":
            out = []
            for tag in self.tags.values():
                match = True
                for cond in (args[0] if args else []):
                    if isinstance(cond, list) and len(cond) == 3:
                        field, op, val = cond
                        if op == "=" and tag.get(field) != val:
                            match = False
                        elif op == "ilike" and str(val).lower() not in str(tag.get(field) or "").lower():
                            match = False
                if match:
                    out.append(dict(tag))
            return out
        if model == "res.partner.category" and method == "create":
            self._next_id += 1
            self.tags[self._next_id] = {"id": self._next_id, "name": args[0]["name"]}
            return self._next_id
        if method == "search_read":
            return [{"id": 7, "name": "Viito Test Partner", "email": "testyyy44@gmail.com"}]
        if method == "create":
            self._next_id += 1
            return self._next_id
        if method in ("write", "unlink"):
            return True
        return True

    def _refs(self) -> list[str]:
        return [f"{m}.{method}" for m, method, _a, _k in self.calls if m != "ir.model.fields"]


def _make_odoo(relations=None) -> tuple[OdooExecutor, FakeOdooProxy]:
    from server.execution.executors.odoo import OdooExecutor

    proxy = FakeOdooProxy(relations)
    executor = OdooExecutor.__new__(OdooExecutor)
    executor._models = proxy  # type: ignore[assignment]
    executor._uid = 1
    executor._common = None
    executor._url = "http://localhost:8069"
    executor.db = "oddo-db"
    executor.login = "admin@test"
    executor.password = "pw"
    executor._comodel_cache = {}
    return executor, proxy


def _odoo_ctx(domain: str, actions: dict[str, Any], intent: str = "") -> ExecutionContext:
    return ExecutionContext(domain=domain, actions=actions, intent=intent)


def test_odoo_partner_create_maps_company_to_name() -> None:
    """A company fiche ('la société Newayit') must map to res.partner.name,
    otherwise Odoo rejects the create with 'Contacts require a name'."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "contacts",
            {
                "res.partner.create": {
                    "fields": {"company": "Newayit", "email": "testyyy44@gmail.com"}
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    calls = [
        (m, method, args)
        for m, method, args, _k in proxy.calls
        if m == "res.partner" and method == "create"
    ]
    assert len(calls) == 1
    vals = calls[0][2][0]
    assert vals["name"] == "Newayit"
    assert vals["is_company"] is True
    assert vals["email"] == "testyyy44@gmail.com"


def test_odoo_partner_create_maps_address_to_street() -> None:
    """A free-text 'address' from the router must land in res.partner.street
    (multi-line in street2), not be silently dropped."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "contacts",
            {
                "res.partner.create": {
                    "fields": {
                        "company": "Newayit",
                        "email": "testyyy44@gmail.com",
                        "address": "13 rue indepedance ariana",
                    }
                }
            },
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    calls = [
        (m, method, args)
        for m, method, args, _k in proxy.calls
        if m == "res.partner" and method == "create"
    ]
    assert len(calls) == 1
    vals = calls[0][2][0]
    assert vals["street"] == "13 rue indepedance ariana"
    assert "street2" not in vals


def test_odoo_partner_create_maps_tags_to_category_id() -> None:
    """Contact tags ('le tag Prospect') must be extracted by the router and land
    in res.partner.category_id (find-or-create), not silently dropped."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "contacts",
            {
                "res.partner.create": {
                    "fields": {
                        "name": "TechNova",
                        "email": "contact@technova.com",
                        "phone": "+33123456789",
                        "city": "Paris",
                        "tags": ["Prospect", "Vip"],
                    }
                }
            },
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    create_calls = [
        (m, method, args)
        for m, method, args, _k in proxy.calls
        if m == "res.partner" and method == "create"
    ]
    assert len(create_calls) == 1
    vals = create_calls[0][2][0]
    assert vals["name"] == "TechNova"
    cmd = vals["category_id"][0]
    assert cmd[0] == 6
    tag_ids = cmd[2]
    assert 21 in tag_ids  # pre-existing "Vip"
    assert len(tag_ids) == 2
    category_creates = [
        (m, method, args)
        for m, method, args, _k in proxy.calls
        if m == "res.partner.category" and method == "create"
    ]
    assert len(category_creates) == 1  # "Prospect" only; "Vip" already exists
    assert category_creates[0][2][0]["name"] == "Prospect"


def test_odoo_partner_write_maps_tags_to_category_id() -> None:
    """Tags on an update (res.partner.write) follow the same find-or-create path."""
    executor, proxy = _make_odoo()
    proxy.partner_store_mode = True
    proxy.partners[9] = {"id": 9, "name": "TechNova", "email": "contact@technova.com"}
    result = executor.execute(
        _odoo_ctx(
            "contacts",
            {
                "res.partner.write": {
                    "fields": {"name": "TechNova", "tags": "Prospect"}
                }
            },
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    write_calls = [
        (m, method, args)
        for m, method, args, _k in proxy.calls
        if m == "res.partner" and method == "write"
    ]
    assert len(write_calls) == 1
    vals = write_calls[0][2][1]
    assert vals["category_id"][0][0] == 6


def test_odoo_lead_create_maps_fields_and_stage() -> None:
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "crm",
            {
                "crm.lead.create": {
                    "fields": {
                        "company": "Acme",
                        "email": "a@b.c",
                        "revenue": 12000,
                        "priority": "high",
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    calls = [
        (m, method, args)
        for m, method, args, _k in proxy.calls
        if m == "crm.lead" and method == "create"
    ]
    assert len(calls) == 1
    vals = calls[0][2][0]
    assert vals["name"] == "Acme"
    assert vals["expected_revenue"] == 12000
    assert vals["priority"] == "2"
    assert vals["email_from"] == "a@b.c"


def test_odoo_lead_write_resolves_target_by_name() -> None:
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "crm",
            {"crm.lead.write": {"fields": {"lead": "Acme", "revenue": 5000}}},
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    write_call = [
        (m, args)
        for m, method, args, _k in proxy.calls
        if m == "crm.lead" and method == "write"
    ]
    assert write_call, "crm.lead.write called"
    assert write_call[0][1][0] == [7]  # resolved partner/lead id
    assert write_call[0][1][1] == {"expected_revenue": 5000}


def test_odoo_invoice_create_resolves_partner_and_adds_line() -> None:
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "accounting",
            {
                "account.move.create": {
                    "fields": {"partner": "Viito Test Partner", "amount": 250}
                }
            },
        )
    )
    calls = [
        args[0]
        for m, method, args, _k in proxy.calls
        if m == "account.move" and method == "create"
    ]
    assert calls
    vals = calls[0]
    assert vals["partner_id"] == 7
    assert vals["move_type"] == "out_invoice"
    assert vals["invoice_line_ids"][0][2]["price_unit"] == 250


def test_odoo_invoice_create_maps_ref_partner_currency_and_date() -> None:
    """'facture nommée facture_newayit ... 450 EUR pour le 13 septembre pour
    la société newayit' must produce a findable invoice: ref, partner, date and
    monetary line — not a blank move."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "accounting",
            {
                "account.move.create": {
                    "fields": {
                        "invoice_number": "facture_newayit",
                        "customer": "newayit",
                        "amount": "450",
                        "currency": "EUR",
                        "invoice_day": "13",
                        "invoice_month": "septembre",
                    }
                }
            },
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    calls = [
        args[0]
        for m, method, args, _k in proxy.calls
        if m == "account.move" and method == "create"
    ]
    assert calls
    vals = calls[0]
    assert vals["ref"] == "facture_newayit"
    assert vals["partner_id"] == 7
    assert vals["invoice_date"] == "2026-09-13"
    assert vals["invoice_line_ids"][0][2]["price_unit"] == 450


def test_odoo_read_invoice_packet_returns_move_partner_lines() -> None:
    """The PDF builder's data source: an invoice found by ref yields the move,
    its partner and its product lines over the same XML-RPC channel."""
    executor, _ = _make_odoo()
    executor.execute(
        _odoo_ctx(
            "accounting",
            {
                "account.move.create": {
                    "fields": {"invoice_number": "facture_x", "customer": "newayit", "amount": 450}
                }
            },
        )
    )
    packet = executor.read_invoice_packet("facture_x")
    assert packet is not None
    assert packet["move"]["ref"] == "facture_x"
    assert packet["partner"]["name"] == "Viito Test Partner"
    assert packet["partner"]["email"] == "testyyy44@gmail.com"
    assert isinstance(packet["lines"], list)


def test_odoo_read_partner_email_resolves_by_name() -> None:
    executor, _ = _make_odoo()
    assert executor.read_partner_email("Viito Test Partner") == "testyyy44@gmail.com"


def test_odoo_invoice_download_pdf_posts_and_returns_identity() -> None:
    """`action_invoice_download_pdf` must not crash on a nonexistent RPC method:
    it confirms the draft (action_post -> real number), returns the invoice's
    identity, and lets the turn advance to email_action."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "accounting",
            {
                "account.move.create": {
                    "fields": {
                        "invoice_number": "facture_newayit",
                        "customer": "newayit",
                        "amount": "450",
                        "invoice_day": "13",
                        "invoice_month": "septembre",
                    }
                },
                "account.move.action_invoice_download_pdf": {
                    "fields": {"customer": "newayit", "invoice": "facture_newayit"}
                },
            },
        )
    )
    by_action = {r["action"]: r for r in result["results"]}
    download = by_action["account.move.action_invoice_download_pdf"]
    assert download["data"]["ok"] is True
    invoices = download["data"]["response"]["invoices"]
    assert invoices and invoices[0]["ref"] == "facture_newayit"
    assert invoices[0]["state"] == "posted"
    assert invoices[0]["name"].startswith("INV/2026/")
    assert "account.move.action_post" in proxy._refs()


def test_odoo_invoice_action_get_uses_read_invoice_packet() -> None:
    """`account.move.action_get` is a classifier-invented action, NOT an Odoo
    RPC method. It must be served locally from `read_invoice_packet` so the
    turn doesn't fail with AttributeError on a nonexistent XML-RPC call."""
    executor, proxy = _make_odoo()
    executor.execute(
        _odoo_ctx(
            "accounting",
            {
                "account.move.create": {
                    "fields": {"invoice_number": "facture_newayit", "customer": "newayit", "amount": 450}
                }
            },
        )
    )
    result = executor.execute(
        _odoo_ctx(
            "accounting",
            {
                "account.move.action_get": {
                    "fields": {"invoice": "facture_newayit"}
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    response = entry["data"]["response"]
    assert response["move"]["ref"] == "facture_newayit"
    assert response["partner"]["email"] == "testyyy44@gmail.com"
    assert "action_get" not in proxy._refs(), "action_get must not hit the RPC channel"


def test_odoo_calendaring_event_create_maps_title_and_dates() -> None:
    """`event.create` on the calendaring domain must translate to a live
    `calendar.event` create on Odoo, mapping title + start/end dates."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.create": {
                    "fields": {
                        "title": "TechNova meeting",
                        "start_day": "14",
                        "start_month": "aout",
                        "start_hour": "10",
                        "start_minute": "30",
                        "location": "Salle A",
                        "attendees": ["Jean Dupont"],
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    created = entry["data"]["response"]
    assert created["name"] == "TechNova meeting"
    assert "calendar.event" in [m for m, _x, _a, _k in proxy.calls]
    ev = next(v for v in proxy.calendar_events.values() if v["id"] == created["id"])
    assert ev["name"] == "TechNova meeting"
    assert ev["start"].startswith("2026-08-14 10:30")
    assert ev["stop"].startswith("2026-08-14 11:30")


def test_odoo_event_create_converts_local_hour_to_utc_with_user_tz() -> None:
    """The wall-clock hour the user typed must be stored as the matching UTC
    value for the Odoo account timezone, so the calendar renders 14h (not 13h
    or 15h). Africa/Tunis is UTC+1 year-round: 14:00 -> 13:00 UTC."""
    executor, proxy = _make_odoo()
    executor._user_tz_cache = "Africa/Tunis"
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.create": {
                    "fields": {
                        "title": "Réunion démo",
                        "start_day": "14",
                        "start_month": "octobre",
                        "start_hour": "14",
                        "location": "Salle B",
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    created = entry["data"]["response"]
    ev = next(v for v in proxy.calendar_events.values() if v["id"] == created["id"])
    assert ev["start"] == "2026-10-14 13:00:00"
    assert ev["stop"] == "2026-10-14 14:00:00"


def test_odoo_dt_conversion_keeps_unknown_or_invalid_tz_unchanged() -> None:
    from server.execution.executors.odoo import _to_utc_naive

    assert _to_utc_naive("2026-10-14 14:00:00", None) == "2026-10-14 14:00:00"
    assert _to_utc_naive("2026-10-14 14:00:00", "") == "2026-10-14 14:00:00"
    assert _to_utc_naive("2026-10-14 14:00:00", "Not/AZone") == "2026-10-14 14:00:00"


def test_odoo_calendaring_event_create_keeps_wallclock_when_tz_unknown() -> None:
    """Regression guard: without a resolvable Odoo user timezone the executor
    keeps the typed values exactly (no blind shift)."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.create": {
                    "fields": {
                        "title": "Rendez-vous",
                        "start_day": "14",
                        "start_month": "octobre",
                        "start_hour": "14",
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    created = entry["data"]["response"]
    ev = next(v for v in proxy.calendar_events.values() if v["id"] == created["id"])
    assert ev["start"] == "2026-10-14 14:00:00"
    assert ev["stop"] == "2026-10-14 15:00:00"


def test_odoo_calendaring_event_delete_and_query() -> None:
    """`event.delete` unlinks a matching calendar.event; `event.query` lists
    them, and multi-get/sync reuse the same local read path."""
    executor, _ = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.delete": {"fields": {"title": "TechNova meeting"}},
            },
        )
    )
    del_entry = result["results"][0]
    assert del_entry["data"]["ok"] is True
    assert del_entry["data"]["response"]["ids"] == [15]

    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.query": {"fields": {"title": "TechNova meeting"}},
            },
        )
    )
    q = result["results"][0]
    assert q["data"]["ok"] is True
    assert q["data"]["response"] == []


def test_odoo_calendaring_event_free_busy_reports_busy() -> None:
    """`event.free_busy` scans calendar.event over a range and, if any event
    overlaps, reports busy with the offending event(s)."""
    executor, _ = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.free_busy": {
                    "fields": {
                        "from_day": "14",
                        "from_month": "aout",
                        "day_to": "15",
                        "month_to": "aout",
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    resp = entry["data"]["response"]
    assert resp["free"] is False
    assert any("TechNova meeting" in b["event"] for b in resp["busy_events"])


def test_odoo_calendaring_event_recurrence_and_reminder() -> None:
    """`event.recurrence` writes an rrule; `event.reminder` creates/links a
    calendar.alarm at the requested lead time."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.recurrence": {"fields": {"title": "TechNova meeting", "recurrence": "weekly", "count": "12"}},
                "event.reminder": {"fields": {"title": "TechNova meeting", "minutes_before": "15"}},
            },
        )
    )
    by_action = {r["action"]: r for r in result["results"]}
    rec = by_action["event.recurrence"]
    assert rec["data"]["ok"] is True
    assert "FREQ=WEEKLY" in rec["data"]["response"]["rrule"]
    rem = by_action["event.reminder"]
    assert rem["data"]["ok"] is True
    assert rem["data"]["response"]["minutes_before"] == 15
    write_calls = [(m, a[0]) for m, method, a, _k in proxy.calls if method == "write"]
    assert ("calendar.event", [15]) in write_calls


def test_odoo_calendaring_event_delete_all_when_no_title() -> None:
    """« supprimer tous les réunions » reaches the executor as event.delete with
    empty fields; the intent must trigger a delete-all fallback instead of a
    hard failure."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {"event.delete": {"fields": {}}},
            intent="supprimer tous les réunions",
        )
    )
    deleted = result["results"][0]
    assert deleted["data"]["ok"] is True
    assert deleted["data"]["response"]["deleted"] == 1
    assert len(proxy.calendar_events) == 0


def test_odoo_calendaring_event_create_links_attendee_by_email() -> None:
    """An attendee passed as an email (tayssirandolssi@gmail.com for la société
    Newayit) must resolve to the matching res.partner via an email search and
    land in calendar.event.partner_ids — not be silently dropped."""
    executor, proxy = _make_odoo()
    proxy.partner_store_mode = True
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.create": {
                    "fields": {
                        "title": "Réunion Newayit",
                        "start_month": "septembre",
                        "start_day": "18",
                        "start_hour": "13",
                        "start_minute": "0",
                        "attendees": ["tayssirandolssi@gmail.com"],
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    create_call = next(
        (a for m, method, a, _k in proxy.calls if m == "calendar.event" and method == "create"),
        None,
    )
    assert create_call is not None
    assert create_call[0]["name"] == "Réunion Newayit"
    assert create_call[0]["partner_ids"] == [(4, 8)]


def test_odoo_calendaring_event_modify_updates_existing_event() -> None:
    """« Modifie la date de la réunion au 21 septembre à 9h » → event.modify
    resolves the existing calendar.event by title and writes the new start
    (never creates a duplicate)."""
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "calendaring",
            {
                "event.modify": {
                    "fields": {
                        "title": "TechNova meeting",
                        "start_day": "21",
                        "start_month": "septembre",
                        "start_hour": "9",
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["updated"] == ["name", "start", "stop"]
    write_calls = [(m, a[0]) for m, method, a, _k in proxy.calls if method == "write"]
    assert ("calendar.event", [15]) in write_calls
    assert proxy.calendar_events["15"]["start"].startswith("2026-09-21 09:00")
    assert not any(
        m == "calendar.event" and method == "create" for m, method, _a, _k in proxy.calls
    )


def test_odoo_lead_write_coerces_percent_and_currency_strings() -> None:
    # "85%" / "45000 EUR" arrive as raw classifier strings and must be parsed
    # to floats before RPC (the backend raises TypeError on str >= int).
    executor, proxy = _make_odoo()
    result = executor.execute(
        _odoo_ctx(
            "crm",
            {
                "crm.lead.write": {
                    "fields": {
                        "lead": "Acme",
                        "probability": "85%",
                        "expected_revenue": "45000 EUR",
                        "status": "Qualified",
                    }
                }
            },
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    write_call = [
        args
        for m, method, args, _k in proxy.calls
        if m == "crm.lead" and method == "write"
    ]
    assert write_call
    vals = write_call[0][1]
    assert vals["probability"] == 85.0
    assert vals["expected_revenue"] == 45000.0


def test_parse_number_extracts_french_and_thousands() -> None:
    from server.execution.executors.odoo import _parse_number

    assert _parse_number("85%") == 85.0
    assert _parse_number("45000 EUR") == 45000.0
    assert _parse_number("25 000,50 €") == 25000.5
    assert _parse_number("2,5") == 2.5
    assert _parse_number("12000 euros") == 12000.0
    assert _parse_number("not a number") == "not a number"
    assert _parse_number(12000) == 12000


def test_odoo_unknown_action_is_deferred() -> None:
    executor, _ = _make_odoo()
    result = executor.execute(
        _odoo_ctx("hr", {"hr.leave.reconcile": {"fields": {"leave": 3}}})
    )
    entry = result["results"][0]
    assert entry["data"]["deferred"] is True
    assert entry["tool_calls"] == []


# ---------------------------------------------------------------------------
# Stalwart executor (offline: fake JMAP endpoint).
# Live verification (container): Email/set + EmailSubmission/set accepted a
# send (submission id + sendAt returned), Email/query/get and Mailbox/get
# round-tripped, Mailbox/set created a mailbox.
# ---------------------------------------------------------------------------


class FakeJmapServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, path: str, **_kw: Any) -> Any:
        return {
            "primaryAccounts": {"urn:ietf:params:jmap:mail": "b"},
            "apiUrl": "https://localhost/jmap/",
        }

    def post(self, path: str, json: Any = None, **_kw: Any) -> Any:
        calls = (json or {}).get("methodCalls", [])
        responses = []
        for name, args, call_id in calls:
            self.calls.append(name)
            if name == "Mailbox/get":
                responses.append([
                    name,
                    {"accountId": "b", "list": [
                        {"id": "d", "name": "Drafts", "role": "drafts"},
                        {"id": "e", "name": "Sent Items", "role": "sent"},
                        {"id": "a", "name": "Inbox", "role": "inbox"},
                    ]},
                    call_id,
                ])
            elif name == "Identity/get":
                responses.append([name, {"accountId": "b", "list": [{"id": "id1", "email": "admin@localhost"}]}, call_id])
            elif name == "Email/set":
                created_id = args.get("force_id", "mail1")
                responses.append([name, {"accountId": "b", "created": {"draft": {"id": created_id}}}, call_id])
            elif name == "EmailSubmission/set":
                responses.append([name, {"accountId": "b", "created": {"send": {"id": "sub1", "sendAt": "2026-08-29T00:00:00Z"}}}, call_id])
            elif name == "Email/query":
                responses.append([name, {"accountId": "b", "ids": ["mail1", "mail2"]}, call_id])
            elif name == "Email/get":
                responses.append([name, {"accountId": "b", "list": [{"id": "mail1", "subject": "demo"}]}, call_id])
            elif name == "Mailbox/set":
                responses.append([name, {"accountId": "b", "created": {"m": {"id": "newbox"}}}, call_id])
            else:
                responses.append([name, {"accountId": "b"}, call_id])
        return {"methodResponses": responses}

    def close(self) -> None:
        pass


def _make_stalwart() -> tuple[StalwartExecutor, FakeJmapServer]:
    from server.execution.executors.stalwart import StalwartExecutor

    fake = FakeJmapServer()
    executor = StalwartExecutor.__new__(StalwartExecutor)
    executor._http = fake  # type: ignore[assignment]
    executor.admin_email = "admin@localhost"
    executor.admin_password = "pw"
    executor.default_sender = "admin@localhost"
    executor._session = None
    executor._account_id = None
    executor._api_path = "/jmap/"
    executor._identity_id = None
    executor._mailbox_ids = {}
    return executor, fake


def _stalwart_ctx(actions: dict[str, Any]) -> ExecutionContext:
    return ExecutionContext(domain="email_action", actions=actions)


def test_stalwart_send_email() -> None:
    executor, fake = _make_stalwart()
    result = executor.execute(
        _stalwart_ctx(
            {
                "email.email_submission.set": {
                    "fields": {"recipients": "a@x.fr, b@x.fr", "cc": "c@x.fr",
                               "subject": "hi", "content": "body"}
                }
            }
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["email_id"] == "mail1"
    assert entry["data"]["response"]["submission_id"] == "sub1"
    assert "Email/set" in fake.calls
    assert "EmailSubmission/set" in fake.calls


def test_stalwart_query_emails() -> None:
    executor, _ = _make_stalwart()
    result = executor.execute(
        _stalwart_ctx({"email.email.query": {"fields": {"search_query": "devis"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["ids"] == ["mail1", "mail2"]


def test_stalwart_mailbox_create() -> None:
    executor, _ = _make_stalwart()
    result = executor.execute(
        _stalwart_ctx({"email.mailbox.create": {"fields": {"mailbox": "Archive"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["id"] == "newbox"


def test_stalwart_blob_is_deferred() -> None:
    executor, _ = _make_stalwart()
    result = executor.execute(
        _stalwart_ctx({"email.blob.lookup": {"fields": {"attachment": "devis.pdf"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["deferred"] is True


# ---------------------------------------------------------------------------
# Gmail executor: invoice-by-email (recipient resolution + PDF attachment)
# ---------------------------------------------------------------------------
class FakeGmailSendHttp:
    """Captures Gmail sends without touching the network."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        self.sent.append(json or {})
        return {"id": "1", "thread_id": "t1"}

    def get(self, path: str, **kw: Any) -> Any:
        raise AssertionError(f"unexpected GET: {path}")


def _gmail_email_ctx(actions: dict[str, Any], log: list[dict[str, Any]]) -> ExecutionContext:
    return ExecutionContext(
        domain="email_action",
        actions=actions,
        executor_log=log,
    )


def _make_gmail(monkeypatch, fake_odoo: Any) -> tuple[Any, FakeGmailSendHttp]:
    import time
    from server.execution.executors.gmail import GmailExecutor

    http = FakeGmailSendHttp()
    executor = GmailExecutor.__new__(GmailExecutor)
    executor._tokens = {"access_token": "t", "expires_at": time.time() + 3600}  # type: ignore[attr-defined]
    monkeypatch.setattr(GmailExecutor, "_get_authenticated_http", lambda self: http)
    monkeypatch.setattr(GmailExecutor, "_odoo", lambda self: fake_odoo)
    return executor, http


def _invoice_log_result(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    invoice_reco = {
        "id": 63,
        "name": "INV/2026/00011",
        "ref": "facture_newayit",
        "state": "posted",
        "amount_total": 450.0,
        "invoice_date": "2026-09-13",
        "partner_id": [107, "newayit"],
    }
    download = action_result(
        "account.move.action_invoice_download_pdf",
        params={"invoice": "facture_newayit"},
        data={"ok": True, "response": {"invoices": [invoice_reco]}},
    )
    return log + [executor_result("accounting", [download])]


def test_gmail_invoice_email_attaches_pdf_and_resolves_recipient(monkeypatch) -> None:
    """Query said 'à l'adresse email de la société' with no address given: the
    sender must drop classifier fragments, resolve the partner's email from
    Odoo, and attach the invoice PDF from the same turn's log."""
    class FakeOdoo:
        def read_partner_email(self, target: Any) -> str:
            return "testyyy44@gmail.com"

        def read_invoice_packet(self, ref: Any) -> dict[str, Any]:
            return {
                "move": {"id": 63, "name": "INV/2026/00011", "ref": "facture_newayit",
                         "state": "posted", "invoice_date": "2026-09-13", "amount_total": 450.0,
                         "currency_id": [2, "EUR"], "partner_id": [107, "newayit"]},
                "partner": {"name": "newayit", "email": "testyyy44@gmail.com"},
                "lines": [{"name": "Service", "quantity": 1, "price_unit": 450.0, "price_subtotal": 450.0}],
            }

    executor, http = _make_gmail(monkeypatch, FakeOdoo())
    ctx = _gmail_email_ctx(
        {
            "email.email_submission.set": {
                "fields": {
                    "recipients": [
                        "cette societe du montant 450 EUR pour",
                        "envoie la par mail a l'adress email de la société",
                    ],
                    "subject": "Votre facture facture_newayit",
                    "content": "Bonjour, veuillez trouver votre facture en pièce jointe.",
                }
            }
        },
        _invoice_log_result([]),
    )
    result = executor.execute(ctx)
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert http.sent, "a send was issued"

    import email as email_lib
    import base64 as b64
    raw_payload = http.sent[0]["raw"]
    msg = email_lib.message_from_string(b64.urlsafe_b64decode(raw_payload).decode("utf-8"))
    assert "testyyy44@gmail.com" in (msg["To"] or "")
    assert "cette societe" not in (msg["To"] or "")
    pdf_parts = [
        part for part in msg.walk()
        if part.get_content_type() == "application/pdf"
    ]
    assert pdf_parts, "a PDF attachment is present"
    assert "INV/2026/00011.pdf" in (pdf_parts[0].get("Content-Disposition") or "")
    assert pdf_parts[0].get_payload(decode=True).startswith(b"%PDF")


def test_gmail_find_turn_invoice_from_action_get_resolves_and_sends(monkeypatch) -> None:
    """A 'envoie la facture à <email>' follow-up: the accounting domain emits
    `account.move.action_get` (served locally, not an RPC method). The email
    turn must recover that invoice from the log and attach its PDF, sending to
    the explicit recipient."""
    class FakeOdoo:
        def read_partner_email(self, target: Any) -> str:
            return "testyyy44@gmail.com"

        def read_invoice_packet(self, ref: Any) -> dict[str, Any]:
            return {
                "move": {"id": 63, "name": "INV/2026/00011", "ref": "facture_newayit",
                         "state": "posted", "invoice_date": "2026-09-13", "amount_total": 450.0,
                         "currency_id": [2, "EUR"], "partner_id": [107, "newayit"]},
                "partner": {"name": "newayit", "email": "testyyy44@gmail.com"},
                "lines": [{"name": "Service", "quantity": 1, "price_unit": 450.0, "price_subtotal": 450.0}],
            }

    executor, http = _make_gmail(monkeypatch, FakeOdoo())
    packet = {
        "move": {"id": 63, "name": "INV/2026/00011", "ref": "facture_newayit", "state": "posted"},
    }
    get_result = action_result(
        "account.move.action_get",
        params={"invoice": "facture_newayit"},
        data={"ok": True, "response": packet},
    )
    log = [executor_result("accounting", [get_result])]
    ctx = _gmail_email_ctx(
        {
            "email.email_submission.set": {
                "fields": {
                    "recipients": "testyyy44@gmail.com",
                    "subject": "Votre facture facture_newayit",
                    "content": "Bonjour, votre facture en pièce jointe.",
                }
            }
        },
        log,
    )
    result = executor.execute(ctx)
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert http.sent, "a send was issued"

    import email as email_lib
    import base64 as b64
    msg = email_lib.message_from_string(b64.urlsafe_b64decode(http.sent[0]["raw"]).decode("utf-8"))
    assert "testyyy44@gmail.com" in (msg["To"] or "")
    pdf_parts = [part for part in msg.walk() if part.get_content_type() == "application/pdf"]
    assert pdf_parts, "a PDF attachment is present"
    assert pdf_parts[0].get_payload(decode=True).startswith(b"%PDF")


def test_gmail_retry_never_sends_the_same_email_twice(monkeypatch) -> None:
    """A retried turn must not re-deliver the mail (it used to send it 3x)."""
    executor, http = _make_gmail(monkeypatch, None)
    actions = {
        "email.email_submission.set": {
            "fields": {
                "recipients": ["testyyy44@gmail.com"],
                "subject": "final report",
                "content": "rapport en piece jointe",
            }
        },
        "email.blob.upload": {"fields": {"attachments": "rapport.pdf"}},
    }
    first = executor.execute(_gmail_email_ctx(actions, []))
    assert len(http.sent) == 1
    assert first["results"][0]["data"]["ok"] is True

    for _ in range(2):
        again = executor.execute(_gmail_email_ctx(actions, []))
        assert again["results"][0]["data"]["ok"] is True
        assert again["results"][0]["data"].get("duplicate_suppressed") is True

    assert len(http.sent) == 1, "the mail must be sent exactly once per turn"


def test_gmail_blob_upload_is_not_deferred(monkeypatch) -> None:
    """email.blob.upload has no Gmail endpoint: it must not be 'deferred'.

    A deferred action failed structural validation, which re-ran the whole
    domain (re-sending the mail) and finally reported a false failure.
    """
    executor, http = _make_gmail(monkeypatch, None)
    result = executor.execute(
        _gmail_email_ctx(
            {
                "email.email_submission.set": {
                    "fields": {
                        "recipients": ["testyyy44@gmail.com"],
                        "subject": "final report",
                        "content": "body",
                    }
                },
                "email.blob.upload": {"fields": {"attachments": "rapport.pdf"}},
            },
            [],
        )
    )
    blob = next(r for r in result["results"] if r["action"] == "email.blob.upload")
    assert not blob["data"].get("deferred"), "blob upload must not be deferred"
    assert blob["data"]["response"]["handled_by"] == "email.email_submission.set"


def test_gmail_resolves_attachment_names_from_turn_uploads(monkeypatch) -> None:
    """`attachment_names` (classifier output) must ship the real file bytes."""
    import base64 as b64
    import email as email_lib

    executor, http = _make_gmail(monkeypatch, None)
    content = b64.b64encode(b"%PDF-1.4 fake report").decode("ascii")
    ctx = ExecutionContext(
        domain="email_action",
        actions={
            "email.email_submission.set": {
                "fields": {
                    "recipients": ["testyyy44@gmail.com"],
                    "subject": "final report",
                    "content": "ci joint le rapport",
                    "attachment_names": "Blue White Modern Minimalist Research Project Presentation.pdf",
                }
            }
        },
        attachments=[
            {
                "filename": "Blue White Modern Minimalist Research Project Presentation.pdf",
                "content_type": "application/pdf",
                "content": content,
            }
        ],
        turn_id="turn-1",
    )
    result = executor.execute(ctx)
    assert result["results"][0]["data"]["ok"] is True
    msg = email_lib.message_from_string(b64.urlsafe_b64decode(http.sent[0]["raw"]).decode("utf-8"))
    parts = [p for p in msg.walk() if p.get_content_type() == "application/pdf"]
    assert parts, "the named PDF must be attached"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4 fake report"


def test_gmail_blob_upload_reports_what_travelled_with_the_mail(monkeypatch) -> None:
    """The upload ack must list the files the send actually attached.

    It used to read ctx.executor_log, which never contains the current domain's
    own results, so it always answered "attached: []" and the validator then
    told the user the attachment had not been sent.
    """
    import base64 as b64

    executor, _http = _make_gmail(monkeypatch, None)
    ctx = ExecutionContext(
        domain="email_action",
        actions={
            "email.email_submission.set": {
                "fields": {
                    "recipients": ["testyyy44@gmail.com"],
                    "subject": "final report",
                    "content": "ci joint le rapport",
                    "attachment_names": "rapport final.docx",
                }
            },
            "email.blob.upload": {"fields": {"attachments": "rapport final.docx"}},
        },
        attachments=[
            {"filename": "rapport final.docx", "content_b64": b64.b64encode(b"docx").decode()}
        ],
        turn_id="turn-ack",
    )
    result = executor.execute(ctx)
    ack = next(r for r in result["results"] if r["action"] == "email.blob.upload")
    response = ack["data"]["response"]
    assert response["attached"] == ["rapport final.docx"], "must report the attached file"
    assert response["missing"] == []


def test_gmail_blob_upload_reports_the_file_sent_on_the_first_attempt(monkeypatch) -> None:
    """The upload ack must name the file that just travelled with the mail.

    Reading ctx.executor_log cannot work (the domain's own results are not in
    the log while it executes): the ack said "attached: []", the validator read
    that as "the attachment was never sent" and the turn was reported failed.
    """
    executor, http = _make_gmail(monkeypatch, None)
    import base64 as _b64

    content = _b64.b64encode(b"%PDF-1.4 rapport").decode("ascii")
    ctx = ExecutionContext(
        domain="email_action",
        actions={
            "email.email_submission.set": {
                "fields": {
                    "recipients": ["testyyy44@gmail.com"],
                    "subject": "final report",
                    "content": "ci joint le rapport",
                    "attachment_names": "rapport.pdf",
                }
            },
            # the classifier often puts the NAME under `attachments`
            "email.blob.upload": {"fields": {"attachments": "rapport.pdf"}},
        },
        attachments=[{"filename": "rapport.pdf", "content_type": "application/pdf", "content": content}],
        turn_id="turn-blob-1",
    )
    result = executor.execute(ctx)
    assert result["results"][0]["data"]["ok"] is True
    blob = next(r for r in result["results"] if r["action"] == "email.blob.upload")
    payload = blob["data"]["response"]
    assert payload["requested"] == ["rapport.pdf"]
    assert payload["attached"] == ["rapport.pdf"], "ack must report the attached file"
    assert payload["missing"] == []


def test_gmail_missing_attachment_is_reported_not_claimed(monkeypatch) -> None:
    """An unresolvable file must be reported as missing, never as sent."""
    executor, _ = _make_gmail(monkeypatch, None)
    result = executor.execute(
        ExecutionContext(
            domain="email_action",
            actions={
                "email.email_submission.set": {
                    "fields": {
                        "recipients": ["testyyy44@gmail.com"],
                        "subject": "final report",
                        "content": "ci joint",
                        "attachment_names": "fichier-inexistant-xyz.pdf",
                    }
                }
            },
            attachments=[],
            turn_id="turn-missing-1",
        )
    )
    response = result["results"][0]["data"]["response"]
    assert response["missing_attachments"] == ["fichier-inexistant-xyz.pdf"]
    assert "attachment_names" not in response


def test_gmail_same_request_in_a_new_turn_sends_again(monkeypatch) -> None:
    """Identical text in a LATER turn must be delivered again.

    The guard used to fall back to `conversation_id|intent` (the graph thread
    is the conversation), so every repeat of the same request was reported as
    "sent" while Gmail was never called.
    """
    executor, http = _make_gmail(monkeypatch, None)
    actions = {
        "email.email_submission.set": {
            "fields": {
                "recipients": ["testyyy44@gmail.com"],
                "subject": "final report",
                "content": "rapport",
            }
        }
    }
    def turn(turn_id: str):
        return ExecutionContext(
            domain="email_action",
            actions=actions,
            conversation_id="conv-1",
            intent="envoie le rapport",
            turn_id=turn_id,
        )

    executor.execute(turn("turn-1"))
    executor.execute(turn("turn-2"))
    assert len(http.sent) == 2, "a new turn must send the mail again"


def test_gmail_without_turn_id_suppression_expires(monkeypatch) -> None:
    """Safety net: without a turn id the suppression must expire quickly."""
    import server.execution.executors.gmail as gmail_mod

    executor, http = _make_gmail(monkeypatch, None)
    actions = {
        "email.email_submission.set": {
            "fields": {
                "recipients": ["testyyy44@gmail.com"],
                "subject": "final report",
                "content": "rapport",
            }
        }
    }
    def turn():
        return ExecutionContext(
            domain="email_action",
            actions=actions,
            conversation_id="conv-1",
            intent="envoie le rapport",
        )

    executor.execute(turn())
    assert len(http.sent) == 1
    monkeypatch.setattr(gmail_mod, "_SIDE_EFFECT_TTL_SECONDS", 0.0)
    executor.execute(turn())
    assert len(http.sent) == 2, "expired suppression must send again"


def test_gmail_filename_string_under_attachments_is_resolved(monkeypatch) -> None:
    """`attachments: "rapport.pdf"` (a NAME) must not be split into characters.

    list("rapport.pdf") produced one empty MIME part per letter, so Gmail got a
    mail whose attachment was unusable (or dropped) while Viito said "sent".
    """
    import base64 as _b64
    import email as email_lib

    executor, http = _make_gmail(monkeypatch, None)
    content = _b64.b64encode(b"%PDF-1.4 rapport").decode("ascii")
    result = executor.execute(
        ExecutionContext(
            domain="email_action",
            actions={
                "email.email_submission.set": {
                    "fields": {
                        "recipients": ["testyyy44@gmail.com"],
                        "subject": "final report",
                        "content": "ci joint",
                        "attachments": "rapport.pdf",
                    }
                }
            },
            attachments=[{"filename": "rapport.pdf", "content_type": "application/pdf", "content": content}],
            turn_id="turn-str-att",
        )
    )
    response = result["results"][0]["data"]["response"]
    assert response["attachment_names"] == ["rapport.pdf"]
    assert "missing_attachments" not in response
    msg = email_lib.message_from_string(
        _b64.urlsafe_b64decode(http.sent[0]["raw"]).decode("utf-8")
    )
    parts = [p for p in msg.walk() if p.get_content_type() == "application/pdf"]
    assert len(parts) == 1, "exactly one PDF part, not one per character"
    assert parts[0].get_filename() == "rapport.pdf"


def test_gmail_attachment_dict_without_content_is_not_sent_empty(monkeypatch) -> None:
    """A dict without bytes is a reference, not an attachment to ship empty."""
    import base64 as _b64
    import email as email_lib

    executor, http = _make_gmail(monkeypatch, None)
    content = _b64.b64encode(b"%PDF-1.4 rapport").decode("ascii")
    executor.execute(
        ExecutionContext(
            domain="email_action",
            actions={
                "email.email_submission.set": {
                    "fields": {
                        "recipients": ["testyyy44@gmail.com"],
                        "subject": "final report",
                        "content": "ci joint",
                        "attachments": [{"filename": "rapport.pdf"}],
                    }
                }
            },
            attachments=[{"filename": "rapport.pdf", "content_type": "application/pdf", "content": content}],
            turn_id="turn-empty-att",
        )
    )
    msg = email_lib.message_from_string(
        _b64.urlsafe_b64decode(http.sent[0]["raw"]).decode("utf-8")
    )
    for part in msg.walk():
        assert part.get_payload(decode=True) != b"", "no empty attachment part"


def test_gmail_file_named_only_in_the_request_is_attached(monkeypatch) -> None:
    """When the classifier drops the file, the request text still names it."""
    import base64 as _b64

    executor, http = _make_gmail(monkeypatch, None)
    content = _b64.b64encode(b"%PDF-1.4 rapport").decode("ascii")
    result = executor.execute(
        ExecutionContext(
            domain="email_action",
            actions={
                "email.email_submission.set": {
                    "fields": {
                        "recipients": ["testyyy44@gmail.com"],
                        "subject": "final report",
                        "content": "ci joint",
                    }
                }
            },
            intent="envoie le mail avec fichier3.docx en piece jointe",
            attachments=[{"filename": "fichier3.docx", "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "content": content}],
            turn_id="turn-intent-file",
        )
    )
    assert result["results"][0]["data"]["response"]["attachment_names"] == ["fichier3.docx"]


def test_gmail_email_address_is_not_treated_as_a_file(monkeypatch) -> None:
    executor, _ = _make_gmail(monkeypatch, None)
    result = executor.execute(
        ExecutionContext(
            domain="email_action",
            actions={
                "email.email_submission.set": {
                    "fields": {
                        "recipients": ["testyyy44@gmail.com"],
                        "subject": "final report",
                        "content": "bonjour",
                    }
                }
            },
            intent="envoie un mail a testyyy44@gmail.com",
            attachments=[],
            turn_id="turn-no-file",
        )
    )
    response = result["results"][0]["data"]["response"]
    assert "missing_attachments" not in response
    assert "attachment_names" not in response


def test_gmail_non_ascii_attachment_filename_survives(monkeypatch) -> None:
    """Accented file names must reach Gmail with a parseable filename.

    Hand-writing `attachment; filename="...données.docx"` made Python
    RFC 2047-encode the whole header, which Gmail parsed as an attachment with
    no name at all: the bytes arrived, the file did not.
    """
    import base64 as _b64
    import email as email_lib

    executor, http = _make_gmail(monkeypatch, None)
    content = _b64.b64encode(b"%PDF-1.4 rapport accentue").decode("ascii")
    executor.execute(
        ExecutionContext(
            domain="email_action",
            actions={
                "email.email_submission.set": {
                    "fields": {
                        "recipients": ["testyyy44@gmail.com"],
                        "subject": "rapport",
                        "content": "ci joint",
                        "attachments": "Compte rendu tp3-administration base de donnees.docx",
                    }
                }
            },
            attachments=[
                {
                    "filename": "Compte rendu tp3-administration base de donnees.docx",
                    "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "content": content,
                }
            ],
            turn_id="turn-accents",
        )
    )
    msg = email_lib.message_from_string(
        _b64.urlsafe_b64decode(http.sent[0]["raw"]).decode("utf-8")
    )
    parts = [p for p in msg.walk() if not p.get_content_maintype() == "multipart" and p.get_content_type() != "text/plain"]
    assert len(parts) == 1
    disposition = str(parts[0]["Content-Disposition"])
    assert disposition.startswith("attachment;"), "the token must not be encoded away"
    assert "=?utf-8?" not in disposition, "no RFC 2047 encoding of the whole header"
    # Classic parameter (ASCII-safe) + RFC 2231 extended one (exact name).
    assert 'filename="Compte rendu tp3-administration base de donnees.docx"' in disposition
    assert "filename*=utf-8''" in disposition
    assert "donnees.docx" in disposition


def test_gmail_send_drops_fragment_recipients_keeps_valid(monkeypatch) -> None:
    executor, http = _make_gmail(monkeypatch, None)
    result = executor.execute(
        _gmail_email_ctx(
            {
                "email.email_submission.set": {
                    "fields": {"recipients": ["testyyy44@gmail.com", "telecharge moi le PDF"],
                               "subject": "hi", "content": "body"}
                }
            },
            [],
        )
    )
    assert result["results"][0]["data"]["ok"] is True

    import email as email_lib
    import base64 as b64
    msg = email_lib.message_from_string(b64.urlsafe_b64decode(http.sent[0]["raw"]).decode("utf-8"))
    assert "testyyy44@gmail.com" in (msg["To"] or "")
    assert "telecharge" not in (msg["To"] or "")

    import email as email_lib
    import base64 as b64
    msg = email_lib.message_from_string(b64.urlsafe_b64decode(http.sent[0]["raw"]).decode("utf-8"))
    assert "testyyy44@gmail.com" in (msg["To"] or "")
    assert "telecharge" not in (msg["To"] or "")


def test_gmail_send_no_recipient_raises_clear_error(monkeypatch) -> None:
    executor, http = _make_gmail(monkeypatch, None)
    result = executor.execute(
        _gmail_email_ctx(
            {
                "email.email_submission.set": {
                    "fields": {"recipients": ["telecharge", "envoie la par mail a l'adress email de la société"],
                               "subject": "hi", "content": "body"}
                }
            },
            [],
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "destinataire" in entry["data"]["error"]


# ---------------------------------------------------------------------------
# OnlyOffice executor (offline: fake HTTP).
# Live verification (container): converted a hosted docx -> real PDF (100%,
# endConvert true), version command returned 9.4.0.129; info/forcesave return
# honest errors while no editing session is open; wopi/docbuilder deferred.
# ---------------------------------------------------------------------------


class FakeOnlyOfficeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def post(self, path: str, json: Any = None, **_kw: Any) -> Any:
        self.calls.append((path, str(json), json))
        if "ConvertService" in path:
            return {
                "fileUrl": "http://localhost:8093/cache/out/output.pdf",
                "fileType": "pdf",
                "percent": 100,
                "endConvert": True,
            }
        body = json or {}
        command = body.get("c")
        if command == "version":
            return {"error": 0, "version": "9.4.0.129"}
        return {"error": 1}


def _make_onlyoffice() -> tuple[OnlyOfficeExecutor, FakeOnlyOfficeHttp]:
    from server.execution.executors.onlyoffice import OnlyOfficeExecutor

    fake = FakeOnlyOfficeHttp()
    executor = OnlyOfficeExecutor.__new__(OnlyOfficeExecutor)
    executor.base_url = "http://localhost:8093"
    executor.secret = "test-secret"
    executor._http = fake  # type: ignore[assignment]
    return executor, fake


def _office_ctx(actions: dict[str, Any]) -> ExecutionContext:
    return ExecutionContext(domain="office", actions=actions)


def test_onlyoffice_convert(monkeypatch) -> None:
    import urllib.request
    from server.webapp import docbuilder as docbuilder_mod

    class FakeResp:
        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def read(self) -> bytes:
            return b"%PDF-1.4 fake"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResp())
    monkeypatch.setattr(docbuilder_mod._file_storage, "save_file", lambda path, content: None)
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_ctx(
            {"office.converter.convert": {"fields": {"document": "http://x/doc.docx", "target_format": "pdf"}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["end_convert"] is True
    assert entry["data"]["response"]["type"] == "pdf"
    assert entry["data"]["response"]["converted_url"].startswith("http")


def test_onlyoffice_version_command() -> None:
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_ctx({"office.command.version": {"fields": {}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["version"] == "9.4.0.129"


def test_onlyoffice_convert_without_url_is_honest_error() -> None:
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_ctx({"office.converter.convert": {"fields": {"title": "Rapport"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "URL" in entry["data"]["error"]


def test_onlyoffice_wopi_is_deferred() -> None:
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_ctx({"office.wopi.get_file": {"fields": {"document": "Rapport"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["deferred"] is True


def test_onlyoffice_create_routes_to_docbuilder(monkeypatch) -> None:
    from server.webapp import docbuilder as docbuilder_mod

    seen: dict[str, Any] = {}

    def fake_create(doc_type: str, filename: str, content_text: str = "", folder: str | None = None) -> dict[str, Any]:
        seen.update(doc_type=doc_type, filename=filename, content=content_text, folder=folder)
        rel_path = f"{folder}/{filename}" if folder else filename
        return {"success": True, "file_path": rel_path, "content": "x"}

    monkeypatch.setattr(docbuilder_mod, "create_document", fake_create)
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_ctx(
            {"office.editor.create": {"fields": {"name": "test.docx", "folder": "test", "content": "test creation fichier"}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert seen["doc_type"] == "word"
    assert seen["filename"] == "test.docx"
    assert seen["content"] == "test creation fichier"
    assert seen["folder"] == "test"
    assert entry["data"]["response"]["file"] == "test/test.docx"


def test_onlyoffice_create_seeds_content_from_prior_read_results(monkeypatch) -> None:
    """\"ecris mes disponibilites dans un fichier\" creates the document with no
    content in the fields: the data lives in the previous domain's read result,
    so the file must not come out empty."""
    from server.webapp import docbuilder as docbuilder_mod

    seen: dict[str, Any] = {}

    def fake_create(doc_type: str, filename: str, content_text: str = "", folder: str | None = None) -> dict[str, Any]:
        seen.update(content=content_text, filename=filename)
        return {"success": True, "file_path": f"{folder}/{filename}", "content": "x"}

    monkeypatch.setattr(docbuilder_mod, "create_document", fake_create)
    executor, _ = _make_onlyoffice()
    ctx = ExecutionContext(
        domain="office",
        actions={"office.editor.create": {"fields": {"name": "dispo.docx", "folder": "dispo"}}},
        executor_log=[
            {
                "domain": "calendaring",
                "results": [
                    {
                        "action": "event.free_busy",
                        "data": {
                            "ok": True,
                            "response": {
                                "free": False,
                                "busy_events": [
                                    {
                                        "event": "techx",
                                        "start": "2026-09-28 10:00:00",
                                        "stop": "2026-09-28 11:00:00",
                                    }
                                ],
                            },
                        },
                    }
                ],
            }
        ],
    )
    entry = executor.execute(ctx)["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["content_seeded"] is True
    assert "techx" in seen["content"]
    assert "lundi 28 septembre 2026" in seen["content"]
    assert "10:00" in seen["content"]


def test_file_put_without_content_is_a_noop_when_the_file_already_exists() -> None:
    """office.editor.create already wrote the file this turn: a contentless
    file.put must not fail, otherwise 3 structural retries kill the domains
    still queued (the email never ran)."""
    executor = FileExecutor()
    ctx = ExecutionContext(
        domain="file_management",
        actions={"file.put": {"fields": {"file_name": "disponibilite.docx"}}},
        executor_log=[
            {
                "domain": "office",
                "results": [
                    {
                        "action": "office.editor.create",
                        "data": {"ok": True, "response": {"file": "dispo2/disponibilite.docx"}},
                    }
                ],
            }
        ],
    )
    entry = executor.execute(ctx)["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["already_exists"] is True
    assert entry["data"]["response"]["path"] == "dispo2/disponibilite.docx"


def test_file_put_without_content_still_fails_when_nothing_was_created() -> None:
    executor = FileExecutor()
    ctx = ExecutionContext(
        domain="file_management",
        actions={"file.put": {"fields": {"file_name": "jamais_cree_zzz.docx"}}},
    )
    entry = executor.execute(ctx)["results"][0]
    assert entry["data"]["ok"] is False
    assert "no content provided" in entry["data"]["error"]


def test_onlyoffice_create_xlsx_maps_to_cell_doc_type(monkeypatch) -> None:
    from server.webapp import docbuilder as docbuilder_mod

    seen: dict[str, Any] = {}

    def fake_create(doc_type: str, filename: str, content_text: str = "", folder: str | None = None) -> dict[str, Any]:
        seen.update(doc_type=doc_type, filename=filename, folder=folder)
        return {"success": True, "file_path": filename, "content": ""}

    monkeypatch.setattr(docbuilder_mod, "create_document", fake_create)
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_ctx({"office.editor.create": {"fields": {"name": "budget.xlsx", "folder": "finance"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert seen["doc_type"] == "cell"
    assert seen["folder"] == "finance"


def test_onlyoffice_create_error_is_honest(monkeypatch) -> None:
    from server.webapp import docbuilder as docbuilder_mod

    monkeypatch.setattr(
        docbuilder_mod, "create_document",
        lambda *a, **k: {"success": False, "message": "boom"},
    )
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_ctx({"office.editor.create": {"fields": {"name": "test.docx"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "create failed: boom" in entry["data"]["error"]


def test_docbuilder_create_script_uses_real_formats() -> None:
    from server.webapp import docbuilder as docbuilder_mod

    assert docbuilder_mod._js_create_file("word").strip() == 'builder.CreateFile("docx");'
    assert docbuilder_mod._js_create_file("cell").strip() == 'builder.CreateFile("xlsx");'
    assert docbuilder_mod._js_create_file("slide").strip() == 'builder.CreateFile("pptx");'
    assert docbuilder_mod._js_save_file("word", "test.docx").strip() == 'builder.SaveFile("docx", "test.docx");'


def test_onlyoffice_convert_local_path_saves_output(monkeypatch) -> None:
    import urllib.request
    from server.webapp import docbuilder as docbuilder_mod

    saved: dict[str, Any] = {}

    class FakeResp:
        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def read(self) -> bytes:
            return b"%PDF-1.4 fake"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResp())
    monkeypatch.setattr(
        docbuilder_mod._file_storage, "save_file",
        lambda path, content: saved.update(path=path, size=len(content)),
    )
    executor, fake = _make_onlyoffice()
    result = executor.execute(
        _office_ctx(
            {"office.converter.convert": {"fields": {"document": "test.docx", "target_format": "pdf", "title": "test2.pdf"}}}
        )
    )
    entry = result["results"][0]
    # A local stored path must have been turned into a download URL (no "needs a
    # document URL" error) and the converted bytes saved under the requested title.
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["file"] == "test2.pdf"
    assert saved["path"] == "test2.pdf"
    assert saved["size"] == len(b"%PDF-1.4 fake")
    sent = [j for path, _, j in fake.calls if "ConvertService" in path][0]
    # The bare local name was resolved to its real stored path (may be in a
    # subfolder like test/test.docx) before being served to the converter.
    assert "onlyoffice/download/" in sent["url"]
    assert sent["url"].endswith(sent["url"].split("/onlyoffice/download/")[1])


# ---------------------------------------------------------------------------
# OnlyOffice add_content: general-domain handoff (write text, THEN insert it)
# ---------------------------------------------------------------------------
def _general_answer_record(text: str) -> dict[str, Any]:
    """An executor_log record shaped like a successful general redaction."""
    return {
        "domain": "general",
        "status": "success",
        "results": [
            {
                "action": "redaction",
                "params": {},
                "data": {"ok": True, "subtype": "redaction", "answer": text},
            }
        ],
    }


def _office_edit_ctx(actions: dict[str, Any], *, prior_log: list[dict[str, Any]] | None = None) -> ExecutionContext:
    return ExecutionContext(
        domain="office",
        actions=actions,
        attachments=[{"_viito_file_path": "rapport.docx"}],
        executor_log=prior_log or [],
    )


def test_onlyoffice_add_content_uses_general_handoff_text(monkeypatch) -> None:
    """A redaction produced by the general executor in this turn is inserted."""
    from server.webapp import docbuilder as docbuilder_mod

    paragraph = "En 2026, l'innovation technologique se caractérise par l'essor de l'IA générative..."
    seen: dict[str, Any] = {}

    def fake_edit_document(file_path, request, table=None, modify=None, insert=None):
        seen.update(file_path=file_path, insert=insert)
        return {"success": True, "modified": True, "message": "ok"}

    monkeypatch.setattr(docbuilder_mod, "edit_document", fake_edit_document)
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_edit_ctx(
            {"office.editor.add_content": {"fields": {}}},
            prior_log=[_general_answer_record(paragraph)],
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert seen["file_path"] == "rapport.docx"
    assert seen["insert"]["text"] == paragraph


def test_onlyoffice_add_content_ignores_own_prior_office_confirmations(monkeypatch) -> None:
    """Prior office edit-confirmation messages must never be re-inserted."""
    from server.webapp import docbuilder as docbuilder_mod

    office_confirmation = _office_edit_ctx({"office.editor.add_content": {"fields": {}}})
    prior_office_rec = {
        "domain": "office",
        "status": "success",
        "results": [
            {
                "action": "office.editor.add_content",
                "params": {},
                "data": {"ok": True, "response": "Document modifié via Document Builder: ajoute du contenu"},
            }
        ],
    }
    ctx = ExecutionContext(
        domain="office",
        actions=office_confirmation.actions,
        attachments=office_confirmation.attachments,
        executor_log=[prior_office_rec],
    )
    executor, _ = _make_onlyoffice()
    result = executor.execute(ctx)
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "no generated or explicit text" in entry["data"]["error"]


def test_onlyoffice_add_content_honest_error_when_no_text(monkeypatch) -> None:
    """Without explicit text or prior generated prose, add_content fails cleanly."""
    from server.webapp import docbuilder as docbuilder_mod

    monkeypatch.setattr(docbuilder_mod, "edit_document", lambda *a, **k: {"success": True, "modified": True, "message": "x"})
    executor, _ = _make_onlyoffice()
    result = executor.execute(_office_edit_ctx({"office.editor.add_content": {"fields": {"text": "du contenu"}}}))
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "no generated or explicit text" in entry["data"]["error"]


def test_onlyoffice_add_content_prefers_prior_general_over_fragment(monkeypatch) -> None:
    """The attribute-extracted fragment never wins over the general prose."""
    from server.webapp import docbuilder as docbuilder_mod

    paragraph = "En 2026, l'innovation technologique se caractérise par l'essor de l'IA générative..."
    seen: dict[str, Any] = {}

    def fake_edit_document(file_path, request, table=None, modify=None, insert=None):
        seen.update(file_path=file_path, insert=insert)
        return {"success": True, "modified": True, "message": "ok"}

    monkeypatch.setattr(docbuilder_mod, "edit_document", fake_edit_document)
    executor, _ = _make_onlyoffice()
    result = executor.execute(
        _office_edit_ctx(
            {"office.editor.add_content": {"fields": {"text": "l'innovation technologique 2026"}}},
            prior_log=[_general_answer_record(paragraph)],
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert seen["insert"]["text"] == paragraph


def test_is_generic_placeholder_detects_du_contenu() -> None:
    from server.execution.executors.onlyoffice import _is_generic_placeholder

    assert _is_generic_placeholder("du contenu")
    assert _is_generic_placeholder("du contenu\ndu contenu\ndu contenu")
    assert _is_generic_placeholder("le contenu")
    assert _is_generic_placeholder("le paragraphe")


def test_general_strip_context_removes_viito_file_dump() -> None:
    from server.execution.executors.general import _source_text, _strip_context

    clean = "ajouter une nouvelle paragraph qui parle de l'innovation technologique 2026"
    full = clean + "\n\n---\nFichier actuel (note.docx):\nLorem ipsum dolor sit amet..."
    assert _strip_context(full) == clean
    # _source_text falls back to the stripped instruction when no field supplies text.
    assert _source_text({"redaction": {"fields": {}}}, full) == clean



# ---------------------------------------------------------------------------
# Vikunja executor (offline: fake HTTP).
# Live verification (container): task create/read/update (PUT /projects/{id}/tasks,
# POST /tasks/{id}), comment create, label.add (label_id), assignee.add (user_id),
# project read/duplicate, bucket create, share.link all succeeded. Vikunja 2.5
# uses PUT-to-create / POST-to-update (classic Vikunja verbs are inverted).
# ---------------------------------------------------------------------------


def test_vikunja_task_create() -> None:
    executor, fake = _make_vikunja(
        {
            "/api/v1/projects?per_page=200": [{"id": 3, "title": "Viito Projet Test"}],
            "/api/v1/projects/3/tasks": {"id": 10, "title": "Tache X", "project_id": 3},
        }
    )
    result = executor.execute(
        _tasking_ctx(
            {"tasking.task.create": {"fields": {"task": "Tache X", "project": "Viito Projet Test", "priority": 2}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["id"] == "10"
    assert entry["tool_calls"] == ["vikunja:tasking.task.create"]
    assert ("PUT", "/api/v1/projects/3/tasks") in [(c[0], c[1]) for c in fake.calls]


def test_vikunja_due_date_parses_french_month_name() -> None:
    """'25 octobre' must land on the 25th of October (not September, which was
    the broken fallback to today's month when the French name wasn't readable)."""
    from datetime import date as _date

    from server.execution.executors.vikunja import _build_due_date

    assert _build_due_date({"due_day": "25", "due_month": "octobre", "due_year": "2026"}) == "2026-10-25T00:00:00Z"
    assert _build_due_date({"due_day": "25", "due_month": "10", "due_year": "2026"}) == "2026-10-25T00:00:00Z"
    assert _build_due_date({"due_day": "25", "due_month": "octobre"}) == f"{_date.today().year:04d}-10-25T00:00:00Z"


def test_vikunja_due_date_free_text_and_hour() -> None:
    from server.execution.executors.vikunja import _build_due_date

    assert _build_due_date({"due_date": "25 octobre 2026", "due_hour": "14"}) == "2026-10-25T14:00:00Z"
    assert _build_due_date({"due_date": "25/10/2026"}) == "2026-10-25T00:00:00Z"


def test_vikunja_task_update_marks_done() -> None:
    executor, _ = _make_vikunja(
        {
            "/api/v1/tasks": [{"id": 10, "title": "Tache X", "description": "d"}],
            "GET /api/v1/tasks/10": {"id": 10, "title": "Tache X", "description": "d", "done": False},
            "POST /api/v1/tasks/10": {"id": 10, "title": "Tache X", "done": True},
        }
    )
    result = executor.execute(
        _tasking_ctx({"tasking.task.update": {"fields": {"task": "Tache X", "done": "done"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["id"] == "10"
    assert entry["data"]["response"]["response"]["done"] is True


def test_vikunja_comment_and_label() -> None:
    executor, fake = _make_vikunja(
        {
            "/api/v1/tasks": [{"id": 10, "title": "Tache X"}],
            "/api/v1/tasks/10/comments": {"id": 5, "comment": "bonjour"},
            "/api/v1/labels?per_page=200": [{"id": 2, "title": "urgent"}],
            "/api/v1/tasks/10/labels": {"label_id": 2},
        }
    )
    result = executor.execute(
        _tasking_ctx(
            {
                "tasking.comment.create": {"fields": {"task": "Tache X", "content": "bonjour"}},
                "tasking.label.add": {"fields": {"task": "Tache X", "label": "urgent"}},
            }
        )
    )
    entries = {e["action"]: e for e in result["results"]}
    assert entries["tasking.comment.create"]["data"]["ok"] is True
    assert entries["tasking.comment.create"]["data"]["response"]["id"] == "5"
    assert entries["tasking.label.add"]["data"]["ok"] is True
    assert entries["tasking.label.add"]["data"]["response"]["label_id"] == "2"
    assert any(m == "PUT" and "/comments" in p for m, p, _ in fake.calls)


def test_vikunja_same_turn_create_then_reference_uses_just_created_project() -> None:
    # Two "DemoNL" projects exist (old id 8, new id 9). Create + then create a
    # task in "DemoNL" inside one turn must hit the just-created id 9, not the
    # older duplicate that the listing returns first.
    executor, fake = _make_vikunja(
        {
            "/api/v1/projects": {"id": 9, "title": "DemoNL", "parent_project_id": 0},
        }
    )
    result = executor.execute(
        _tasking_ctx(
            {
                "tasking.project.create": {"fields": {"title": "DemoNL"}},
                "tasking.task.create": {"fields": {"task": "Premiere tache", "project": "DemoNL"}},
            }
        )
    )
    entries = {e["action"]: e for e in result["results"]}
    assert entries["tasking.project.create"]["data"]["response"]["id"] == "9"
    created = [p for m, p, _ in fake.calls if m == "PUT" and "/tasks" in p]
    assert created == ["/api/v1/projects/9/tasks"], f"task must go to new project, got {created}"


def test_vikunja_task_create_before_project_still_resolves() -> None:
    """Even if dict order puts task.create before project.create, the task
    must still link to the newly-created project (not an older duplicate)."""
    executor, fake = _make_vikunja(
        {
            "/api/v1/projects": {"id": 9, "title": "DemoNL", "parent_project_id": 0},
            "/api/v1/projects/9/tasks": {"id": 10, "title": "Premiere tache", "project_id": 9},
        }
    )
    from collections import OrderedDict
    actions = OrderedDict([
        ("tasking.task.create", {"fields": {"task": "Premiere tache", "project": "DemoNL"}}),
        ("tasking.project.create", {"fields": {"title": "DemoNL"}}),
    ])
    result = executor.execute(_tasking_ctx(actions))
    entries = {e["action"]: e for e in result["results"]}
    assert entries["tasking.project.create"]["data"]["response"]["id"] == "9"
    created = [p for m, p, _ in fake.calls if m == "PUT" and "/tasks" in p]
    assert created == ["/api/v1/projects/9/tasks"], f"task must go to new project, got {created}"


def test_vikunja_task_links_to_project_created_in_same_turn_without_project_field() -> None:
    """Regression (report): 'ajoute un nouveau projet Mon dernier projet front et
    ajoute la tache initi' — the attribute extractor omits the project on
    task.create. The task must still link to the single project created in the
    same turn instead of failing or falling into the Inbox."""
    executor, fake = _make_vikunja(
        {
            "/api/v1/projects": {"id": 9, "title": "Mon dernier projet front", "parent_project_id": 0},
            "/api/v1/projects/9/tasks": {"id": 10, "title": "initi", "project_id": 9},
        }
    )
    result = executor.execute(
        _tasking_ctx(
            {
                "tasking.project.create": {"fields": {"title": "Mon dernier projet front"}},
                "tasking.task.create": {"fields": {"task": "initi", "priority": "moyenne"}},
            }
        )
    )
    entries = {e["action"]: e for e in result["results"]}
    assert entries["tasking.task.create"]["data"]["ok"] is True
    created = [p for m, p, _ in fake.calls if m == "PUT" and "/tasks" in p]
    assert created == ["/api/v1/projects/9/tasks"], f"task must go to new project, got {created}"


def test_vikunja_unknown_action_is_deferred() -> None:
    executor, _ = _make_vikunja({})
    result = executor.execute(
        _tasking_ctx({"tasking.webhook.create": {"fields": {"project": "Viito"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["deferred"] is True


def test_vikunja_login_retries_on_rate_limit(monkeypatch) -> None:
    """Vikunja /login is rate-limited (429); the executor must back off and retry."""
    class RateLimitedLoginHttp(FakeVikunjaHttp):
        def __init__(self, failures: int = 2) -> None:
            super().__init__({})
            self.failures = failures

        def post(self, path: str, json: Any = None, **_kw: Any) -> Any:
            self.calls.append(("POST", path, {"json": json}))
            if path.endswith("/login"):
                if self.failures > 0:
                    self.failures -= 1
                    raise ExecutorError("POST /api/v1/login -> HTTP 429: Too Many Requests")
                return {"token": "tok"}
            return self._lookup("POST", path)

    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    fake = RateLimitedLoginHttp(failures=2)
    executor = VikunjaExecutor.__new__(VikunjaExecutor)
    executor.base_url = "http://localhost:3456"
    executor.username = "viito-agent"
    executor.password = "test"
    executor._token = None
    executor._auth_lock = threading.Lock()
    executor._http = fake  # type: ignore[assignment]

    token = executor._login()
    assert token == "tok"
    assert len([c for c in fake.calls if c[0] == "POST" and c[1].endswith("/login")]) == 3
    assert slept == [1.5, 3.0]


def test_vikunja_login_gives_up_after_429_retries(monkeypatch) -> None:
    class Always429Http(FakeVikunjaHttp):
        def __init__(self) -> None:
            super().__init__({})

        def post(self, path: str, json: Any = None, **_kw: Any) -> Any:
            self.calls.append(("POST", path, {"json": json}))
            raise ExecutorError("POST /api/v1/login -> HTTP 429: Too Many Requests")

    class Still429(Always429Http):
        pass

    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = Still429()
    executor = VikunjaExecutor.__new__(VikunjaExecutor)
    executor.base_url = "http://localhost:3456"
    executor.username = "viito-agent"
    executor.password = "test"
    executor._token = None
    executor._auth_lock = threading.Lock()
    executor._http = fake  # type: ignore[assignment]

    with pytest.raises(ExecutorError, match="rate-limited"):
        executor._login()


class FakeVikunjaHttp:
    """Canned HTTP stand-in for the Vikunja client (request + login only)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _lookup(self, method: str, path: str) -> Any:
        exact = f"{method} {path}"
        if exact in self.responses:
            return self.responses[exact]
        matches = [k for k in self.responses if path.startswith(k)]
        if not matches:
            raise AssertionError(f"no canned response for {method} {path}")
        key = max(matches, key=len)
        return self.responses[key]

    def request(self, method: str, path: str, **kw: Any) -> Any:
        self.calls.append((method, path, kw))
        return self._lookup(method, path)

    def post(self, path: str, json: Any = None, **_kw: Any) -> Any:
        self.calls.append(("POST", path, {"json": json}))
        if path.endswith("/login"):
            return {"token": "tok"}
        return self._lookup("POST", path)

    def put(self, path: str, **kw: Any) -> Any:
        self.calls.append(("PUT", path, kw))
        return self._lookup("PUT", path)

    def get(self, path: str, **kw: Any) -> Any:
        self.calls.append(("GET", path, kw))
        return self._lookup("GET", path)

    def delete(self, path: str, **kw: Any) -> Any:
        self.calls.append(("DELETE", path, kw))
        return None

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _make_vikunja(responses: dict[str, Any]) -> tuple[VikunjaExecutor, FakeVikunjaHttp]:
    fake = FakeVikunjaHttp(responses)
    executor = VikunjaExecutor.__new__(VikunjaExecutor)
    executor.base_url = "http://localhost:3456"
    executor.username = "viito-agent"
    executor.password = "test"
    executor._token = "tok"
    executor._auth_lock = threading.Lock()
    executor._http = fake  # type: ignore[assignment]
    return executor, fake


def _tasking_ctx(actions: dict[str, Any]) -> ExecutionContext:
    return ExecutionContext(domain="tasking", actions=actions)


# ---------------------------------------------------------------------------
# RAG executor (offline: fake panel + fake qdrant HTTP).
# Live verification (containers): answer.grounded / search (vector) via the
# RAG panel's POST /api/chat/ask (real contexts on target medium), fulltext
# search over qdrant node payloads (10 delegation hits in UA-1), spaces/fulltext
# distinct-UA listing, status.batch/processing from /api/runs. The panel config
# GET exposes an OpenRouter API key -- the executor never echoes it.
# ---------------------------------------------------------------------------


def test_rag_answer_grounded() -> None:
    executor, _ = _make_rag(
        {
            "/api/chat/ask": {
                "answer": "Les cinq principes sont...",
                "model": "qwen/qwen3-14b",
                "contexts": ["Le texte du module"],
                "units_called": ["UA-1"],
                "tools_called": ["query_by_ua"],
                "iterations": 1,
            }
        }
    )
    result = executor.execute(
        _kb_ctx(
            {"kb.answer.grounded": {"fields": {"question": "Les 5 principes ?", "space": "reference"}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["answer"]
    assert entry["data"]["response"]["units_called"] == ["UA-1"]
    assert entry["tool_calls"] == ["activiitykdb:kb.answer.grounded"]


def test_rag_fulltext_filters_by_space() -> None:
    node_ua1 = json.dumps({"text": "Les principes de la delegation efficace"})
    node_ua9 = json.dumps({"text": "La boite a outils du manager"})
    executor, _ = _make_rag(
        {
            "/collections": {"result": {"collections": [{"name": "kb_test"}]}},
            "/collections/kb_test/points/scroll": {
                "result": {
                    "points": [
                        {"id": "p1", "payload": {"ua_id": "UA-1", "ua_title": "Delegation", "header": "Principes", "file_name": "f1.pdf", "page": 5, "_node_content": node_ua1}},
                        {"id": "p2", "payload": {"ua_id": "UA-9", "ua_title": "Manager", "file_name": "f2.pdf", "page": 1, "_node_content": node_ua9}},
                    ],
                    "next_page_offset": None,
                }
            },
        }
    )
    result = executor.execute(
        _kb_ctx({"kb.search.fulltext": {"fields": {"search_query": "delegation", "space": "UA-1"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["total"] == 1
    assert entry["data"]["response"]["hits"][0]["ua_id"] == "UA-1"
    assert entry["data"]["response"]["hits"][0]["file_name"] == "f1.pdf"


def test_rag_space_read_lists_units() -> None:
    node_a = json.dumps({"text": "x"})
    node_b = json.dumps({"text": "y"})
    executor, _ = _make_rag(
        {
            "/collections": {"result": {"collections": [{"name": "kb_test"}]}},
            "/collections/kb_test/points/scroll": {
                "result": {
                    "points": [
                        {"id": "p1", "payload": {"ua_id": "UA-3", "ua_title": "Motiver", "_node_content": node_a}},
                        {"id": "p2", "payload": {"ua_id": "UA-1", "ua_title": "Delegation", "_node_content": node_b}},
                    ],
                    "next_page_offset": None,
                }
            },
        }
    )
    result = executor.execute(_kb_ctx({"kb.space.read": {"fields": {}}}))
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    ids = [s["id"] for s in entry["data"]["response"]["spaces"]]
    assert ids == ["UA-1", "UA-3"]


def test_rag_ingest_is_deferred() -> None:
    executor, _ = _make_rag({})
    result = executor.execute(
        _kb_ctx({"kb.ingest.document": {"fields": {"space": "UA-1", "document_names": ["a.pdf"]}}})
    )
    entry = result["results"][0]
    assert entry["data"]["deferred"] is True


class FakeRagHttp:
    """Canned HTTP for both the panel (8787) and qdrant (6333) clients."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _lookup(self, method: str, path: str) -> Any:
        exact = f"{method} {path}"
        if exact in self.responses:
            return self.responses[exact]
        matches = [k for k in self.responses if path.startswith(k)]
        if not matches:
            raise AssertionError(f"no canned response for {method} {path}")
        key = max(matches, key=len)
        return self.responses[key]

    def request(self, method: str, path: str, **kw: Any) -> Any:
        self.calls.append((method, path, kw))
        return self._lookup(method, path)

    def post(self, path: str, json: Any = None, **_kw: Any) -> Any:
        self.calls.append(("POST", path, {"json": json}))
        if path == "/api/chat/ask":
            return self._lookup("POST", path)
        return self._lookup("POST", path)

    def get(self, path: str, **kw: Any) -> Any:
        self.calls.append(("GET", path, kw))
        return self._lookup("GET", path)

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _make_rag(responses: dict[str, Any]) -> tuple[object, FakeRagHttp]:
    from server.execution.executors.rag import RagExecutor

    fake = FakeRagHttp(responses)
    executor = RagExecutor.__new__(RagExecutor)
    executor.rag_url = "http://localhost:8787"
    executor.qdrant_url = "http://localhost:6333"
    executor.collection = "kb_test"
    executor._panel = fake  # type: ignore[assignment]
    executor._qdrant = fake  # type: ignore[assignment]
    return executor, fake


def _kb_ctx(actions: dict[str, Any]) -> ExecutionContext:
    return ExecutionContext(domain="knowledge_kb", actions=actions)


def test_attributes_prompt_injects_conversation_history() -> None:
    """Prior-turn context must reach the field-extraction prompt so anaphora
    like « ce fichier » (referring to a file created in an earlier turn) can be
    resolved to an explicit filename."""
    from server.agents.attributes_classifier import build_attributes_prompt

    without = build_attributes_prompt(
        "déplace ce fichier dans le dossier test", "office", ["office.editor.move_file"]
    )
    with_hist = build_attributes_prompt(
        "déplace ce fichier dans le dossier test",
        "office",
        ["office.editor.move_file"],
        history="Utilisateur : convertit le fichier test.docx en pdf et le nomme test2.pdf\nViito : fait.",
    )
    assert "Contexte de la conversation précédente" not in without
    assert "Contexte de la conversation précédente" in with_hist
    assert "test2.pdf" in with_hist
    assert "ce fichier" in with_hist


# ---------------------------------------------------------------------------
# Telegram executor (offline: fake Telethon client).
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, mid, text, date, out=False, sender_id=None, media=None):
        self.id = mid
        self.message = text
        self.date = date
        self.out = out
        self.sender_id = sender_id
        self.media = media
        self.photo = None
        self.document = None
        self.voice = None
        self.sticker = None


class FakeEntity:
    def __init__(self, eid, title=None, first_name=None, last_name=None, username=None,
                 broadcast=None, megagroup=None, access_hash=0, phone=None):
        self.id = eid
        self.title = title
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.broadcast = broadcast
        self.megagroup = megagroup
        self.access_hash = access_hash
        self.phone = phone


class _SearchResult:
    def __init__(self, users=None, chats=None):
        self.users = users or []
        self.chats = chats or []


class FakeDialog:
    def __init__(self, dlg_id, name, is_channel=False, is_group=False, is_private=False,
                 entity=None, last_message=None, date=None):
        self.id = dlg_id
        self.name = name
        self.is_channel = is_channel
        self.is_group = is_group
        self.is_private = is_private
        self.entity = entity or FakeEntity(dlg_id, title=name)
        self.date = date
        self.message = FakeMessage(1, last_message, date) if last_message is not None else None


class FakeTelethonClient:
    """Async stand-in for an authorized Telethon client."""

    def __init__(self, dialogs=None, messages=None, entity_map=None, me=None,
                 authorized=True, password_needed=False, contacts=None, search_results=None):
        self.dialogs = dialogs or []
        self.messages = messages or {}
        self.entity_map = entity_map or {}
        self.me = me or FakeEntity(1, first_name="Tayssir", last_name="Andolssi", username="tayssir_viito")
        self.authorized = authorized
        self.password_needed = password_needed
        self.contacts = contacts or []
        self.search_results = search_results or []
        self.connected = False
        self.sent = []
        self.code_requests = []
        self.last_code = None
        self.last_password = None
        self.phone_code_hash = "hash123"
        self.raw_calls = []
        self.edits = []
        self.deleted_messages = []
        self.read_acks = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.me

    async def send_code_request(self, phone):
        self.code_requests.append(phone)
        return json.loads(json.dumps({"phone_code_hash": self.phone_code_hash}))

    async def sign_in(self, **kwargs):
        if kwargs.get("password"):
            self.last_password = kwargs["password"]
            self.authorized = True
            return self.me
        self.last_code = kwargs.get("code")
        if self.password_needed:
            from telethon.errors import SessionPasswordNeededError

            raise SessionPasswordNeededError(request=None)
        self.authorized = True
        return self.me

    async def log_out(self):
        self.authorized = False
        return True

    async def get_dialogs(self, limit=None):
        return self.dialogs

    async def get_entity(self, value):
        if value in self.entity_map:
            return self.entity_map[value]
        for contact in self.contacts or []:
            if getattr(contact, "id", None) == value or str(getattr(contact, "id", "")) == value:
                return contact
        for entity in self.entity_map.values():
            if getattr(entity, "id", None) == value or str(getattr(entity, "id", "")) == value:
                return entity
        raise ValueError(f"Entity not found: {value}")

    async def get_contacts(self):
        return self.contacts

    async def get_input_entity(self, value):
        if isinstance(value, FakeEntity):
            return value
        if value in self.entity_map:
            return self.entity_map[value]
        return value

    async def get_messages(self, entity, limit=None):
        return self.messages.get(entity, self.messages.get(str(entity), []))

    async def send_message(self, entity, text):
        self.sent.append((entity, text))
        return FakeMessage(mid=len(self.sent), text=text, date=dt(2024, 1, 1, 13, 0), out=True)

    async def edit_message(self, entity, mid, text):
        self.edits.append((entity, mid, text))
        return FakeMessage(mid=mid, text=text, date=dt(2024, 1, 1, 14, 0), out=True)

    async def delete_messages(self, entity, ids):
        self.deleted_messages.append((entity, ids))
        return True

    async def send_read_acknowledge(self, entity):
        self.read_acks.append(entity)
        return True

    async def kick_participant(self, entity, user):
        self.raw_calls.append(("kick", entity, user))
        return True

    async def __call__(self, request):
        name = type(request).__name__
        if name == "CreateChannelRequest":
            self.raw_calls.append((name, request.title, getattr(request, "about", ""),
                                   bool(getattr(request, "megagroup", False))))
            created = FakeEntity(eid=1001, title=request.title,
                                 broadcast=not getattr(request, "megagroup", False),
                                 megagroup=bool(getattr(request, "megagroup", False)))
            self.entity_map[created.id] = created
            self.entity_map[str(created.id)] = created
            return created
        if name == "SearchRequest":
            self.raw_calls.append((name, request.q, getattr(request, "limit", None)))
            contacts = {getattr(c, "id", None): c for c in (self.contacts or [])}
            users = [c for c in self.search_results if c not in contacts]
            return _SearchResult(users=users)
        if name == "AddContactRequest":
            self.raw_calls.append((name, getattr(request, "id", None),
                                   getattr(request, "first_name", ""),
                                   getattr(request, "last_name", ""),
                                   getattr(request, "phone", "")))
            return True
        self.raw_calls.append((name, getattr(request, "peer", None),
                               getattr(request, "channel", None),
                               getattr(request, "chat_id", None)))
        return True


def _make_telegram(client: FakeTelethonClient | None = None, creds=None) -> TelegramExecutor:
    import threading

    executor = TelegramExecutor.__new__(TelegramExecutor)
    executor._testing = True
    executor._lock = threading.Lock()
    executor._client = client
    executor._phone = (creds or {}).get("phone", "")
    executor._pending_code = False
    executor._code_hash = None
    executor._store = {"chats": {}, "channels": {}}
    executor._load_credentials = lambda: (creds if creds is not None else {})
    return executor


def _telegram_ctx(actions: dict[str, Any]) -> ExecutionContext:
    return ExecutionContext(domain="messaging", actions=actions)


def test_telegram_send_message_success() -> None:
    client = FakeTelethonClient()
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.message.send": {"fields": {"conversation": "123", "message_text": "hi"}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["tool_calls"] == ["telegram:messaging.message.send"]
    assert client.sent and client.sent[0] == (123, "hi")
    assert entry["data"]["response"]["chat_id"] == "123"


def test_telegram_send_message_resolves_by_username() -> None:
    client = FakeTelethonClient(
        entity_map={"@alice_viito": FakeEntity(999, first_name="Alice", username="alice_viito")}
    )
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.message.send": {"fields": {"conversation": "@alice_viito", "message_text": "bonjour"}}}
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    assert client.sent and int(getattr(client.sent[0][0], "id", client.sent[0][0])) == 999


def test_telegram_send_message_deferred_when_unknown_target() -> None:
    executor = _make_telegram(FakeTelethonClient())
    result = executor.execute(
        _telegram_ctx(
            {"messaging.message.send": {"fields": {"conversation": "Sellerie du Sud", "message_text": "hi"}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "aucune conversation connue" in entry["data"]["error"].lower()


def test_telegram_list_messages_returns_live_messages() -> None:
    client = FakeTelethonClient(
        messages={
            42: [
                FakeMessage(1, "reçu", dt(2024, 1, 1, 10, 0), out=False, sender_id=9),
                FakeMessage(2, "envoyé", dt(2024, 1, 1, 11, 0), out=True),
            ]
        }
    )
    executor = _make_telegram(client)
    executor._store["chats"]["42"] = {"chat_id": "42", "title": "Alice", "type": "private"}
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.messages": {"fields": {"conversation": "42"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    msgs = entry["data"]["response"]["messages"]
    assert [m["message_id"] for m in msgs] == [1, 2]
    assert msgs[0]["direction"] == "in" and msgs[0]["from_name"] == "Alice"
    assert msgs[1]["direction"] == "out" and msgs[1]["from_name"] == "Moi"


def test_telegram_refresh_ingests_dialogs() -> None:
    client = FakeTelethonClient(
        dialogs=[
            FakeDialog(42, "Alice", is_private=True, last_message="hello", date=dt(2024, 1, 1, 9, 0),
                       entity=FakeEntity(42, first_name="Alice", phone="+21653163345")),
            FakeDialog(-100123, "Mon Canal", is_channel=True, last_message="post", date=dt(2024, 1, 2, 8, 0)),
        ]
    )
    executor = _make_telegram(client)
    assert executor.refresh() == 2
    assert "42" in executor._store["chats"]
    assert "-100123" in executor._store["channels"]
    assert executor.list_conversations()[0]["last_message"] == "hello"
    assert executor.list_channels()[0]["chat_id"] == "-100123"
    private = executor.list_conversations()[0]
    assert private["phone"] == "+21653163345"
    assert private["type"] == "private"


def test_telegram_refresh_backfills_phone_from_odoo(monkeypatch) -> None:
    client = FakeTelethonClient(
        dialogs=[
            FakeDialog(42, "Alice", is_private=True, last_message="hello", date=dt(2024, 1, 1, 9, 0)),
        ]
    )
    executor = _make_telegram(client)
    monkeypatch.setattr(TelegramExecutor, "_odoo_phone", lambda self, text: "+21699112233" if text == "Alice" else None)
    executor.refresh()
    assert executor.list_conversations()[0]["phone"] == "+21699112233"


def test_telegram_unknown_action_is_deferred() -> None:
    executor = _make_telegram(FakeTelethonClient())
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.toggle_priority": {"fields": {"priority": "high"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["deferred"] is True


def test_telegram_status_not_configured() -> None:
    executor = _make_telegram(None, creds={})
    status = executor.get_status()
    assert status["connected"] is False
    assert status["configured"] is False
    assert status["awaiting_code"] is False


def test_telegram_status_connected() -> None:
    executor = _make_telegram(
        FakeTelethonClient(), creds={"api_id": "1", "api_hash": "a" * 32, "phone": "+33612345678"}
    )
    status = executor.get_status()
    assert status["connected"] is True
    assert status["configured"] is True
    assert status["me"]["username"] == "tayssir_viito"


def test_telegram_login_flow_code_then_2fa() -> None:
    client = FakeTelethonClient(password_needed=True)
    executor = _make_telegram(
        client, creds={"api_id": "1", "api_hash": "a" * 32, "phone": "+33612345678"}
    )
    executor.send_code()
    assert client.code_requests == ["+33612345678"]
    assert executor._pending_code is True
    with pytest.raises(TelegramNeedsPasswordError):
        executor.login("12345")
    status = executor.login("12345", password="secret")
    assert client.last_password == "secret"
    assert client.last_code == "12345"
    assert status["connected"] is True
    assert executor._pending_code is False


def test_telegram_send_message_resolves_by_phone() -> None:
    client = FakeTelethonClient(entity_map={"+33123456789": FakeEntity(555, first_name="Emma", last_name="Rousseau")})
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.message.send": {"fields": {"conversation": "+33123456789", "message_text": "salut"}}}
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    assert client.sent and int(getattr(client.sent[0][0], "id", client.sent[0][0])) == 555


def test_telegram_create_conversation_starts_private_chat_by_contact_name() -> None:
    client = FakeTelethonClient(contacts=[FakeEntity(555, first_name="Tayssir", last_name="Andolsi")])
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.create": {"fields": {"customer": "Tayssir Andolsi"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["chat_id"] == "555"
    assert entry["data"]["response"]["type"] == "private"
    assert "555" in executor._store["chats"]
    assert entry["data"]["response"]["message_sent"] is False


def test_telegram_create_conversation_sends_first_message() -> None:
    client = FakeTelethonClient(contacts=[FakeEntity(555, first_name="Tayssir", last_name="Andolsi")])
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.conversation.create": {"fields": {"customer": "Tayssir Andolsi", "message": "Bonjour"}}}
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    assert client.sent and int(getattr(client.sent[0][0], "id", client.sent[0][0])) == 555
    assert client.sent[0][1] == "Bonjour"


def test_telegram_message_update_edits_last_out_message() -> None:
    client = FakeTelethonClient(
        messages={42: [FakeMessage(5, "ancien texte", dt(2024, 1, 1, 12, 0), out=True)]}
    )
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.message.update": {"fields": {"conversation": "42", "message": "dernier message", "message_text": "nouveau texte"}}}
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    assert client.edits == [(42, 5, "nouveau texte")]


def test_telegram_message_delete_removes_message() -> None:
    client = FakeTelethonClient(
        messages={42: [FakeMessage(7, "a supprimer", dt(2024, 1, 1, 12, 0), out=True)]}
    )
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.message.delete": {"fields": {"conversation": "42", "message": "dernier message"}}}
        )
    )
    assert result["results"][0]["data"]["ok"] is True
    assert client.deleted_messages == [(42, [7])]


def test_telegram_conversation_delete_erases_private_history() -> None:
    client = FakeTelethonClient(entity_map={42: FakeEntity(42, first_name="Alice", broadcast=None)})
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.delete": {"fields": {"conversation": "42"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["action"] == "history_deleted"
    assert any(name == "DeleteHistoryRequest" for name, *_ in client.raw_calls)


def test_telegram_conversation_delete_leaves_channel() -> None:
    client = FakeTelethonClient(entity_map={100: FakeEntity(100, title="Canal", broadcast=True)})
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.delete": {"fields": {"conversation": "100"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["action"] == "left"
    assert any(name == "LeaveChannelRequest" for name, *_ in client.raw_calls)


def test_telegram_delete_unknown_entity_returns_failure_not_raise() -> None:
    """A raw Telegram error (unknown entity) must not crash the turn: it is
    recorded as an in-band per-action failure instead (no 502 upstream)."""
    client = FakeTelethonClient(entity_map={})  # get_entity raises ValueError
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.delete": {"fields": {"conversation": "42"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is False
    assert "telegram:" in (entry["data"]["error"] or "")


def test_telegram_mute_and_unmute_conversation() -> None:
    client = FakeTelethonClient()
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.mute": {"fields": {"conversation": "42"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["muted"] is True
    assert any(name == "UpdateNotifySettingsRequest" for name, *_ in client.raw_calls)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.unmute": {"fields": {"conversation": "42"}}})
    )
    assert result["results"][0]["data"]["response"]["muted"] is False


def test_telegram_create_group_with_members() -> None:
    client = FakeTelethonClient(entity_map={"@tayssir": FakeEntity(555, first_name="Tayssir", username="tayssir")})
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.group.create": {"fields": {"title": "Projet Alpha", "participants": "@tayssir", "message_text": "Salut"}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert any(name == "CreateChannelRequest" and megagroup is True for name, _t, _a, megagroup in client.raw_calls)
    assert any(name in ("AddChatUserRequest", "InviteToChannelRequest") for name, *_ in client.raw_calls)
    assert any(text == "Salut" for _e, text in client.sent)


def test_telegram_create_channel_publishes() -> None:
    client = FakeTelethonClient()
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx(
            {"messaging.channel.create": {"fields": {"title": "Actu", "about": "infos", "message_text": "Bonjour"}}}
        )
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["type"] == "channel"
    assert any(name == "CreateChannelRequest" and megagroup is False for name, _t, _a, megagroup in client.raw_calls)
    assert any(text == "Bonjour" for _e, text in client.sent)


def test_telegram_target_from_query_extracts_name() -> None:
    from server.execution.executors.telegram import _target_from_query

    assert _target_from_query("crée une nouvelle conversation avec Bob Martin") == "Bob Martin"
    assert _target_from_query("Crée une conversation telegram avec Betty Baker.") == "Betty Baker"
    assert _target_from_query("quand tu peux, il faudrait") is None


def test_telegram_create_conversation_falls_back_to_intent_target() -> None:
    """Empty attribute extraction must not block the action: the executor
    recovers the target from the raw query itself."""
    client = FakeTelethonClient(contacts=[FakeEntity(555, first_name="Bob", last_name="Martin")])
    executor = _make_telegram(client)
    ctx = ExecutionContext(
        domain="messaging",
        actions={"messaging.conversation.create": {"fields": {}}},
        intent="crée une nouvelle conversation avec Bob Martin",
    )
    result = executor.execute(ctx)
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["chat_id"] == "555"
    assert "555" in executor._store["chats"]


def test_telegram_conversation_found_via_global_search() -> None:
    """A name with no local contact still resolves when exactly one public
    Telegram profile matches."""
    client = FakeTelethonClient(
        search_results=[FakeEntity(555, first_name="Noah", last_name="Bennett")]
    )
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.create": {"fields": {"contact": "Noah Bennett"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["chat_id"] == "555"
    assert any(name == "SearchRequest" for name, *_ in client.raw_calls)


def test_telegram_conversation_disambiguated_via_odoo_phone(monkeypatch) -> None:
    """Many public profiles are ambiguous, but the Odoo phonebook points at the
    right user: the conversation starts AND the contact is added to Telegram."""
    from server.execution.executors.telegram import TelegramExecutor

    client = FakeTelethonClient(
        search_results=[FakeEntity(11, first_name="Noah", last_name="Bennett")] * 7,
        entity_map={"+21653163345": FakeEntity(555, first_name="Noah", last_name="Bennett")},
    )
    monkeypatch.setattr(TelegramExecutor, "_odoo_phone", lambda self, text: "21653163345")
    executor = _make_telegram(client)
    result = executor.execute(
        _telegram_ctx({"messaging.conversation.create": {"fields": {"contact": "Noah Bennett"}}})
    )
    entry = result["results"][0]
    assert entry["data"]["ok"] is True
    assert entry["data"]["response"]["chat_id"] == "555"
    assert entry["data"]["response"]["added_to_contacts"] is True
    assert any(name == "AddContactRequest" for name, *_ in client.raw_calls)