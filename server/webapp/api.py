"""Viito HTTP API — conversations + actions (executor_log).

Exposes thin HTTP wrappers over the existing pipeline primitives:
  * ``run_turn()`` (server/execution/graph.py) for every user turn,
  * ``PipelineState.executor_log`` for the action inventory.

No new business logic is invented here — these routes only translate the
pipeline's state into the JSON shapes the webapp expects.

Endpoints
---------
GET  /conversations                -> list of conversations
GET  /conversations/{id}           -> { messages: [...] }
POST /conversations                -> create a conversation, body { context_item }
POST /conversations/{id}/messages  -> run one user turn, body { text }
                                      returns { role: "assistant", content, meta }
GET  /actions/summary              -> [{ domain, count }, ...]
GET  /actions/{domain}             -> ActionLogItem[]
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.config import load_environment
from server.text_prose import is_topic_fragment

load_environment()

router = APIRouter(tags=["viito"])

# ---------------------------------------------------------------------------
# Store (in-memory + JSON file for durability across restarts)
# ---------------------------------------------------------------------------
_STORE_PATH = Path(__file__).resolve().parent / "viito_store.json"
_lock = threading.Lock()

# Each conversation: { id, title, tool, last_message, time, created_at, messages:[{role,content,meta}] }
# Each conversation stores its source thread id (used to re-join the same
# LangGraph thread for cross-turn memory) as `thread_id`.


def _default_store() -> dict:
    return {"conversations": [], "action_log": []}


def _load_store_locked() -> dict:
    try:
        if _STORE_PATH.exists():
            data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "conversations" in data:
                return data
    except Exception:
        pass
    return _default_store()


def _save_store_locked(store: dict) -> None:
    try:
        _STORE_PATH.write_text(
            json.dumps(store, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


def _get_store() -> dict:
    with _lock:
        return _load_store_locked()


def _mutate(mutator):
    """Apply ``mutator(store)`` under lock and persist."""
    with _lock:
        store = _load_store_locked()
        result = mutator(store)
        _save_store_locked(store)
        return result


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------
class CreateConversationRequest(BaseModel):
    context_item: dict | None = None


class FileAttachment(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    content_b64: str


class SendMessageRequest(BaseModel):
    text: str
    attachments: list[FileAttachment] | None = None


# ---------------------------------------------------------------------------
# Pipeline bridge
# ---------------------------------------------------------------------------
_CONTEXT_SEP_RE = re.compile(r"\n\n---\n|\n\n\[Pi[eè]ce[s]? join(?:te[s]?|e)?:|\n\n\[Pi[eè]ces jointes:|\n\n\[Image disponible:")


def _instruction_only(query: str) -> str:
    """Return the genuine user instruction, stripping appended document/file context.

    The Viito session prepends the document's full text (and attachment/image
    markers) to the query so content-generating actions (resume/redaction) can
    reference it. That dump is pure noise for attribute extraction, so we cut
    at the first context separator before classifying attributes.
    """
    if not query:
        return query
    match = _CONTEXT_SEP_RE.search(query)
    return (query[: match.start()] if match else query).strip()


def _build_history(messages: list[dict], current_text: str | None = None, max_turns: int = 6) -> str | None:
    """Flatten recent conversation turns into a compact context block.

    Excludes the current user message (already supplied as the intent) so the
    classifier can resolve anaphora ("ce fichier", "le même") from prior turns.
    Returns None when there is no prior context.
    """
    turns: list[str] = []
    for msg in reversed(messages or []):
        if msg.get("content") == current_text or not msg.get("content"):
            continue
        role = "Utilisateur" if msg.get("role") == "user" else "Viito"
        turns.append(f"{role} : {msg['content']}")
        if len(turns) >= max_turns * 2:
            break
    if not turns:
        return None
    return "\n".join(reversed(turns))


#: office actions whose content must be WRITTEN first: the text is produced by the
#: general (redaction) domain, then injected into the document by office.
_PROSE_OFFICE_ACTIONS = {"office.editor.add_content", "office.docbuilder.generate"}

#: Tone requested by the user -> the wording handed to the redaction model. The
#: attributes classifier extracts ``tone`` on its own; these rules cover the turns
#: where only office was classified, so the redaction still gets an explicit tone.
_TONE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(formel|formelle|soutenu|corporate|professionnel|professionnelle)\b", re.I),
     "professionnel et formel"),
    (re.compile(r"\b(d[ée]contract[ée]|familier|famili[èe]re|amical|chaleureux|detendez? vous)\b", re.I),
     "décontracté et amical"),
    (re.compile(r"\b(persuasif|persuasive|convaincant|commercial|incitatif)\b", re.I),
     "persuasif et commercial"),
    (re.compile(r"\b(neutre|factuel|impartial|objective)\b", re.I), "neutre et factuel"),
    (re.compile(r"\b(technique|technique et d[ée]taill[ée]|d[ée]taill[ée]|pr[ée]cis)\b", re.I),
     "technique et détaillé"),
    (re.compile(r"\b(p[ée]dagogique|clair|claire|simple|vulgaris[ée])\b", re.I),
     "clair et pédagogique"),
)

#: Business-document default when the user states no tone.
_DEFAULT_TONE = "professionnel"


def _requested_tone(instruction: str) -> str:
    """Read the requested tone out of the instruction (default: professional)."""
    for pattern, tone in _TONE_RULES:
        if pattern.search(instruction or ""):
            return tone
    return _DEFAULT_TONE


def _redaction_fields(envelope: dict, instruction: str) -> dict[str, str]:
    """Build the redaction action's fields: what to write, and in which tone.

    ``message`` is the context-stripped instruction (never the Viito document
    dump), so the general executor writes the requested prose instead of
    rewriting the file. Whatever the attributes classifier already decided is
    kept: the router's choice wins, these fields only fill the gaps.
    """
    classified = (
        ((envelope.get("fields") or {}).get("general") or {}).get("redaction") or {}
    ).get("fields") or {}
    message = str(classified.get("message") or "").strip() or instruction
    tone = str(classified.get("tone") or "").strip() or _requested_tone(instruction)
    fields = {str(k): str(v) for k, v in classified.items()}
    fields["message"] = _instruction_only(message) or instruction
    fields["tone"] = tone
    return fields


def _office_carries_literal_text(envelope: dict, action: str, intent: str) -> bool:
    """True when the classified office fields already hold a text to insert verbatim.

    A user who dictates the content ("ajoute le texte \\"Bonjour l'equipe\\"")
    must not have it replaced by a generated paragraph, so such a turn needs no
    redaction domain.
    """
    fields = (envelope.get("fields") or {}).get("office") or {}
    payload = (fields.get(action) or {}).get("fields") or {}
    text = str(payload.get("text") or payload.get("content_text") or "").strip()
    if not text:
        return False
    return not is_topic_fragment(text, intent)


def _ensure_redaction_domain(
    query: str, domains: list[str], actions: list[str], envelope: dict
) -> dict:
    """Route a document edit that needs new prose through the general domain first.

    The office executor never invents text: it injects the prose produced by the
    general domain (``_prior_text_content``). So any ``add_content``/``generate``
    turn whose text is missing or only a topic must be executed as general
    (redaction) -> office, in that order, otherwise the classifier's bare topic
    ("les bienfaits de l'IA...") would be written into the document as a stub.
    """
    prose_actions = [a for a in actions if a in _PROSE_OFFICE_ACTIONS]
    if not prose_actions or "office" not in domains:
        return envelope
    intent = _instruction_only(query)
    if any(_office_carries_literal_text(envelope, a, intent) for a in prose_actions):
        return envelope  # the user supplied the words to insert

    fields = dict(envelope.get("fields") or {})
    general_fields = dict(fields.get("general") or {})
    redaction_fields = _redaction_fields(envelope, intent)
    if "general" in domains:
        if "redaction" in general_fields:
            # Keep the classifier's redaction attributes, complete them so the
            # inspector always shows the message and the tone it will use.
            general_fields["redaction"] = {
                **(general_fields["redaction"] or {}),
                "fields": redaction_fields,
            }
            fields["general"] = general_fields
        if domains.index("general") > domains.index("office"):
            # Stable sort: general first, every other domain keeps its order.
            domains = sorted(domains, key=lambda d: 0 if d == "general" else 1)
            print(f"[CLASSIFY] general reordered before office for {query[:60]!r}", flush=True)
        return {**envelope, "domains": domains, "fields": fields}

    # No general action classified: create the redaction with explicit fields, so
    # the model works from the user instruction alone instead of from the document
    # dump (see ``_strip_context`` in the general executor).
    fields["general"] = {"redaction": {"fields": redaction_fields}}
    envelope = {
        **envelope,
        "domains": ["general", *domains],
        "actions": ["redaction", *actions],
        "fields": fields,
    }
    print(
        f"[CLASSIFY] general/redaction added before office (tone={redaction_fields['tone']!r})"
        f" for {query[:60]!r}",
        flush=True,
    )
    return envelope


def _classify_and_envelope(query: str, history: str | None = None) -> dict:
    """Route a natural-language query -> RouterOutput envelope.

    Mirrors exec_demo._cmd_query's classification steps (same agents the
    production path uses). Requires OPENROUTER_API_KEY for the LLM path;
    raises RuntimeError when unavailable so the caller can report honestly.
    """
    import os

    from server.cli import DEFAULT_MODEL, attribute_classifier_model
    from server.agents.domain_classifier2 import classify_domain
    from server.agents.action_classifier import classify_actions
    from server.agents.attributes_classifier import classify_attributes

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY absent — cannot classify request.")

    common = dict(model=DEFAULT_MODEL, api_key=api_key, temperature=0.0, max_tokens=1024)
    attributes_common = dict(
        model=attribute_classifier_model, api_key=api_key, temperature=0.0, max_tokens=1024
    )
    domain_client = _build_domain_client(common)
    action_client = _build_action_client(common)
    attributes_client = _build_attributes_client(attributes_common)

    domains = classify_domain(query, model_client=domain_client).predicted_domains
    print(f"[CLASSIFY] domain={domains} query={query[:80]!r}", flush=True)
    actions = classify_actions(
        query, domains=domains, model_client=action_client, domain_client=domain_client
    ).predicted_actions
    print(f"[CLASSIFY] actions={actions} query={query[:80]!r}", flush=True)
    # Attribute extraction only needs the genuine instruction; the appended
    # document/file context (which content-generating domains rely on via
    # `intent`) only adds noise here and can suppress field extraction.
    attributes_intent = _instruction_only(query)
    envelope = classify_attributes(
        attributes_intent,
        domains=domains,
        actions=actions,
        model_client=attributes_client,
        domain_client=domain_client,
        action_client=action_client,
        history=history,
    ).to_terminal()
    envelope["intent"] = query
    envelope = _ensure_redaction_domain(query, domains, actions, envelope)
    print(f"[CLASSIFY] envelope={json.dumps(envelope, ensure_ascii=False)[:500]}", flush=True)
    return envelope


def _build_domain_client(cfg):
    from server.agents.domain_classifier2 import build_domain_classifier_client

    return build_domain_classifier_client(**cfg)


def _build_action_client(cfg):
    from server.agents.action_classifier import build_action_classifier_client

    return build_action_classifier_client(**cfg)


def _build_attributes_client(cfg):
    from server.agents.attributes_classifier import build_attributes_classifier_client

    return build_attributes_classifier_client(**cfg)


_CHAT_READ_VERB_RE = re.compile(
    r"(r[ée]sum|synth[ée]t|condens|analys|explic|tradu|que contient|que dit|contenu de|"
    r"extrai|relis la|lecture|v[ée]rif|interpr[èe]te|r[ée]dige|r[ée]daction|"
    r"pr[ée]cise|donne-moi|dis-moi|indique)",
    re.IGNORECASE,
)

_CHAT_READ_EXCLUDE_RE = re.compile(
    r"\b(envoie|envoi|transf[èe]re|transmission|sauvegarde|enregistre|importe|"
    r"t[éè]l[éè]charge|partage|d[éè]place|renomme|convert|supprime|efface|archiv|"
    r"modif|corrige|mets (?:a|à) jour|ajoute|ins[èe]re|cr[ée]e)\b",
    re.IGNORECASE,
)


def _is_chat_read_request(instruction: str) -> bool:
    """True when a chat-attached file is used as reference content (never when
    the request manipulates the file: send/save/import/upload/share/edit...).

    The webapp chat attaches files as informational context; the model must be
    able to READ them (summarize, analyze, redact from). Routing such requests
    deterministically to ``general`` avoids the office ``wopi.get_file`` stub
    ("executor not wired") which can neither read chat attachments nor answer.
    """
    if not instruction:
        return False
    if _CHAT_READ_EXCLUDE_RE.search(instruction):
        return False
    return bool(_CHAT_READ_VERB_RE.search(instruction))


def _force_general_router_output(instruction: str, intent: str) -> dict:
    """Deterministic ``general`` envelope for chat-attachment content requests."""
    return {
        "domains": ["general"],
        "fields": {
            "general": {
                "general": {"role": "redaction", "fields": {"message": instruction}}
            }
        },
        "intent": intent,
        "forced_domain": "general",
    }


def _chat_attachment_sources(attachments: list[dict] | None) -> str:
    """Decode raw chat attachments (``content_b64``) into readable text blocks.

    Returns the concatenated per-file text (capped like the office session), or
    "" when nothing readable. Reference material the webapp chat uploads must be
    consumable by the generation/summarization LLM — the webapp chat has no
    docserver file behind it, so embedding the text is the only way the model
    can answer about it (same extraction helpers as the OnlyOffice session).
    """
    if not attachments:
        return ""
    import base64 as b64

    from server.webapp.onlyoffice import _extract_file_text

    blocks: list[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        name = str(att.get("filename") or "fichier")
        raw = att.get("content") or att.get("content_b64") or ""
        if not raw:
            continue
        try:
            content = b64.urlsafe_b64decode(raw)
        except Exception as exc:
            print(f"[PIPELINE] could not decode attachment {name}: {exc}", flush=True)
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        try:
            text = _extract_file_text(content, ext)
        except Exception as exc:
            print(f"[PIPELINE] could not extract text from {name}: {exc}", flush=True)
            text = None
        if text and text.strip():
            blocks.append(f"===== {name} =====\n{text.strip()[:8000]}")
    return "\n\n".join(blocks)


def _run_pipeline(
    query: str,
    thread_id: str,
    user_id: str,
    attachments: list[dict] | None = None,
    history: str | None = None,
) -> dict:
    """Run a full user turn through the LangGraph execution graph.

    Returns the checkpointed state (contains final_response, executor_log).
    Raises on any pipeline failure (no API key, backend unreachable, ...).
    """
    from server.execution.graph import build_graph, run_turn

    # When files are attached, append their info + decoded text so the LLM can
    # actually read them. The block uses a CHAT-specific header ("---\nPièce(s)
    # attachée(s) au chat:") so the office-session context ("Fichier actuel")
    # stays stripped for redactions — see GeneralExecutor._attached_sources.
    enriched_query = query
    router_output: dict = {}
    if attachments:
        file_list = ", ".join(a.get("filename", "fichier") for a in attachments)
        enriched_query += f" [Pièces jointes: {file_list}]"
        chat_sources = _chat_attachment_sources(attachments)
        if chat_sources:
            enriched_query += "\n\n---\nPièce(s) attachée(s) au chat:\n" + chat_sources
            # Chat attachments are reference content: content-read/redaction
            # requests must reach the general executor (it reads the embedded
            # text), not the office wopi.get_file stub. Manipulation requests
            # (send/save/import/edit...) are left to normal routing.
            instruction = _instruction_only(query)
            if _is_chat_read_request(instruction):
                router_output = _force_general_router_output(instruction, enriched_query)
                print(
                    f"[PIPELINE] chat attachment content request -> forced general "
                    f"instruction={instruction[:80]!r}",
                    flush=True,
                )

    app = build_graph()
    state = run_turn(
        app,
        router_output=router_output,
        query=enriched_query,
        history=history,
        thread_id=thread_id,
        intent=enriched_query,
        user_id=user_id,
        attachments=attachments,
    )
    envelope = state.get("router_output") or {}
    print(f"[PIPELINE] thread={thread_id} intent={enriched_query[:80]!r} routing_retries={state.get('router_retry_count')} execution_retries={state.get('retry_count')}", flush=True)
    return state


def _final_to_reply(state: dict) -> dict:
    """Translate final_response into the assistant message the webapp expects."""
    final = state.get("final_response") or {}
    raw_actions = final.get("actions_attempted") or []
    actions_summary = []
    for rec in raw_actions:
        domain = rec.get("domain", "")
        status = rec.get("status", "")
        for r in rec.get("results") or []:
            actions_summary.append({
                "domain": domain,
                "action": r.get("action", ""),
                "status": status,
                "params": r.get("params") or {},
                "data": r.get("data") or {},
            })
    meta = {
        "outcome": final.get("outcome", "finished"),
        "domains": final.get("domains") or [],
        "actions_attempted": len(raw_actions),
        "actions": actions_summary,
        "escalated": bool(final.get("escalated")),
    }
    return {"role": "assistant", "content": final.get("message") or "Terminé.", "meta": meta}


def _summarize_data(data) -> str:
    out: list[str] = []
    if isinstance(data, dict):
        resp = data.get("response", data)
        if isinstance(resp, dict):
            for key in ("id", "task_id", "project_id", "email_id", "submission_id", "run_id", "duplicate_id"):
                if resp.get(key) is not None:
                    out.append(f"{key}={resp[key]}")
            if resp.get("answer"):
                out.append(f"answer={str(resp['answer'])[:80]}")
        if isinstance(resp, list) and resp:
            out.append(f"{len(resp)} items")
    return " | ".join(out) if out else "ok"


# ---------------------------------------------------------------------------
# Action log helpers
# ---------------------------------------------------------------------------
_AMOUNT_KEYS = ("expected_revenue", "amount_total", "amount", "price_total", "subtotal", "total", "prix", "montant")
_EMAIL_KEYS = ("email", "mail", "email_from", "email_to")
_PHONE_KEYS = ("phone", "mobile", "phone_mobile", "tel", "telephone")
_STAGE_KEYS = ("stage", "stage_id", "stage_name", "state", "step", "etape", "funnel_stage")
_TO_KEYS = ("to", "to_email", "recipient", "recipients", "address", "email_to")
_SUBJ_KEYS = ("subject", "body", "message")
_FILE_KEYS = ("filename", "file_name", "fileName", "path", "file")
_SIZE_KEYS = ("size", "filesize", "size_bytes", "size_kb")
_OBJECT_KEYS = ("title", "subject", "task_name", "event_name", "opportunity_name", "deal_name", "name")
_ENTITY_KEYS = ("company", "partner", "partner_name", "contact", "contact_name", "account", "client", "fournisseur", "organization", "organisme", "company_name", "structure", "workspace", "espace")

_RESOURCE_LABELS = {
    "crm.lead": "Lead",
    "crm.opportunity": "Opportunité",
    "res.partner": "Contact",
    "sale.order": "Devis",
    "account.move": "Facture",
    "account.move.line": "Écriture comptable",
    "account.bank.statement": "Relevé bancaire",
    "hr.leave": "Congé",
    "hr.expense": "Note de frais",
    "project.task": "Tâche",
    "project.project": "Projet",
    "calendar.event": "Événement",
    "mail.message": "Email",
    "kb.answer": "Réponse",
    "kb.space": "Espace",
    "core.document": "Document",
    "office.document": "Document",
}

_VERB_LABELS = {
    "action_convert": "converti",
    "action_post": "validé",
    "action_draft": "passé en brouillon",
    "action_cancel": "annulé",
    "action_undo_reconciliation": "dé-rapproché",
    "action_reconcile": "rapproché",
    "mark_as_done": "terminé",
    "mark_done": "terminé",
    "archived": "archivé",
    "unarchive": "désarchivé",
    "duplicate": "dupliqué",
    "send": "envoyé",
    "create": "créé",
    "write": "mis à jour",
    "update": "mis à jour",
    "unlink": "supprimé",
    "convert": "converti",
    "download": "téléchargé",
    "upload": "importé",
    "ingest": "importé",
    "share": "partagé",
    "export": "exporté",
    "move": "déplacé",
    "merge": "fusionné",
}


def _trunc(value, n: int = 120) -> str:
    text = str(value).strip().replace("\n", " ").replace("\r", " ")
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _trunc_list(value, n: int = 60) -> str:
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(x) for x in value)
    return _trunc(value, n)


def _lookup(container, keys) -> object | None:
    if not isinstance(container, dict):
        return None
    for key in keys:
        value = container.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _flatten_params(params) -> dict:
    if isinstance(params, dict) and isinstance(params.get("fields"), dict):
        merged = dict(params)
        for key, value in params["fields"].items():
            merged.setdefault(key, value)
        return merged
    return params


def _lookup_deep(params, resp, keys, relate: bool = False) -> object | None:
    for container in (params, resp):
        value = _lookup(container, keys)
        if value is not None:
            return value
        if isinstance(resp, dict):
            for nested in resp.values():
                if isinstance(nested, dict):
                    value = _lookup(nested, keys)
                    if value is not None:
                        return value
        if isinstance(container, dict):
            for nested in container.values():
                if isinstance(nested, dict):
                    value = _lookup(nested, keys)
                    if value is not None:
                        return value
    return None


def _fmt_money(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return f"{value:,.0f}".replace(",", " ") + " €"
    text = str(value).strip().lower()
    has_euro = ("eur" in text) or ("€" in text)
    digits = re.sub(r"[^0-9.,]", "", text)
    if not digits:
        return _trunc(text, 24)
    try:
        number = float(digits.replace(",", ".").replace(" ", ""))
    except ValueError:
        return _trunc(text, 24)
    return f"{number:,.0f}".replace(",", " ") + (" €" if has_euro else "")


def _fmt_size(value) -> str | None:
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if number < 1000:
            return f"{number:g} {unit}"
        number /= 1000
    return f"{number:g} Po"


def _base_label(action: str, domain: str) -> str:
    if domain in ("knowledge_kb", "general"):
        return "Réponse"
    lowered = action.lower()
    verb = next((label for suffix, label in _VERB_LABELS.items() if lowered.endswith(suffix)), None)
    resource = next((label for prefix, label in _RESOURCE_LABELS.items() if lowered.startswith(prefix)), None)
    if resource and verb:
        return f"{resource} {verb}"
    if resource:
        return resource
    if verb:
        return f"Action {verb}"
    return action or domain


def _is_create_action(action: str) -> bool:
    lowered = action.lower()
    return (".create" in lowered or lowered.endswith("create")
            or "ingest" in lowered or lowered in ("upload", ".upload", "kb.space.create"))


def _entity_parts(params, resp) -> tuple[str | None, str | None]:
    company = _lookup_deep(params, resp, _ENTITY_KEYS)
    name = _lookup_deep(params, resp, ("name", "firstname", "lastname", "contact_name", "display_name"))
    title = _lookup_deep(params, resp, ("title", "subject", "task_name", "event_name", "opportunity_name", "deal_name"))
    company = _trunc(company, 60) if company is not None else None
    name = _trunc(name, 60) if name is not None else None
    title = _trunc(title, 60) if title is not None else None
    if company and title:
        return company, title
    if company and name:
        return company, name
    if name and title and name != title:
        return name, title
    if company:
        return company, None
    if name:
        return name, None
    if title:
        return None, title
    return None, None


def _detail_segments(action: str, domain: str, params, resp) -> list[str]:
    segments: list[str] = []
    amount = _lookup_deep(params, resp, _AMOUNT_KEYS)
    money = _fmt_money(amount)
    if money:
        segments.append(money)

    email = _lookup_deep(params, resp, _EMAIL_KEYS)
    phone = _lookup_deep(params, resp, _PHONE_KEYS)
    if email is not None:
        segments.append(str(email))
    if phone is not None:
        segments.append(str(phone))

    stage = _lookup_deep(params, resp, _STAGE_KEYS)
    if stage is not None and str(stage).strip() not in ("", "1"):
        segments.append(f"Étape : {_trunc(stage, 40)}")

    if domain in ("email_action", "messaging") or action.startswith("mail.") or "email" in action:
        to = _lookup_deep(params, resp, _TO_KEYS)
        if to is not None:
            segments.append(f"À : {_trunc_list(to)}")
        subject = _lookup_deep(params, resp, _SUBJ_KEYS)
        if subject is not None and subject not in (amount, email, phone):
            segments.append(f"Objet : {_trunc(subject, 54)}")
        attachments = _lookup_deep(params, resp, ("attachments", "attachment_count", "nb_attachments"))
        if isinstance(attachments, (list, tuple)):
            attachments = len(attachments)
        if isinstance(attachments, int) and attachments > 0:
            segments.append(f"{attachments} pièce(s) jointe(s)")

    if domain in ("file_management", "office", "calendaring", "tasking"):
        filename = _lookup_deep(params, resp, _FILE_KEYS)
        if filename is not None:
            segments.append(_trunc(filename, 46))
        size = _lookup_deep(params, resp, _SIZE_KEYS)
        formatted_size = _fmt_size(size)
        if formatted_size:
            segments.append(formatted_size)

    return segments


def _describe_action(domain: str, action: str, entry: dict) -> tuple[str, str]:
    params = _flatten_params(entry.get("params") or {})
    data = entry.get("data") or {}
    resp = data.get("response", data) if isinstance(data, dict) else {}
    failed = isinstance(data, dict) and (data.get("ok") is False or bool(data.get("error")))
    error = (data.get("error") or "") if isinstance(data, dict) else ""

    if isinstance(resp, list):
        title = _base_label(action, domain)
        detail = f"{len(resp)} résultat(s)" if resp else "Aucun résultat"
        if failed:
            detail = "Échec — " + (_trunc(error, 120) if error else detail)
        return title, detail

    if domain in ("knowledge_kb", "general"):
        question = _lookup_deep(params, resp, ("question", "query", "message"))
        answer = _lookup_deep(params, resp, ("answer", "response_text", "text"))
        base = _base_label(action, domain)
        title = _trunc(question, 74) if question is not None else base
        detail = _trunc(answer, 160) if answer is not None else base
        if failed:
            detail = "Échec — " + _trunc(error or detail, 160)
        return title, detail

    entity, subject = _entity_parts(params, resp)
    base = _base_label(action, domain)
    if entity and subject and entity != subject:
        title = f"{entity} — {subject}"
    elif entity:
        title = f"{entity} — {base}"
    elif subject:
        title = subject
    else:
        title = base

    segments = _detail_segments(action, domain, params, resp)
    detail = " · ".join(segments)
    if _is_create_action(action):
        if detail:
            detail += " · créé depuis une conversation"
        else:
            detail = "créé depuis une conversation"
    if failed:
        detail = ("Échec — " if detail else "Échec — " + (_trunc(error, 120) if error else "")) + detail
    return title, detail


def _record_to_action_items(record: dict) -> list[dict]:
    """Expand one executor_log record into ActionLogItem[] entries."""
    domain = record.get("domain", "general")
    tool = domain_tool(domain)
    thread = record.get("conversation_id", "")
    created = record.get("ts") or _now_iso()
    results = record.get("results") or []
    items: list[dict] = []
    if not results:
        _, detail = _describe_action(domain, domain, {"params": record.get("data") or {}, "data": record.get("data") or {}})
        items.append({
            "id": f"{domain}:{record.get('status','unknown')}",
            "title": f"[{record.get('status','?')}] {domain}",
            "detail": detail or _summarize_data(record.get("data") or {}),
            "summary": _summarize_data(record.get("data") or {}),
            "tool": tool,
            "created_at": created,
            "source_thread_id": thread,
        })
    for entry in results:
        action = entry.get("action", "")
        title, detail = _describe_action(domain, action, entry)
        items.append({
            "id": f"{thread}:{action}" if thread else action,
            "title": title,
            "detail": detail or _summarize_data(entry.get("data") or {}),
            "summary": _summarize_data(entry.get("data") or {}),
            "tool": tool,
            "created_at": created,
            "source_thread_id": thread,
        })
    return items


def domain_tool(domain: str) -> str:
    return {
        "crm": "Odoo",
        "accounting": "Odoo",
        "sales": "Odoo",
        "hr": "Odoo",
        "contacts": "Odoo",
        "tasking": "Vikunja",
        "office": "OnlyOffice",
        "messaging": "Telegram",
        "email_action": "Gmail",
        "file_management": "Nextcloud",
        "calendaring": "Odoo",
        "knowledge_kb": "RAG",
        "general": "Réponse libre",
    }.get(domain, "Viito")


# ---------------------------------------------------------------------------
# Routes — Conversations
# ---------------------------------------------------------------------------
@router.get("/conversations")
def list_conversations():
    store = _get_store()
    conversations = [
        {
            "id": c["id"],
            "title": c.get("title", "Conversation"),
            "last_message": c.get("last_message", ""),
            "tool": c.get("tool", "Viito"),
            "time": c.get("time", ""),
        }
        for c in store["conversations"]
    ]
    # Most recent first (stable: equal timestamps keep store order).
    conversations.sort(key=lambda c: c.get("time") or "", reverse=True)
    return conversations


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    store = _get_store()
    conv = next((c for c in store["conversations"] if c["id"] == conversation_id), None)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": conv["id"], "messages": conv.get("messages", [])}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    store = _get_store()
    idx = next((i for i, c in enumerate(store["conversations"]) if c["id"] == conversation_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _mutate(lambda s: s["conversations"].pop(idx))
    return {"ok": True}


@router.post("/conversations")
def create_conversation(payload: CreateConversationRequest | None = None):
    context_item = (payload.context_item if payload else None) or {}
    conv_id = uuid.uuid4().hex[:12]
    title = context_item.get("title") or "Nouvelle conversation"
    tool = context_item.get("tool") or "Viito"
    new_conv = {
        "id": conv_id,
        "thread_id": conv_id,
        "title": title,
        "tool": tool,
        "context_item": context_item,
        "last_message": "",
        "time": _now_iso(),
        "created_at": _now_iso(),
        "messages": [],
    }
    _mutate(lambda s: s["conversations"].append(new_conv))
    return new_conv


@router.post("/conversations/{conversation_id}/messages")
def send_message(conversation_id: str, payload: SendMessageRequest):
    store = _get_store()
    conv = next((c for c in store["conversations"] if c["id"] == conversation_id), None)
    # Re-join a fresh conversation that was navigated to while offline ("/new").
    if not conv:
        conv = {
            "id": conversation_id,
            "thread_id": conversation_id,
            "title": f"Conversation {conversation_id[:6]}",
            "tool": "Viito",
            "context_item": {},
            "last_message": "",
            "time": _now_iso(),
            "created_at": _now_iso(),
            "messages": [],
        }
        _mutate(lambda s: s["conversations"].append(conv))

    file_note = ""
    if payload.attachments:
        names = ", ".join(a.filename for a in payload.attachments)
        file_note = f" [{names}]"
    user_msg = {"role": "user", "content": payload.text + file_note}
    _mutate(
        lambda s: next(
            (c for c in s["conversations"] if c["id"] == conversation_id), conv
        )["messages"].append(user_msg)
    )

    try:
        attachments = None
        if payload.attachments:
            attachments = [
                {"filename": a.filename, "content_type": a.content_type, "content": a.content_b64}
                for a in payload.attachments
            ]
        state = _run_pipeline(
            payload.text,
            thread_id=conv.get("thread_id") or conv["id"],
            user_id="webapp",
            attachments=attachments,
            history=_build_history(conv.get("messages") or [], payload.text),
        )
    except Exception as exc:
        # Honest failure: surface as a 502 so the UI shows the error bubble.
        eager = {
            "role": "assistant",
            "content": (
                "Oups, Viito n'a pas pu traiter la demande (pipeline indisponible). "
                f"Raison: {exc}"
            ),
            "meta": {"outcome": "failed"},
        }
        _mutate(
            lambda s: next(
                (c for c in s["conversations"] if c["id"] == conversation_id), conv
            )["messages"].append(eager)
        )
        raise HTTPException(status_code=502, detail={"error": str(exc), "message": eager})

    reply = _final_to_reply(state)

    # Persist assistant reply + any executor_log records into the action log.
    def _apply(s):
        target = next((c for c in s["conversations"] if c["id"] == conversation_id), conv)
        target["messages"].append(reply)
        target["last_message"] = state.get("final_response", {}).get("message", reply["content"])
        target["time"] = _now_iso()
        log = s.setdefault("action_log", [])
        for record in state.get("executor_log") or []:
            stamped = dict(record)
            stamped.setdefault("conversation_id", conversation_id)
            stamped.setdefault("ts", _now_iso())
            log.append(stamped)

    _mutate(_apply)
    return reply


# ---------------------------------------------------------------------------
# Routes — Voice transcription (Mistral Voxtral via OpenRouter)
# ---------------------------------------------------------------------------
@router.post("/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """Receive an audio blob, send to OpenRouter transcription endpoint, return text."""
    import base64
    import httpx

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY not configured")

    raw = await audio.read()
    if len(raw) < 100:
        raise HTTPException(status_code=400, detail="Audio trop court")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio trop volumineux (max 25 Mo)")

    filename = audio.filename or "recording.webm"
    fmt = (Path(filename).suffix.lstrip(".") or "webm").lower()
    if fmt in ("mp4", "m4a"):
        fmt = "m4a"
    model = os.environ.get("VIITO_ASR_MODEL", "qwen/qwen3-asr-1.7b").strip()
    fallback_model = os.environ.get("VIITO_ASR_FALLBACK_MODEL", "openai/whisper-1").strip()

    async def _post(m: str):
        return await client.post(
            "https://openrouter.ai/api/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": m, "input_audio": {"data": base64.b64encode(raw).decode("ascii"), "format": fmt}},
        )

    timeout = int(os.environ.get("VIITO_ASR_TIMEOUT", "120"))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await _post(model)
            # Transient gateway/provider failures (429/5xx) fall back to a
            # different, well-provisioned ASR model instead of failing the turn.
            if resp.status_code in (429,) or resp.status_code >= 500:
                resp = await _post(fallback_model)
    except httpx.ReadTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail=f"La transcription a dépassé le délai ({timeout} s). Réessayez avec un enregistrement plus court.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Erreur réseau du service de transcription : {exc}") from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Erreur de transcription ({resp.status_code}) : {resp.text[:300]}")

    data = resp.json()
    return {"text": data.get("text", "").strip()}


# ---------------------------------------------------------------------------
# Routes — File upload (for email attachments etc.)
# ---------------------------------------------------------------------------
_UPLOAD_DIR = Path(__file__).resolve().parent.parent / "memory" / "uploads"


@router.post("/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    """Receive one or more files, store them temporarily, return their IDs."""
    import base64 as b64

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    uploaded = []
    for f in files:
        raw = await f.read()
        file_id = uuid.uuid4().hex[:12]
        ext = Path(f.filename or "file").suffix
        saved_path = _UPLOAD_DIR / f"{file_id}{ext}"
        saved_path.write_bytes(raw)
        uploaded.append({
            "id": file_id,
            "filename": f.filename or f"file{ext}",
            "content_type": f.content_type or "application/octet-stream",
            "size": len(raw),
            "content_b64": b64.urlsafe_b64encode(raw).decode("ascii"),
        })
    return {"files": uploaded}


# ---------------------------------------------------------------------------
# Routes — Actions (executor_log)
# ---------------------------------------------------------------------------
@router.get("/actions/summary")
def actions_summary():
    store = _get_store()
    log = store.get("action_log", [])
    counts: dict[str, int] = {}
    for record in log:
        domain = record.get("domain", "general")
        n = len(record.get("results") or []) or 1
        counts[domain] = counts.get(domain, 0) + n
    return [{"domain": domain, "count": count} for domain, count in sorted(counts.items())]


@router.get("/actions/{domain}")
def actions_by_domain(domain: str):
    store = _get_store()
    log = store.get("action_log", [])
    items: list[dict] = []
    seen: dict[str, dict] = {}
    for record in log:
        if record.get("domain") != domain:
            continue
        for item in _record_to_action_items(record):
            key = f"{item.get('source_thread_id', '')}:{item.get('title', '')}"
            if key in seen:
                seen[key]["created_at"] = item["created_at"]
            else:
                seen[key] = item
                items.append(item)
    return items


# ---------------------------------------------------------------------------
# Gmail endpoints
# ---------------------------------------------------------------------------

def _get_gmail_executor():
    from server.execution.executors.registry import get_registry
    from server.execution.executors.gmail import GmailExecutor
    reg = get_registry()
    exec_ = reg.executor_for("email_action")
    if isinstance(exec_, GmailExecutor):
        return exec_
    raise HTTPException(status_code=503, detail="Gmail executor not available")


@router.get("/gmail/status")
def gmail_status():
    try:
        gmail = _get_gmail_executor()
    except HTTPException:
        return {"connected": False, "email": None, "configured": False}
    return {**gmail.get_status(), "configured": gmail._client_id != ""}


class GmailCredentialsRequest(BaseModel):
    client_id: str
    client_secret: str


@router.post("/gmail/credentials")
def gmail_save_credentials(payload: GmailCredentialsRequest):
    gmail = _get_gmail_executor()
    gmail.set_credentials(payload.client_id, payload.client_secret)
    return {"configured": True}


@router.get("/gmail/auth-url")
def gmail_auth_url():
    try:
        gmail = _get_gmail_executor()
    except HTTPException as e:
        raise HTTPException(status_code=e.status_code, detail="Gmail non disponible")
    if not gmail._client_id:
        raise HTTPException(status_code=400, detail="Veuillez d'abord enregistrer vos identifiants OAuth.")
    return {"url": gmail.get_auth_url()}


from fastapi.responses import RedirectResponse


@router.get("/gmail/callback")
def gmail_oauth_callback(code: str = "", error: str = ""):
    """Handle the OAuth2 callback from Google (GET redirect)."""
    if error:
        return RedirectResponse(url=f"/gmail/config?error={error}")
    if not code:
        return RedirectResponse(url="/gmail/config?error=no_code")
    try:
        gmail = _get_gmail_executor()
        result = gmail.exchange_code(code)
        if result.get("connected"):
            return RedirectResponse(url="/gmail/config?connected=1")
        return RedirectResponse(url="/gmail/config?error=exchange_failed")
    except Exception as exc:
        return RedirectResponse(url=f"/gmail/config?error={str(exc)[:100]}")


class GmailCodeRequest(BaseModel):
    code: str


@router.post("/gmail/callback")
def gmail_callback(payload: GmailCodeRequest):
    gmail = _get_gmail_executor()
    return gmail.exchange_code(payload.code)


@router.post("/gmail/disconnect")
def gmail_disconnect():
    gmail = _get_gmail_executor()
    return gmail.disconnect()


class GmailMessagesRequest(BaseModel):
    label: str = "inbox"
    query: str = ""
    max_results: int = 20


@router.post("/gmail/messages")
def gmail_messages(payload: GmailMessagesRequest):
    gmail = _get_gmail_executor()
    msgs = gmail._list_messages(
        label=payload.label,
        query=payload.query,
        max_results=payload.max_results,
    )
    return {"emails": msgs}


@router.get("/gmail/messages/{msg_id}")
def gmail_message_detail(msg_id: str):
    gmail = _get_gmail_executor()
    return gmail._get_message(msg_id)


@router.get("/gmail/drafts")
def gmail_drafts():
    gmail = _get_gmail_executor()
    return {"drafts": gmail._list_drafts()}


class GmailSendRequest(BaseModel):
    to: str
    subject: str = ""
    body: str = ""
    cc: str = ""


@router.post("/gmail/send")
def gmail_send(payload: GmailSendRequest):
    gmail = _get_gmail_executor()
    return gmail._send_message(
        to=payload.to,
        subject=payload.subject,
        body=payload.body,
        cc=payload.cc,
    )


@router.post("/gmail/trash/{msg_id}")
def gmail_trash(msg_id: str):
    gmail = _get_gmail_executor()
    return gmail._trash_message(msg_id)


@router.post("/gmail/untrash/{msg_id}")
def gmail_untrash(msg_id: str):
    gmail = _get_gmail_executor()
    return gmail._untrash_message(msg_id)


@router.get("/gmail/messages/{msg_id}/attachments/{att_id}")
def gmail_download_attachment(msg_id: str, att_id: str):
    from fastapi.responses import Response
    import base64
    gmail = _get_gmail_executor()
    result = gmail._download_attachment(msg_id, att_id)
    if "error" in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=result["error"])
    raw = base64.b64decode(result["data_b64"])
    return Response(
        content=raw,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment"},
    )


@router.get("/gmail/threads/{thread_id}")
def gmail_thread(thread_id: str):
    gmail = _get_gmail_executor()
    return gmail._get_thread(thread_id)


@router.delete("/gmail/drafts/{draft_id}")
def gmail_delete_draft(draft_id: str):
    gmail = _get_gmail_executor()
    return gmail._delete_draft(draft_id)


# ---------------------------------------------------------------------------
# File management (local filesystem)
# ---------------------------------------------------------------------------
from server.execution.executors.file_storage import FileStorage

_file_storage = FileStorage()


@router.get("/files")
def files_list(path: str = ""):
    return {"items": _file_storage.list_dir(path), "path": path}


@router.get("/files/info")
def files_info(path: str):
    info = _file_storage.get_info(path)
    if info is None:
        raise HTTPException(status_code=404, detail="File not found")
    return info


@router.post("/files/upload")
async def files_upload(files: list[UploadFile] = File(...), path: str = ""):
    results = []
    for f in files:
        content = await f.read()
        filename = f.filename or "upload"
        full_path = f"{path}/{filename}" if path else filename
        info = _file_storage.save_file(full_path, content)
        results.append(info)
    return {"uploaded": results}


@router.get("/files/download/{path:path}")
def files_download(path: str):
    content = _file_storage.read_file(path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    from fastapi.responses import Response
    info = _file_storage.get_info(path) or {}
    filename = info.get("name", path.rsplit("/", 1)[-1])
    return Response(
        content=content,
        media_type=info.get("mime", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/files/mkdir")
def files_mkdir(body: dict):
    folder = body.get("folder") or body.get("path") or ""
    if not folder:
        raise HTTPException(status_code=400, detail="folder name required")
    return _file_storage.mkdir(folder)


@router.post("/files/move")
def files_move(body: dict):
    src = body.get("source") or body.get("source_path") or ""
    dst = body.get("destination") or body.get("target_path") or ""
    if not src or not dst:
        raise HTTPException(status_code=400, detail="source and destination required")
    result = _file_storage.move(src, dst)
    if result is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return result


@router.post("/files/copy")
def files_copy(body: dict):
    src = body.get("source") or body.get("source_path") or ""
    dst = body.get("destination") or body.get("target_path") or ""
    if not src or not dst:
        raise HTTPException(status_code=400, detail="source and destination required")
    result = _file_storage.copy(src, dst)
    if result is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return result


@router.post("/files/rename")
def files_rename(body: dict):
    path = body.get("path") or ""
    new_name = body.get("new_name") or ""
    if not path or not new_name:
        raise HTTPException(status_code=400, detail="path and new_name required")
    result = _file_storage.rename(path, new_name)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found")
    return result


@router.get("/files/trash")
def files_trash():
    return {"items": _file_storage.list_trash()}


@router.post("/files/trash/restore")
def files_trash_restore(body: dict):
    name = body.get("name") or body.get("file_name") or ""
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    ok = _file_storage.restore_from_trash(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found in trash")
    return {"restored": name}


@router.delete("/files/trash/purge")
def files_trash_purge():
    count = _file_storage.purge_trash()
    return {"purged": count}


@router.get("/files/shares")
def files_shares():
    return {"shares": _file_storage.list_shares()}


@router.post("/files/shares")
def files_share_create(body: dict):
    path = body.get("path") or ""
    recipient = body.get("recipient") or body.get("recipient_email") or ""
    if not path or not recipient:
        raise HTTPException(status_code=400, detail="path and recipient required")
    return _file_storage.create_share(path, recipient)


@router.delete("/files/shares/{share_id}")
def files_share_delete(share_id: str):
    ok = _file_storage.delete_share(share_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Share not found")
    return {"deleted": share_id}


def _send_shared_file_email(
    info: dict, raw: bytes | None, recipients: list[str], subject: str, content: str
) -> dict:
    """Send the file by email through the registered email_action executor
    (Gmail with attachment, Stalwart/JMAP with a blob upload, fallback text)."""
    import base64
    from server.execution.executors.registry import get_registry
    from server.execution.executors.gmail import GmailExecutor
    from server.execution.executors.stalwart import StalwartExecutor

    exec_ = get_registry().executor_for("email_action")
    attachments = None
    if raw is not None:
        attachments = [{
            "filename": info.get("name") or "file",
            "content": base64.urlsafe_b64encode(raw).decode("ascii"),
            "content_type": info.get("mime") or "application/octet-stream",
        }]
    if isinstance(exec_, GmailExecutor):
        result = exec_._send_message(
            ", ".join(recipients), subject, content, attachments=attachments
        )
        return {"provider": "gmail", **result}
    if isinstance(exec_, StalwartExecutor):
        result = exec_._send_email({
            "recipients": recipients,
            "subject": subject,
            "content": content,
            "attachments": attachments or [],
        })
        return {"provider": "stalwart", **result}
    if raw is None:
        return {"provider": None, "sent": False, "reason": "folder (no attachment support)"}
    return {"provider": None, "sent": False, "reason": "no email backend configured"}


@router.post("/files/shares/email")
def files_share_by_email(body: dict):
    path = body.get("path") or ""
    recipients = body.get("recipients") or []
    message = body.get("message") or ""
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    if isinstance(recipients, str):
        recipients = [p.strip() for p in recipients.split(",") if p.strip()]
    else:
        recipients = [str(r).strip() for r in recipients]
    recipients = [r for r in recipients if r]
    if not recipients:
        raise HTTPException(status_code=400, detail="recipients required")

    info = _file_storage.get_info(path)
    if info is None:
        raise HTTPException(status_code=404, detail="File not found")

    shares = [_file_storage.create_share(path, recipient) for recipient in recipients]
    subject = "tayssir shared this file"
    content = message or (
        f"Bonjour,\n\nTayssir vous a partagé le fichier « {info.get('name')} » "
        f"depuis le gestionnaire de fichiers.\nVous trouverez ce fichier en pièce jointe.\n\nCordialement."
    )
    raw = _file_storage.read_file(path) if info.get("type") != "folder" else None
    try:
        email_result = _send_shared_file_email(info, raw, recipients, subject, content)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Partage créé, mais envoi par email impossible : {exc}",
        )
    return {"shares": shares, "subject": subject, "email": email_result}


@router.delete("/files/{path:path}")
def files_delete(path: str):
    ok = _file_storage.delete(path, trash=True)
    if not ok:
        raise HTTPException(status_code=404, detail="File not found")
    return {"deleted": path}
