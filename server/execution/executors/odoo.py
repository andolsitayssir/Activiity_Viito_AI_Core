"""Odoo executor (XML-RPC) — serves crm, accounting, sales, hr, contacts.

Authenticates once against ``ODOO_URL`` (db/login/password from the environment)
and translates each ``{model}.{method}`` action into XML-RPC ``execute_kw``
calls. Standard ORM verbs (create/write/search_read/unlink) plus common
``action_*`` methods are handled; anything unmapped is deferred honestly.

Live-verified against the running Odoo container before commit.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import xmlrpc.client as xmlrpc

from server.execution.executors.base import (
    DomainExecutor,
    ExecutorError,
    ExecutionContext,
    action_result,
    deferred_action,
    executor_result,
)

#: action method (the token after the last ".") -> ORM method to call.
_STANDARD_METHODS = {"create", "write", "unlink", "copy"}
_SEARCH_METHODS = {"search_read", "search", "search_count", "name_search", "read_group"}

# Centrally drop the domain-bound model prefix problem: actions arrive as
# e.g. "sale.order.action_confirm" — model is everything before the last ".".
def _split_action(action: str) -> tuple[str, str]:
    model, _, method = action.rpartition(".")
    return model, method


#: Odoo fields expected as floats/monetary. Raw classifier values are often
#: strings like "85%" or "45000 EUR" — coerced before any RPC (the backend
#: would otherwise raise TypeError e.g. on ``probability >= 100``).
_NUMERIC_FIELDS = {
    "expected_revenue",
    "probability",
    "total_amount",
    "amount",
    "amount_total",
    "price_unit",
    "quantity",
    "unit_amount",
    "planned_hours",
    "day_rate",
    "percent",
}


#: Spoken aliases -> exact record names, per comodel (used for *_id resolution).
#: Lets "status: qualifié" land on the Odoo stage "Qualified", etc.
_NAME_ALIASES: dict[str, dict[str, str]] = {
    "crm.stage": {
        "qualifié": "Qualified",
        "qualifie": "Qualified",
        "qualified": "Qualified",
        "qualifier": "Qualified",
        "nouveau": "New",
        "nouvelle": "New",
        "new": "New",
        "proposition": "Proposition",
        "proposal": "Proposition",
        "devis": "Proposition",
        "gagné": "Won",
        "gagne": "Won",
        "won": "Won",
        "perdu": "Lost",
        "perdue": "Lost",
        "lost": "Lost",
    },
    "hr.recruitment.stage": {
        "nouveau": "New",
        "nouvelle": "New",
        "new": "New",
        "entretien": "Interview",
        "interview": "Interview",
        "embauché": "Contract Signed",
        "embauche": "Contract Signed",
        "contract signed": "Contract Signed",
    },
}


def _parse_number(value: Any) -> Any:
    """Parse a human numeric string to float (returns the input untouched if
    it is not numeric). Understands "85%", "45 000 €", "45000 EUR", "2,5"."""
    if value is None or isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value
    cleaned = re.sub(r"[^\d,.\-\s]", "", value).strip()
    if not cleaned:
        return value
    cleaned = cleaned.replace(" ", "").replace("%", "")
    if "," in cleaned and "." in cleaned:
        # digit grouping wins: the last separator is the decimal point.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return value


_FRENCH_MONTHS: dict[str, int] = {
    "janvier": 1, "jan": 1, "fevrier": 2, "février": 2, "fev": 2, "feb": 2,
    "mars": 3, "mar": 3, "avril": 4, "avr": 4, "mai": 5,
    "juin": 6, "juillet": 7, "juil": 7, "aout": 8, "août": 8,
    "septembre": 9, "sept": 9, "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11, "decembre": 12, "décembre": 12, "dec": 12,
}


def _parse_invoice_date(value: Any) -> str | None:
    """Best-effort parse of a date or French date-parts to ISO 'YYYY-MM-DD'."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    iso = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", text)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    dmy = re.fullmatch(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\s*", text)
    if dmy:
        day, month, year = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d}"
    dm = re.fullmatch(r"\s*(\d{1,2})\s+([a-zà-ÿ]+)\s*", text.lower())
    if dm:
        month = _FRENCH_MONTHS.get(dm.group(2))
        if month:
            return f"{date.today().year:04d}-{month:02d}-{int(dm.group(1)):02d}"
    return None


def _parse_time(value: Any) -> int | None:
    """Parse an hour/minute value to an int (0-23 / 0-59)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace("h", ":").replace("heures", ":")
    match = re.search(r"(\d{1,2})\s*[:h]\s*(\d{1,2})?", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2) or 0)
    m = re.search(r"(\d{1,2})", text)
    return int(m.group(1)) if m else None


def _event_datetime(params: dict[str, Any], prefix: str) -> str | None:
    """Build a wall-clock 'YYYY-MM-DD HH:MM:SS' from classifier date + time
    parts with the given prefix (start_/end_/from_/''). The value is meant in
    the Odoo user's timezone; callers convert it to UTC via ``_to_utc_naive``
    before writing to or comparing against Odoo's UTC-known date fields."""
    day = params.get(f"{prefix}day") or params.get(f"{prefix}day_of_month") or params.get("day")
    month = params.get(f"{prefix}month")
    year = params.get(f"{prefix}year")
    try:
        d = int(day) if day else None
        m = _FRENCH_MONTHS.get(str(month).strip().lower()) if month else None
    except Exception:  # noqa: BLE001
        d = None
        m = None
    if d is None or m is None:
        return None
    y = int(year) if year else date.today().year

    hour_val = params.get(f"{prefix}hour")
    minute_val = params.get(f"{prefix}minute")
    h, mi = 0, 0
    if hour_val is not None:
        hour_str = str(hour_val).strip().lower().replace("h", ":")
        if ":" in hour_str:
            parts = hour_str.split(":")
            h = int(parts[0] or 0) if parts[0].isdigit() else 0
            mi = int(parts[1] or 0) if len(parts) > 1 and parts[1].isdigit() else 0
        else:
            digits = "".join(ch for ch in hour_str if ch.isdigit())
            h = int(digits) if digits else 0
    if minute_val is not None:
        digits = "".join(ch for ch in str(minute_val) if ch.isdigit())
        if digits:
            mi = int(digits) % 60
    h = max(0, min(h, 23))
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    return f"{dt.isoformat()} {h:02d}:{mi:02d}:00"


def _add_hours(iso_dt: str, hours: int) -> str:
    """Return an Odoo datetime string with ``hours`` added."""
    try:
        import datetime as _dt
        parsed = _dt.datetime.fromisoformat(iso_dt.replace(" ", "T"))
        return (parsed + _dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - best-effort
        return iso_dt


def _to_utc_naive(local_dt: str | None, tz: str | None) -> str | None:
    """Convert a wall-clock 'YYYY-MM-DD HH:MM:SS' (meant in the Odoo user's
    timezone) to the UTC string Odoo stores/expects. Unknown or unresolvable
    timezones return the input unchanged."""
    if not local_dt or not tz:
        return local_dt
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        naive = _dt.datetime.fromisoformat(local_dt.replace(" ", "T"))
        aware = naive.replace(tzinfo=ZoneInfo(tz))
        return aware.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - best-effort tz conversion
        return local_dt


# Param name -> Odoo field name, per model. Only fields the router emits.
_FIELD_MAPS: dict[str, dict[str, str]] = {
    "res.partner": {
        "name": "name",
        "company": "name",  # company fiche: the company name IS the partner name
        "company_name": "name",
        "email": "email",
        "phone": "phone",
        "mobile": "mobile",
        "website": "website",
        "address": "street",  # free-text address -> street (multi-line -> street2)
        "street": "street",
        "street2": "street2",
        "city": "city",
        "zip": "zip",
        "country": "country_id",
    },
    "crm.lead": {
        "name": "name",  # the opportunity name itself
        "lead": "name",
        "company": "partner_name",  # free text company name, resolved below
        "contact": "contact_name",
        "email": "email_from",
        "phone": "phone",
        "revenue": "expected_revenue",
        "expected_revenue": "expected_revenue",
        "probability": "probability",
        "priority": "priority",
        "source": "source_id",
        "stage": "stage_id",
        "status": "stage_id",
        "team": "team_id",
        "assignee": "user_id",
        "reason": "crm_lost_reason_id",
    },
    "sale.order": {
        "customer": "partner_id",
        "currency": "currency_id",
        "price_unit": "order_line_price",  # kept as pseudo-field, see builder
    },
    "hr.employee": {
        "name": "name",
        "email": "work_email",
        "phone": "work_phone",
        "department": "department_id",
        "manager": "parent_id",
    },
    "hr.applicant": {
        "name": "partner_name",
        "email": "email_from",
        "phone": "phone",
        "recruitment_stage": "stage_id",
        "department": "department_id",
    },
    "hr.expense": {
        "name": "name",
        "amount": "total_amount",
        "date": "date",
    },
    "account.move": {
        "ref": "ref",
        "invoice_number": "ref",  # "facture nommée X" -> stored in the invoice reference
        "invoice_date": "invoice_date",
        "invoice_ref": "ref",
        "partner": "partner_id",
        "customer": "partner_id",  # "pour cette société" -> invoice partner
        "journal": "journal_id",
        "currency": "currency_id",
    },
    "calendar.event": {
        "title": "name",
        "name": "name",
        "date": "start",
        "start": "start",
        "location": "location",
        "description": "description",
        "attendees": "partner_ids",
        "calendar": "calendar_id",
        "user": "user_id",
        "recurrency": "recurrency",
    },
}


#: Temporal scope of a calendar listing, read from the user's wording.
_UPCOMING_RE = re.compile(
    r"(?:\b[àa]\s*venir|\bvenir\b|\bprochain|\bfutur|\b[àa]\s*partir\s+d|\bmaintenant\b|"
    r"\bprochainement\b|\bupcoming\b|\bnext\b)",
    re.IGNORECASE,
)
_PAST_RE = re.compile(
    r"(?:\bpass[ée]es?\b|\btermin[ée]es?\b|\bhistorique\b|\bpast\b|\bd[ée]j[àa]\s+vues?\b|"
    r"\barchiv[ée]es?\b|\boccurr[ée]s?\b)",
    re.IGNORECASE,
)


def _event_scope(intent: str) -> str:
    """'upcoming' / 'past' / '' from the intent ('les réunions à venir').

    Without this, "liste les réunions" returns the whole history and answers the
    wrong question: the user asked for what is coming next.
    """
    text = intent or ""
    if _UPCOMING_RE.search(text):
        return "upcoming"
    if _PAST_RE.search(text):
        return "past"
    return ""


def _relative_window(intent: str) -> tuple[str, str] | None:
    """Resolve a relative period ("cette semaine", "demain", "le mois prochain").

    Returns ('YYYY-MM-DD 00:00:00', 'YYYY-MM-DD 23:59:59') or None. Without it,
    "mes réunions de cette semaine" silently answers with the whole history.
    """
    text = (intent or "").lower()
    today = date.today()
    midnight = " 00:00:00"
    end_of_day = " 23:59:59"

    def _span(first: date, last: date) -> tuple[str, str]:
        return f"{first.isoformat()}{midnight}", f"{last.isoformat()}{end_of_day}"

    week_start = today - timedelta(days=today.weekday())
    if "semaine" in text:
        if "prochaine" in text:
            first = week_start + timedelta(days=7)
            return _span(first, first + timedelta(days=6))
        if "derni" in text:
            first = week_start - timedelta(days=7)
            return _span(first, first + timedelta(days=6))
        return _span(week_start, week_start + timedelta(days=6))
    if "mois" in text:
        if "prochain" in text:
            first = date(today.year + (today.month // 12), (today.month % 12) + 1, 1)
        elif "dernier" in text or "pass" in text:
            first = date(today.year - (1 if today.month == 1 else 0), 12 if today.month == 1 else today.month - 1, 1)
        else:
            first = today.replace(day=1)
        last = date(first.year + (1 if first.month == 12 else 0), 1 if first.month == 12 else first.month + 1, 1) - timedelta(days=1)
        return _span(first, last)
    if "demain" in text:
        target = today + timedelta(days=1)
        return _span(target, target)
    if "aujourd" in text or "ce soir" in text or "ce matin" in text:
        return _span(today, today)
    return None


class OdooExecutor(DomainExecutor):
    domains: tuple[str, ...] = (
        "crm", "accounting", "sales", "hr", "contacts", "calendaring",
    )
    backend = "odoo"

    def __init__(self) -> None:
        url = os.environ.get("ODOO_URL", "http://localhost:8069")
        self.db = os.environ.get("ODOO_DB", "")
        self.login = os.environ.get("ODOO_LOGIN", "")
        self.password = os.environ.get("ODOO_PASSWORD", "")
        self._common = None
        self._models = None
        self._uid: int | None = None
        self._url = url
        self._comodel_cache: dict[tuple[str, str], str | None] = {}
        self._turn_created: dict[tuple[str, str], int] = {}

    def _connect(self) -> tuple[Any, int]:
        if self._models is not None and self._uid:
            return self._models, self._uid
        if not self.db or not self.login or not self.password:
            raise ExecutorError("Odoo env (ODOO_DB/LOGIN/PASSWORD) not configured")
        self._common = xmlrpc.ServerProxy(f"{self._url}/xmlrpc/2/common")
        uid = self._common.authenticate(self.db, self.login, self.password, {})
        if not uid:
            raise ExecutorError(
                f"Odoo authentication failed for {self.login} on db {self.db}"
            )
        self._uid = uid
        self._models = xmlrpc.ServerProxy(f"{self._url}/xmlrpc/2/object")
        return self._models, uid

    # ------------------------------------------------------------------
    def _execute(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        models, uid = self._connect()
        try:
            return models.execute_kw(
                self.db, uid, self.password, model, method, args or [], kwargs or {}
            )
        except xmlrpc.Fault as fault:
            raise ExecutorError(f"odoo {model}.{method}: {fault.faultString}") from fault

    def _resolve_ids(self, model: str, target: Any) -> list[int]:
        """Resolve a target reference (id or fuzzy name) to record ids."""
        if target is None:
            return []
        text = str(target).strip()
        if text.isdigit():
            return [int(text)]
        cached = getattr(self, "_turn_created", {}).get((model, text.lower()))
        if cached:
            return [cached]
        if model == "account.move":
            # a customer invoice is identified by its auto number OR its ref
            # (the router may only know the ref, e.g. "facture_newayit").
            domain = ["|", ["name", "ilike", text], ["ref", "ilike", text]]
        else:
            domain = [["name", "ilike", text]]
        records = self._execute(
            model, "search_read", [domain], {"fields": ["id"], "limit": 10}
        )
        return [r["id"] for r in records]

    def _comodel(self, model: str, field: str) -> str | None:
        """Cached comodel of a relational field (None if not relational)."""
        key = (model, field)
        if key in self._comodel_cache:
            return self._comodel_cache[key]
        co = None
        try:
            recs = self._execute(
                "ir.model.fields", "search_read",
                [[["model", "=", model], ["name", "=", field]]],
                {"fields": ["relation"], "limit": 1},
            )
            relation = recs[0].get("relation") if recs else None
            co = relation or None
        except ExecutorError:
            co = None
        self._comodel_cache[key] = co
        return co

    def _to_id(self, model: str, field: str, value: Any) -> Any:
        """Resolve a value for a relational *_id field: int passes through,
        name-like strings get resolved against the field's comodel."""
        if value is None or value == "":
            return None
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            return int(value)
        co = self._comodel(model, field)
        text = str(value).strip()
        if co:
            alias = _NAME_ALIASES.get(co, {}).get(text.lower().strip())
            if alias is not None:
                recs = self._execute(
                    co, "search_read", [[["name", "=", alias]]],
                    {"fields": ["id"], "limit": 1},
                )
                if recs:
                    return recs[0]["id"]
            recs = self._execute(
                co, "search_read", [[["name", "ilike", text]]],
                {"fields": ["id"], "limit": 1},
            )
            if recs:
                return recs[0]["id"]
        return None  # caller drops the field if it cannot be resolved

    def _resolve_tag_ids(self, tags: Any) -> list[int]:
        """Resolve contact tag names to res.partner.category ids (find-or-create)."""
        names: list[str] = []
        if isinstance(tags, str):
            for part in tags.replace(";", ",").split(","):
                n = part.strip()
                if n:
                    names.append(n)
        else:
            for item in tags or []:
                if item is None:
                    continue
                n = str(item).strip()
                if n:
                    names.append(n)
        ids: list[int] = []
        for name in names:
            recs = self._execute(
                "res.partner.category", "search_read",
                [[["name", "=", name]]], {"fields": ["id"], "limit": 1},
            )
            if recs:
                ids.append(recs[0]["id"])
            else:
                created = self._execute("res.partner.category", "create", [{"name": name}])
                ids.append(int(created))
        return ids

    def _build_vals(self, model: str, params: dict[str, Any], method: str = "create") -> dict[str, Any]:
        field_map = _FIELD_MAPS.get(model, {})
        target_key = (
            OdooExecutor._target_key(f"{model}.x")
            if method in ("write", "unlink", "copy")
            else None
        )
        vals: dict[str, Any] = {}
        for key, value in params.items():
            if key == target_key:
                continue
            if value is None or value == "":
                continue
            if model == "res.partner" and key == "address":
                lines = [ln.strip() for ln in str(value).splitlines() if ln.strip()]
                if lines:
                    vals["street"] = lines[0]
                    if len(lines) > 1:
                        vals["street2"] = "\n".join(lines[1:])
                continue
            if model == "res.partner" and key == "tags":
                tag_ids = self._resolve_tag_ids(value)
                if tag_ids:
                    vals["category_id"] = [(6, 0, tag_ids)]
                continue
            mapped = field_map.get(key)
            if mapped is None:
                continue
            if mapped.endswith("_id") and not str(value).strip().isdigit():
                resolved = self._to_id(model, mapped, value)
                if resolved is None:
                    continue
                vals[mapped] = resolved
            elif mapped in _NUMERIC_FIELDS:
                num = _parse_number(value)
                if num is not None:
                    vals[mapped] = num
                else:
                    vals[mapped] = value
            else:
                vals[mapped] = value
        # semantic escalations
        if model == "res.partner":
            is_company_flagged = bool(params.get("company") or params.get("is_company"))
            if is_company_flagged and not params.get("name"):
                vals["is_company"] = True
            if params.get("is_company"):
                vals["is_company"] = params["is_company"] in (True, 1, "1", "true", "yes")
        if model == "crm.lead":
            if method == "create" and not vals.get("name") and not params.get("name"):
                vals["name"] = params.get("company") or params.get("contact") or "Lead"
            priority = params.get("priority")
            if isinstance(priority, str) and priority not in ("0", "1", "2", "3"):
                _int = {"low": 0, "medium": 1, "high": 2, "urgent": 3}.get(
                    str(priority).lower()
                )
                if _int is not None:
                    vals["priority"] = str(_int)
        if model == "account.move":
            vals.setdefault("move_type", "out_invoice")
            if method == "create":
                amount = _parse_number(
                    params.get("amount") or params.get("total") or params.get("price") or 0
                )
                line = {
                    "name": params.get("label") or params.get("description") or "Service",
                    "quantity": _parse_number(params.get("quantity") or 1),
                    "price_unit": amount,
                }
                account_id = self._default_account_id()
                if account_id:
                    line["account_id"] = account_id
                vals.setdefault("invoice_line_ids", [(0, 0, line)])
                day = params.get("invoice_day") or params.get("day")
                month = params.get("invoice_month") or params.get("month")
                if params.get("invoice_date"):
                    parsed = _parse_invoice_date(str(params["invoice_date"]))
                elif day and month:
                    parsed = _parse_invoice_date(f"{day} {month}")
                else:
                    parsed = None
                if parsed:
                    vals.setdefault("invoice_date", parsed)
        return vals

    def _default_account_id(self) -> int | None:
        recs = self._execute(
            "account.account", "search_read",
            [[["account_type", "in", ["asset_receivable", "liability_payable"]]]],
            {"fields": ["id"], "limit": 1},
        )
        return recs[0]["id"] if recs else None

    def read_partner_email(self, target: Any) -> str | None:
        """First email address of a partner (id or fuzzy name), or None."""
        ids = self._resolve_ids("res.partner", target)
        if not ids:
            return None
        records = self._execute(
            "res.partner", "search_read",
            [[["id", "=", ids[0]]]],
            {"fields": ["email"], "limit": 1},
        )
        email = records[0].get("email") if records else None
        return str(email).strip() or None

    def read_partner_phone(self, target: Any) -> str | None:
        """First phone/mobile of a partner (id or fuzzy name), or None.

        Used by the Telegram executor to bridge a person name found in the
        Odoo contact book to a Telegram account via their phone number."""
        ids = self._resolve_ids("res.partner", target)
        if not ids:
            return None
        records = self._execute(
            "res.partner", "search_read",
            [[["id", "in", ids]]],
            {"fields": ["name", "phone", "mobile"], "limit": 5},
        )
        for record in records:
            phone = str(record.get("mobile") or record.get("phone") or "").strip()
            if phone:
                return phone
        return None

    def read_invoice_packet(self, invoice_ref: Any) -> dict[str, Any] | None:
        """Full printable data for an invoice: move + partner + product lines.

        Used by the email executor to render the PDF attachment. account.move.line
        holds 2 records per invoice line (invoice/asset halves); only the halves
        carrying a description (the product lines) are returned.
        """
        ids = self._resolve_ids("account.move", invoice_ref)
        if not ids:
            return None
        moves = self._execute(
            "account.move", "search_read",
            [[["id", "in", ids]]],
            {
                "fields": [
                    "id", "name", "ref", "state", "invoice_date", "amount_total",
                    "partner_id", "move_type", "currency_id",
                ],
                "limit": 5,
            },
        )
        if not moves:
            return None
        move = moves[0]
        partner: dict[str, Any] | None = None
        pid = move.get("partner_id")
        pid_val = pid[0] if isinstance(pid, (list, tuple)) and pid else pid
        if pid_val:
            records = self._execute(
                "res.partner", "search_read",
                [[["id", "=", pid_val]]],
                {"fields": ["id", "name", "email", "street", "street2", "city", "zip", "country_id"], "limit": 1},
            )
            partner = records[0] if records else None
        lines = self._execute(
            "account.move.line", "search_read",
            [[["move_id", "=", move["id"]]]],
            {
                "fields": ["name", "quantity", "price_unit", "price_subtotal"],
                "limit": 200,
            },
        )
        lines = [
            line
            for line in lines
            if line.get("name")
            and (line.get("quantity") is not None or line.get("price_subtotal") is not None)
        ]
        return {"move": move, "partner": partner, "lines": lines}

    # ------------------------------------------------------------------
    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        self._turn_created = {}

        sorted_actions = sorted(
            ctx.actions.items(),
            key=lambda ap: (0 if _split_action(ap[0])[1] == "create" else 1),
        )

        for action, payload in sorted_actions:
            params = (payload or {}).get("fields", {})
            model, method = _split_action(action)
            if not model or not method:
                results.append(deferred_action(action, params, f"cannot parse '{action}'"))
                continue
            try:
                data = self._handle(model, method, params, action, intent=ctx.intent)
                if data is None:
                    results.append(deferred_action(action, params))
                    continue
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"odoo:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"odoo:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )
        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)

    # ------------------------------------------------------------------
    def _handle(
        self, model: str, method: str, params: dict[str, Any], action: str, intent: str = ""
    ) -> Any | None:
        if model in ("event", "calendar"):
            return self._handle_calendaring(model, method, params, action, intent=intent)
        if method in _STANDARD_METHODS:
            target_key = self._target_key(action)
            if method == "create":
                vals = self._build_vals(model, params, method="create")
                if not vals:
                    raise ExecutorError(f"odoo {action}: no usable fields")
                created_id = self._execute(model, "create", [vals])
                name_val = vals.get("name") or vals.get("title") or vals.get("partner_name") or ""
                if name_val:
                    self._turn_created[(model, str(name_val).strip().lower())] = created_id
                ref_val = vals.get("ref")
                if ref_val:
                    self._turn_created[(model, str(ref_val).strip().lower())] = created_id
                return {"id": created_id}
            if method == "write":
                ids = self._resolve_ids(model, params.get(target_key)) or self._resolve_ids(model, params.get("name"))
                if not ids:
                    raise ExecutorError(f"odoo {action}: target not found ({target_key})")
                vals = self._build_vals(model, params, method="write")
                return {"ids": ids, "ok": self._execute(model, "write", [ids, vals])}
            if method == "unlink":
                ids = self._resolve_ids(model, params.get(target_key))
                if not ids:
                    raise ExecutorError(f"odoo {action}: target not found ({target_key})")
                return {"ids": ids, "ok": self._execute(model, "unlink", [ids])}
            if method == "copy":
                ids = self._resolve_ids(model, params.get(target_key))
                if not ids:
                    raise ExecutorError(f"odoo {action}: target not found ({target_key})")
                return {"ids": ids, "ok": self._execute(model, "copy", [ids])}
        if method in _SEARCH_METHODS:
            query = params.get("query") or params.get("search_query") or params.get("q")
            target = params.get(self._target_key(action))
            name = params.get("name")
            needle = query or name or target
            domain = [[["name", "ilike", needle]]] if needle else [[]]
            fields = params.get("fields") if isinstance(params.get("fields"), list) else ["id", "name"]
            out = self._execute(model, "search_read", domain, {"fields": fields, "limit": 25})
            return out
        if model == "account.move" and method == "action_get":
            packet = self.read_invoice_packet(params.get("invoice"))
            if not packet:
                raise ExecutorError(f"odoo {action}: invoice not found")
            return packet
        if method.startswith("action_") or method in (
            "convert_opportunity", "merge_opportunity", "create_invoices",
        ):
            return self._call_method_on_target(model, method, params, action)
        return None

    # ------------------------------------------------------------------
    # Calendaring (event.* / calendar.* -> calendar.event on Odoo)
    # ------------------------------------------------------------------
    def _handle_calendaring(
        self, model: str, method: str, params: dict[str, Any], action: str, intent: str = ""
    ) -> Any | None:
        cmodel = "calendar.event"
        if method == "create":
            return self._event_create(params, action)
        if method in ("modify", "update"):
            return self._event_modify(params, action)
        if method in ("query", "multiget", "sync"):
            return self._event_query(params, action, intent=intent)
        if method == "delete":
            ids = self._resolve_calendar_event(action, params)
            if not ids and self._wants_delete_all(params, intent):
                all_events = self._execute(
                    "calendar.event", "search_read",
                    [[]], {"fields": ["id"], "limit": 1000, "order": "id asc"},
                )
                ids = [r["id"] for r in all_events]
            if not ids:
                raise ExecutorError(f"odoo {action}: event not found")
            return {"ids": ids, "deleted": len(ids), "ok": self._execute(cmodel, "unlink", [ids])}
        if method == "free_busy":
            return self._event_free_busy(params, action)
        if method == "recurrence":
            ids = self._resolve_calendar_event(action, params)
            if not ids:
                raise ExecutorError(f"odoo {action}: event not found")
            recurrence = str(params.get("recurrence") or "weekly").strip().lower()
            rrule_freq = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "yearly": "YEARLY"}.get(
                recurrence, "WEEKLY"
            )
            count = params.get("count")
            rrule = f"FREQ={rrule_freq};COUNT={int(count)}" if count and str(count).isdigit() else f"FREQ={rrule_freq}"
            self._execute(cmodel, "write", [ids, {"recurrency": True, "rrule": rrule}])
            return {"ids": ids, "ok": True, "rrule": rrule}
        if method == "reminder":
            ids = self._resolve_calendar_event(action, params)
            if not ids:
                raise ExecutorError(f"odoo {action}: event not found")
            minutes = self._reminder_minutes(params)
            alarm = self._ensure_alarm(minutes)
            if alarm is not None:
                self._execute(cmodel, "write", [ids, {"alarm_ids": [(6, 0, [alarm])]}])
            return {"ids": ids, "ok": True, "minutes_before": minutes}
        if model == "calendar" and method == "create":
            # Odoo calendars are user-scoped; create a standalone event as a proxy
            # is not meaningful. Return an explicit acknowledgement with no id.
            return {"ok": True, "note": f"calendar '{params.get('name') or ''}' demandé"}
        if model == "calendar" and method == "list":
            return self._event_query({}, action)
        return None

    @staticmethod
    def _wants_delete_all(params: dict[str, Any], intent: str) -> bool:
        """True when the user asked to remove *all* events (no specific title):
        'supprimer tous les réunions', 'efface toutes mes réunions'..."""
        if params.get("title") or params.get("name") or params.get("event"):
            return False
        text = f"{intent} {' '.join(str(v) for v in params.values())}".lower()
        return any(
            token in text
            for token in ("tous", "toutes", "tout ", " remove", "all ", "tous les", "toutes les")
        ) and any(
            token in text
            for token in ("réunion", "reunion", "event", "événement", "rendez-vous", "rdv", "calendrier")
        )

    def _user_tz(self) -> str | None:
        """The authenticated Odoo user's timezone (res.users.tz), cached.

        Kept as the basis for converting wall-clock event hours to UTC so the
        Odoo calendar renders exactly the hour the user asked for, whatever the
        account timezone is."""
        cached = getattr(self, "_user_tz_cache", None)
        if cached is None:
            tz = ""
            try:
                recs = self._execute(
                    "res.users", "search_read",
                    [[["id", "=", self._uid]]],
                    {"fields": ["tz"], "limit": 1},
                )
                tz = (recs[0].get("tz") or "") if recs else ""
            except Exception:  # noqa: BLE001 - tz stays unknown
                tz = ""
            self._user_tz_cache = tz
        return self._user_tz_cache or None

    def _event_create(self, params: dict[str, Any], action: str) -> dict[str, Any]:
        title = params.get("title") or params.get("name")
        if not title:
            raise ExecutorError(f"odoo {action}: missing title")
        vals: dict[str, Any] = {"name": title}
        tz = self._user_tz()
        start = _event_datetime(params, "start_") or _event_datetime(params, "")
        if start:
            vals["start"] = _to_utc_naive(start, tz)
            stop = _event_datetime(params, "end_")
            if stop:
                vals["stop"] = _to_utc_naive(stop, tz)
            else:
                vals["stop"] = _add_hours(vals["start"], 1)
        if params.get("location"):
            vals["location"] = str(params["location"])
        if params.get("description"):
            vals["description"] = str(params["description"])
        attendees = params.get("attendees") or params.get("participants")
        if isinstance(attendees, list) and attendees:
            ids = self._resolve_partner_ids(attendees)
            if ids:
                vals["partner_ids"] = [(4, i) for i in ids]
        if "start" not in vals:
            raise ExecutorError(f"odoo {action}: missing start date")
        created_id = self._execute("calendar.event", "create", [vals])
        name_key = str(title).strip().lower()
        self._turn_created[("calendar.event", name_key)] = created_id
        return {"id": created_id, "name": title}

    def _event_modify(self, params: dict[str, Any], action: str) -> dict[str, Any]:
        """Update an existing calendar.event by title (modifier/reporter la
        date/heure/lieu d'une réunion existante)."""
        ids = self._resolve_calendar_event(action, params)
        if not ids:
            raise ExecutorError(f"odoo {action}: event not found")
        vals: dict[str, Any] = {}
        title = params.get("title") or params.get("name")
        if title:
            vals["name"] = str(title)
        if params.get("location"):
            vals["location"] = str(params["location"])
        if params.get("description"):
            vals["description"] = str(params["description"])
        tz = self._user_tz()
        start = _event_datetime(params, "start_") or _event_datetime(params, "")
        if start:
            vals["start"] = _to_utc_naive(start, tz)
            stop = _event_datetime(params, "end_")
            vals["stop"] = _to_utc_naive(stop, tz) if stop else _add_hours(vals["start"], 1)
        if not vals:
            raise ExecutorError(f"odoo {action}: no fields to update")
        self._execute("calendar.event", "write", [ids, vals])
        return {"ids": ids, "ok": True, "updated": sorted(vals.keys())}

    def _event_query(
        self, params: dict[str, Any], action: str, intent: str = ""
    ) -> list[dict[str, Any]]:
        title = params.get("title") or params.get("name")
        domain: list[Any] = []
        if title:
            domain.append(["name", "ilike", str(title)])
        start = _event_datetime(params, "start_") or _event_datetime(params, "")
        if start:
            domain.append(["start", ">=", _to_utc_naive(start, self._user_tz())])
        # "les réunions à venir" means FROM NOW ON: without this the query returns
        # past meetings too, which is not what the user asked for.
        scope = _event_scope(intent)
        now = _to_utc_naive(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self._user_tz())
        window = _relative_window(intent) if not start else None
        if scope == "upcoming" and not start:
            domain.append(["start", ">=", now])
        elif scope == "past" and not start:
            domain.append(["stop", "<=", now])
        if window:
            domain.append(["start", ">=", _to_utc_naive(window[0], self._user_tz())])
            domain.append(["start", "<=", _to_utc_naive(window[1], self._user_tz())])
        # Upcoming meetings read best in chronological order, history reversed.
        order = "start desc" if scope == "past" else "start asc"
        recs = self._execute(
            "calendar.event", "search_read",
            [domain],
            {"fields": ["id", "name", "start", "stop", "location", "partner_ids"], "limit": 50, "order": order},
        )
        for r in recs:
            r.pop("partner_ids", None)
            for k in ("start", "stop"):
                if r.get(k):
                    r[k] = str(r[k])
        return recs

    def _event_free_busy(self, params: dict[str, Any], action: str) -> dict[str, Any]:
        from_ = _event_datetime(params, "from_") or _event_datetime(params, "")
        day_to = params.get("day_to") or params.get("to_day")
        month_to = params.get("month_to")
        if day_to and month_to:
            try:
                to_d = int(day_to)
                to_m = _FRENCH_MONTHS.get(str(month_to).strip().lower())
                to = f"{date.today().year:04d}-{to_m:02d}-{to_d:02d} 23:59:59"
            except Exception:  # noqa: BLE001
                to = None
        else:
            to = None
        domain: list[Any] = []
        if from_:
            domain.append(["start", ">=", _to_utc_naive(from_, self._user_tz())])
            if to:
                domain.append(["start", "<=", _to_utc_naive(to, self._user_tz())])
        recs = self._execute(
            "calendar.event", "search_read",
            [domain], {"fields": ["id", "name", "start", "stop"], "limit": 100},
        )
        busy = [
            {"event": r.get("name"), "start": str(r.get("start") or ""), "stop": str(r.get("stop") or "")}
            for r in recs
        ]
        return {"free": not busy, "busy_events": busy, "from": from_ or "", "to": to or ""}

    def _resolve_calendar_event(self, action: str, params: dict[str, Any]) -> list[int]:
        target = params.get("title") or params.get("name") or params.get("event")
        if not target:
            return []
        cache_key = ("calendar.event", str(target).strip().lower())
        cached = self._turn_created.get(cache_key)
        if cached:
            return [cached]
        domain = [["name", "ilike", str(target)]]
        start = _event_datetime(params, "start_") or _event_datetime(params, "")
        if start:
            domain.append(["start", ">=", _to_utc_naive(start, self._user_tz())])
        recs = self._execute(
            "calendar.event", "search_read", [domain], {"fields": ["id"], "limit": 10, "order": "start desc"},
        )
        return [r["id"] for r in recs]

    def _resolve_partner_ids(self, attendees: list[Any]) -> list[int]:
        ids: list[int] = []
        for name in attendees:
            text = str(name).strip()
            if not text:
                continue
            if text.isdigit():
                ids.append(int(text))
                continue
            recs = self._execute(
                "res.partner", "search_read", [[["name", "ilike", text]]], {"fields": ["id"], "limit": 1},
            )
            if not recs and "@" in text:
                recs = self._execute(
                    "res.partner", "search_read", [[["email", "ilike", text]]], {"fields": ["id"], "limit": 1},
                )
            if recs:
                ids.append(recs[0]["id"])
        return ids

    def _reminder_minutes(self, params: dict[str, Any]) -> int:
        if params.get("minutes_before"):
            m = _parse_time(params["minutes_before"])
            if m is not None:
                return m
        if params.get("hours_before"):
            m = _parse_time(params["hours_before"])
            if m is not None:
                return m * 60
        if params.get("days_before"):
            try:
                return int(float(params["days_before"])) * 1440
            except Exception:  # noqa: BLE001
                pass
        return 15

    def _ensure_alarm(self, minutes: int) -> int | None:
        try:
            recs = self._execute(
                "calendar.alarm", "search_read", [[["duration_minutes", "=", minutes]]], {"fields": ["id"], "limit": 1},
            )
            if recs:
                return recs[0]["id"]
            return self._execute("calendar.alarm", "create", [{"name": f"{minutes} min", "duration_minutes": minutes}])
        except ExecutorError:
            return None

    def _call_method_on_target(
        self, model: str, method: str, params: dict[str, Any], action: str
    ) -> dict[str, Any]:
        target_key = self._target_key(action)
        ids = self._resolve_ids(model, params.get(target_key))
        if not ids:
            raise ExecutorError(f"odoo {action}: target not found ({target_key})")
        if model == "account.move" and action.endswith(".action_invoice_download_pdf"):
            return self._invoice_download_payload(ids, action)
        return {
            "ids": ids,
            "result": self._execute(model, method, [ids]),
        }

    def _invoice_download_payload(
        self, ids: list[int], action: str
    ) -> dict[str, Any]:
        """'Download the invoice' without a PDF pipeline: confirm any drafts
        (action_post -> real number) and return each invoice's identity so the
        follow-up email can reference it. PDF bytes need Odoo web auth (not
        XML-RPC), so we hand back the confirmed move data instead of crashing
        the whole turn on a nonexistent RPC method."""
        def _read() -> list[dict[str, Any]]:
            return self._execute(
                "account.move", "search_read",
                [[["id", "in", ids]]],
                {"fields": ["id", "name", "ref", "state", "amount_total", "invoice_date", "partner_id"]},
            )
        recs = _read()
        draft_ids = [r["id"] for r in recs if r.get("state") == "draft"]
        if draft_ids:
            try:
                self._execute("account.move", "action_post", [draft_ids])
            except ExecutorError:
                pass  # still return the draft's identity rather than fail the turn
            recs = _read()
        return {
            "invoices": [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "ref": r.get("ref"),
                    "state": r.get("state"),
                    "amount_total": r.get("amount_total"),
                    "invoice_date": str(r.get("invoice_date") or ""),
                    "partner_id": r.get("partner_id"),
                }
                for r in recs
            ],
        }

    @staticmethod
    def _target_key(action: str) -> str:
        model = action.rsplit(".", 1)[0] if "." in action else ""
        mapping = {
            "res.partner": "partner",
            "crm.lead": "lead",
            "sale.order": "order",
            "sale.order.line": "line",
            "account.move": "invoice",
            "hr.employee": "employee",
            "hr.applicant": "applicant",
            "hr.expense": "expense",
            "hr.leave": "leave",
        }
        return mapping.get(model, "target")