"""Odoo HTTP API — read-only list + detail views for the webapp.

Reuses the existing Odoo XML-RPC client (``OdooExecutor`` in
``server/execution/executors/odoo.py``) — no new client is written here.
These routes translate Odoo records into the JSON shapes the React pages
expect, and surface an explicit error rather than a silent empty list when
the Odoo instance is unreachable.

Endpoints
---------
GET /odoo/status              -> { ok, db, url } or 503
GET /odoo/{model}/list?search= -> { ok, model, records: [Row...] }
GET /odoo/{model}/{record_id}  -> { ok, model, record: {...}, open_url }
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.config import load_environment

load_environment()

router = APIRouter(tags=["odoo"])

_DEFAULTS = {
    "url": os.environ.get("ODOO_URL", "http://localhost:8069"),
    "db": os.environ.get("ODOO_DB", ""),
    "login": os.environ.get("ODOO_LOGIN", ""),
    "password": os.environ.get("ODOO_PASSWORD", ""),
}


def _client():
    """Build a shared OdooExecutor-backed accessor (single instance)."""
    from server.execution.executors.odoo import OdooExecutor

    return OdooExecutor()


def _list_model(app) -> Any:
    """Return the authenticated XML-RPC object proxy + uid."""
    models, uid = app._connect()
    return models, uid


#: domain key -> Odoo model + the fields wanted for a list row / detail.
_VIEWS: dict[str, dict[str, Any]] = {
    "contacts": {
        "model": "res.partner",
        "list": ["id", "name", "email", "company_type", "city", "country_id"],
        "detail": ["id", "name", "email", "phone", "company_type", "is_company",
                   "street", "street2", "city", "zip", "state_id", "country_id", "vat", "website"],
    },
    "crm": {
        "model": "crm.lead",
        "list": ["id", "name", "partner_name", "expected_revenue", "stage_id", "probability"],
        "detail": ["id", "name", "partner_name", "contact_name", "email_from", "phone",
                   "expected_revenue", "probability", "priority", "stage_id", "source_id",
                   "team_id", "date_deadline", "date_open", "tag_ids", "description"],
    },
    "hr-employees": {
        "model": "hr.employee",
        "list": ["id", "name", "work_email", "department_id", "job_title"],
        "detail": ["id", "name", "work_email", "work_phone", "mobile_phone", "department_id",
                   "parent_id", "job_title", "work_location_id", "birthday", "wage",
                   "marital", "work_permit_expiration_date"],
    },
    "hr-leaves": {
        "model": "hr.leave",
        "list": ["id", "employee_id", "holiday_status_id", "date_from", "date_to", "state"],
        "detail": ["id", "name", "employee_id", "holiday_status_id", "date_from", "date_to",
                   "request_date_from", "request_date_to", "number_of_days", "number_of_hours",
                   "state", "private_name", "notes"],
    },
    "accounting": {
        "model": "account.move",
        "list": ["id", "name", "partner_id", "amount_total", "state", "payment_state", "invoice_date"],
        "detail": ["id", "name", "ref", "move_type", "partner_id", "state", "payment_state",
                   "invoice_date", "invoice_date_due", "date", "amount_untaxed", "amount_tax",
                   "amount_total", "amount_residual", "currency_id", "narration"],
    },
    "sales": {
        "model": "sale.order",
        "list": ["id", "name", "partner_id", "amount_total", "state", "date_order"],
        "detail": ["id", "name", "partner_id", "state", "amount_untaxed", "amount_tax",
                   "amount_total", "date_order", "invoice_status", "validity_date",
                   "origin", "note", "payment_term_id", "client_order_ref", "user_id"],
    },
    "meetings": {
        "model": "calendar.event",
        "list": ["id", "name", "start", "stop", "location", "partner_ids", "user_id", "access_token",
                 "videocall_source", "videocall_location"],
        "detail": ["id", "name", "start", "stop", "allday", "duration", "location",
                   "description", "partner_ids", "user_id", "active", "privacy",
                   "recurrence_id", "recurrency", "access_token",
                   "videocall_source", "videocall_location"],
    },
}

#: human labels for relational fields / status enums (must match Odoo selection labels).
_STATUS_LABELS: dict[str, dict[str, str]] = {
    "state": {  # hr.leave / sale.order / account.move share "state"
        "draft": "Brouillon",
        "sent": "Envoyé",
        "done": "Terminé",
        "cancel": "Annulé",
        "confirm": "Confirmé",
        "sale": "Confirmé",
        "posted": "Validé",
        "validate": "Validé",
        "approved": "Approuvé",
        "refuse": "Refusé",
        "refused": "Refusé",
        "false": "Brouillon",
    },
    "payment_state": {
        "draft": "Brouillon",
        "posted": "Validé",
        "paid": "Payé",
        "partial": "Payé partiellement",
        "not_paid": "Non payé",
        "in_payment": "En paiement",
        "reversed": "Reversé",
        "blocked": "Bloqué",
    },
    "move_type": {
        "out_invoice": "Facture client",
        "out_refund": "Avoir client",
        "in_invoice": "Facture fournisseur",
        "in_refund": "Avoir fournisseur",
        "entry": "Écriture comptable",
    },
}

#: Odoo model -> preferred row title (a field name or a callable).
_TITLE_FIELDS: dict[str, Any] = {
    "res.partner": "name",
    "crm.lead": "name",
    "hr.employee": "name",
    "account.move": "name",
    "sale.order": "name",
    "calendar.event": "name",
    "hr.leave": lambda r: " · ".join(
        x for x in (_display(r.get("employee_id")), _display(r.get("holiday_status_id"))) if x
    )
    or _display(None),
}


def _display(value: Any) -> Any:
    """Odoo relational tuples/lists (id, name) -> name; False/None -> ''."""
    if value is False or value is None:
        return ""
    if isinstance(value, (tuple, list)) and len(value) == 2 and isinstance(value[0], int):
        return value[1]
    return value


_as_display = _display  # alias for backward compat


def _format_dt(value: Any) -> str:
    """Pretty-print an Odoo datetime (ISO 2026-09-13 10:00:00) in FR style."""
    if not value:
        return ""
    text = str(value).strip()
    if not text or text in ("false", "False"):
        return ""
    try:
        import datetime as _dt
        if "T" in text:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            parts = text.split(" ")
            parsed = _dt.datetime.fromisoformat(parts[0] + "T" + (parts[1] if len(parts) > 1 else "00:00:00"))
        day = parsed.strftime("%d/%m/%Y")
        if parsed.hour or parsed.minute:
            return f"{day} {parsed.strftime('%H:%M')}"
        return day
    except Exception:  # noqa: BLE001 - best-effort
        return text


def _summarize(model: str, row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("email_from", "email", "work_email", "partner_name"):
        if row.get(key):
            parts.append(str(row[key]))
            break
    for key in ("state", "payment_state"):
        val = row.get(key)
        if val:
            label = _STATUS_LABELS.get(key, {}).get(val, val)
            parts.append(label)
            break
    if model == "calendar.event":
        when = _format_dt(row.get("start"))
        if when:
            parts.append(when)
        loc = row.get("location")
        if loc:
            parts.append(str(loc))
    if model == "crm.lead" and row.get("expected_revenue"):
        parts.append(f"{row['expected_revenue']}")
    if model == "account.move" and row.get("amount_total"):
        parts.append(f"{row['amount_total']}")
    return " · ".join(p for p in parts if p)


def _enrich(model: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        # Title
        tf = _TITLE_FIELDS.get(model)
        if callable(tf):
            title = tf(row)
        elif isinstance(tf, str):
            title = _as_display(row.get(tf))
        else:
            title = _as_display(row.get("name")) or str(row.get("id") or "")
        rec: dict[str, Any] = {
            "id": row.get("id"),
            "title": title or str(row.get("id") or ""),
            "tool": "Odoo",
        }
        # Map enums to human labels
        for key in ("state", "payment_state", "move_type"):
            if key in row and row.get(key):
                rec[key] = _STATUS_LABELS.get(key, {}).get(row[key], row[key])
        rec["summary"] = _summarize(model, row)
        # Copy all remaining enriched fields
        for k, v in row.items():
            if k in ("id", "name", "_model"):
                continue
            if model == "calendar.event" and k in ("start", "stop"):
                rec[k] = _format_dt(v)
            elif k == "partner_ids" and isinstance(v, list):
                names: list[str] = []
                for x in v:
                    if isinstance(x, (list, tuple)):
                        n = x[1] if len(x) > 1 and x[1] else ""
                    else:
                        n = x
                    if n not in (None, ""):
                        names.append(str(n))
                rec[k] = ", ".join(names) if names else ""
            else:
                rec[k] = _as_display(v)
        if model == "calendar.event":
            rec["meeting_link"] = _meeting_link(row)
        out.append(rec)
    return out


def _meeting_link(row: dict[str, Any]) -> str:
    """Real videocall link (Odoo videocall_location) or the invite/web fallback."""
    url = _DEFAULTS["url"].rstrip("/")
    eid = row.get("id")
    token = str(row.get("access_token") or "").strip()
    real = str(row.get("videocall_location") or "").strip()
    if real:
        return real
    if token:
        return f"{url}/calendar/meeting/{eid}/invite?access_token={token}"
    return f"{url}/web#id={eid}&model=calendar.event&view_type=form"


def _partner_ids(row: dict[str, Any]) -> list[int]:
    """partner_ids as a flat list of ints (Odoo returns plain ids over
    XML-RPC, not [id, name] tuples)."""
    out: list[int] = []
    for p in row.get("partner_ids") or []:
        if isinstance(p, int):
            pid = p
        elif isinstance(p, (list, tuple)) and len(p) and isinstance(p[0], int):
            pid = p[0]
        else:
            continue
        if pid not in out:
            out.append(pid)
    return out


def _partner_info(app, ids: list[int]) -> dict[int, dict[str, str]]:
    """id -> {name, email} for the given res.partner records (best-effort)."""
    if not ids:
        return {}
    try:
        models, uid = app._connect()
        pw = os.environ.get("ODOO_PASSWORD", "")
        recs = models.execute_kw(
            _DEFAULTS["db"], uid, pw, "res.partner", "search_read",
            [[["id", "in", ids]]],
            {"fields": ["id", "name", "email"], "limit": 100},
        )
    except Exception:  # noqa: BLE001 - best-effort enrichment
        return {}
    return {
        r["id"]: {
            "name": str(r.get("name") or "").strip(),
            "email": str(r.get("email") or "").strip(),
        }
        for r in recs
    }


def _meetings_attendees(app, rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """record_id -> [{ id, name, email }] for calendar.event rows."""
    all_ids: list[int] = []
    for row in rows:
        for pid in _partner_ids(row):
            if pid not in all_ids:
                all_ids.append(pid)
    info = _partner_info(app, all_ids)
    return {
        row["id"]: [
            {"id": pid, "name": info.get(pid, {}).get("name", ""), "email": info.get(pid, {}).get("email", "")}
            for pid in _partner_ids(row)
        ]
        for row in rows
    }


def _read(app, ctx: dict[str, Any], ids_filter: list | None = None, limit: int = 200) -> list[dict[str, Any]]:
    models, uid = app._connect()
    pw = os.environ.get("ODOO_PASSWORD", "")
    fields = ctx["list"] if ids_filter is None else ctx["detail"]
    if ids_filter:
        domain = [("id", "in", ids_filter)]
    else:
        domain = []
    kwargs: dict[str, Any] = {"fields": fields, "limit": limit, "order": "id desc"}
    return models.execute_kw(_DEFAULTS["db"], uid, pw, ctx["model"], "search_read", [domain], kwargs)


@router.get("/odoo/status")
def odoo_status():
    missing = [k for k in ("db", "login", "password") if not _DEFAULTS[k]]
    if missing:
        raise HTTPException(status_code=503, detail="Odoo non configuré (env ODOO_DB/LOGIN/PASSWORD)")
    try:
        app = _client()
        app._connect()
        return {"ok": True, "db": _DEFAULTS["db"], "url": _DEFAULTS["url"]}
    except Exception as exc:  # noqa: BLE001 - surfaced as an explicit error state
        raise HTTPException(status_code=503, detail=f"Odoo injoignable : {exc}") from exc


@router.get("/odoo/{model_key}/list")
def odoo_list(model_key: str, search: str = ""):
    ctx = _VIEWS.get(model_key)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Modèle Odoo inconnu : {model_key}")
    try:
        app = _client()
        q = search.strip()
        rows = _read(app, ctx, limit=200)
        if q:
            # Backend-side search on the model's display field (name for all our models).
            models, uid = app._connect()
            pw = os.environ.get("ODOO_PASSWORD", "")
            matched = models.execute_kw(
                _DEFAULTS["db"], uid, pw, ctx["model"], "search_read",
                [[["name", "ilike", q]]],
                {"fields": ["id"], "limit": 200},
            )
            ids = [r["id"] for r in matched]
            rows = _read(app, ctx, ids_filter=ids, limit=200)
        for r in rows:
            r["_model"] = ctx["model"]
        records = _enrich(ctx["model"], rows)
        if ctx["model"] == "calendar.event":
            attendees_by_id = _meetings_attendees(app, rows)
            for rec in records:
                atts = attendees_by_id.get(rec["id"], [])
                rec["attendees"] = atts
                rec["partner_ids"] = ", ".join(a["name"] for a in atts if a["name"])
        return {"ok": True, "model": ctx["model"], "records": records}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Odoo injoignable : {exc}") from exc


@router.get("/odoo/{model_key}/{record_id}")
def odoo_detail(model_key: str, record_id: int):
    ctx = _VIEWS.get(model_key)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Modèle Odoo inconnu : {model_key}")
    try:
        app = _client()
        rows = _read(app, ctx, ids_filter=[record_id])
        if not rows:
            raise HTTPException(status_code=404, detail=f"Enregistrement {record_id} introuvable")
        row = rows[0]
        row["_model"] = ctx["model"]
        rec = _enrich(ctx["model"], [row])[0]
        if ctx["model"] == "calendar.event":
            atts = _meetings_attendees(app, [row]).get(row["id"], [])
            rec["attendees"] = atts
            rec["partner_ids"] = ", ".join(a["name"] for a in atts if a["name"])
            rec["meeting_link"] = _meeting_link(row)
        rec["open_url"] = f"{_DEFAULTS['url'].rstrip('/')}/odoo/{ctx['model']}/{record_id}"
        return {"ok": True, "model": ctx["model"], "record": rec}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Odoo injoignable : {exc}") from exc


def _get_gmail_executor():
    from server.execution.executors.registry import get_registry
    from server.execution.executors.gmail import GmailExecutor

    exec_ = get_registry().executor_for("email_action")
    if isinstance(exec_, GmailExecutor):
        return exec_
    raise HTTPException(status_code=503, detail="Gmail non disponible")


@router.post("/odoo/{model_key}/{record_id}/invite")
def odoo_meeting_invite(model_key: str, record_id: int):
    """Send the meeting invite (fixed subject + content, with the meeting
    link) to every attendee that has an email address."""
    ctx = _VIEWS.get(model_key)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Modèle Odoo inconnu : {model_key}")
    if ctx["model"] != "calendar.event":
        raise HTTPException(status_code=400, detail="L'invitation par email n'est disponible que pour les réunions")
    try:
        app = _client()
        rows = _read(app, ctx, ids_filter=[record_id])
        if not rows:
            raise HTTPException(status_code=404, detail=f"Réunion {record_id} introuvable")
        row = rows[0]
        attendees = _meetings_attendees(app, [row]).get(row["id"], [])
        emails = [a["email"] for a in attendees if a.get("email")]
        if not emails:
            raise HTTPException(status_code=400, detail="Aucun participant avec une adresse email sur cette réunion")
        gmail = _get_gmail_executor()
        name = str(row.get("name") or f"Réunion #{record_id}")
        link = _meeting_link(row)
        when = _format_dt(row.get("start"))
        subject = f"Invitation : {name}"
        lines = [
            "Bonjour,",
            "",
            f"Vous êtes invité(e) à la réunion « {name} ».",
        ]
        if when:
            lines.append(f"Date : {when}.")
        location = row.get("location")
        if location:
            lines.append(f"Lieu : {location}.")
        lines += [
            "",
            f"Lien de la réunion : {link}",
            "",
            "Cordialement,",
            "L'équipe Viito",
        ]
        sent = gmail._send_message(to=", ".join(emails), subject=subject, body="\r\n".join(lines))
        return {"ok": True, "meeting_link": link, "to": emails, **sent}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Envoi impossible : {exc}") from exc


def _normalize_dt(value: Any) -> str | None:
    """Accept 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM' or the FR display
    'DD/MM/YYYY HH:MM' and return an Odoo-ready 'YYYY-MM-DD HH:MM:SS'."""
    if not value:
        return None
    import datetime as _dt

    text = str(value).strip().replace("T", " ")
    parsed: _dt.datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            parsed = _dt.datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


class MeetingEditRequest(BaseModel):
    name: str | None = None
    location: str | None = None
    description: str | None = None
    start: str | None = None
    stop: str | None = None


@router.post("/odoo/{model_key}/{record_id}/edit")
def odoo_meeting_edit(model_key: str, record_id: int, payload: MeetingEditRequest):
    """Update editable fields of a calendar.event (name, location,
    description, start, stop). Only provided fields are written."""
    ctx = _VIEWS.get(model_key)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Modèle Odoo inconnu : {model_key}")
    if ctx["model"] != "calendar.event":
        raise HTTPException(status_code=400, detail="La modification n'est disponible que pour les réunions")
    vals: dict[str, Any] = {}
    if payload.name is not None:
        vals["name"] = str(payload.name).strip()
    if payload.location is not None:
        vals["location"] = str(payload.location).strip()
    if payload.description is not None:
        vals["description"] = str(payload.description)
    if payload.start is not None:
        start = _normalize_dt(payload.start)
        if not start:
            raise HTTPException(status_code=400, detail=f"Date de début invalide : {payload.start}")
        vals["start"] = start
        if payload.stop is not None:
            stop = _normalize_dt(payload.stop)
            if not stop:
                raise HTTPException(status_code=400, detail=f"Date de fin invalide : {payload.stop}")
            vals["stop"] = stop
    if not vals:
        raise HTTPException(status_code=400, detail="Rien à modifier")
    try:
        app = _client()
        rows = _read(app, ctx, ids_filter=[record_id])
        if not rows:
            raise HTTPException(status_code=404, detail=f"Réunion {record_id} introuvable")
        app._execute("calendar.event", "write", [[record_id], vals])
        return {"ok": True, "id": record_id, "updated": sorted(vals.keys())}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Modification impossible : {exc}") from exc