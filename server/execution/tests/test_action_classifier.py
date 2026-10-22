"""Rule-based action classifier fallback tests (offline, no LLM).

Verifies that calendaring "modify" phrasings route to event.modify/event.update
instead of event.create, and that delete-all phrasings stay on event.delete.
"""

from __future__ import annotations

import json

from server.agents.action_classifier import (
    ACTION_IDS_BY_DOMAIN,
    RuleBasedActionClassifierClient,
    catalog_for_domains,
)


def _build_prompt(intent: str, domains: list[str]) -> str:
    catalog: list[str] = list(catalog_for_domains(domains))
    lines = [f"Domaine(s) détecté(s) : {', '.join(domains)}", "", "Catalogue d'actions autorisées :"]
    for domain in domains:
        actions = [action_id for action_id in catalog if action_id in ACTION_IDS_BY_DOMAIN.get(domain, ())]
        if not actions:
            continue
        lines.append(f"{domain} :")
        for action_id in actions:
            lines.append(f"- {action_id}")
        lines.append("")
    lines.append(f"Demande : {intent}")
    return "\n".join(lines)


def _predict(intent: str, domains: list[str] = ("calendaring",)) -> list[str]:
    client = RuleBasedActionClassifierClient()
    raw = client.complete(_build_prompt(intent, domains))
    payload = json.loads(raw)
    return payload.get("actions", [])


def test_calendaring_modifier_la_date_routes_to_event_modify() -> None:
    actions = _predict("modifie la date de la reuinion equipe dev vers le 21 septembre a 9 heures")
    assert "event.modify" in actions


def test_calendaring_change_date_routes_to_event_modify() -> None:
    actions = _predict("change la date de la réunion hebdo à demain 10h")
    assert "event.modify" in actions


def test_calendaring_reporter_l_evenement_routes_to_event_modify() -> None:
    actions = _predict("reporter l'evenement a vendredi 14h")
    assert "event.modify" in actions


def test_calendaring_modifier_reunion_never_event_create() -> None:
    actions = _predict("modifie la réunion à lundi 9h")
    assert "event.modify" in actions


def test_calendaring_supprimer_tous_routes_to_event_delete() -> None:
    actions = _predict("supprime tous les réunions")
    assert "event.delete" in actions


def test_contacts_creer_un_nouveau_contact_routes_to_res_partner_create() -> None:
    actions = _predict("créer un nouveau contact ahmed mansour avec le mail ahemed@mansour.com", ("contacts",))
    assert "res.partner.create" in actions


def test_contacts_contact_creation_with_conversation_keeps_res_partner_create() -> None:
    intent = ("créer un nouveau contact ahmed mansour de la société techinno, "
              "créer une nouvelle conversation telegram avec lui et envoie un message de bienvenu")
    actions = _predict(intent, ("contacts",))
    assert "res.partner.create" in actions


def test_predict_falls_back_to_rules_when_llm_returns_empty(monkeypatch) -> None:
    """When the LLM keeps returning an empty/invalid action list, the
    deterministic rules must still emit res.partner.create."""
    import json

    from server.agents.action_classifier import ActionClassifierAgent

    class EmptyClient:
        def __call__(self, prompt, system_prompt=None):
            return json.dumps({"actions": []})

    monkeypatch.setattr("server.agents.action_classifier.time.sleep", lambda seconds: None)
    agent = ActionClassifierAgent(model_client=EmptyClient(), use_domain_pipeline=False)
    prediction = agent.predict(
        "créer un nouveau contact ahmed mansour avec le mail ahemed@mansour.com de la soc",
        domains=["contacts"],
    )
    assert "res.partner.create" in prediction.predicted_actions


def test_generated_paragraph_uses_general_then_office() -> None:
    from server.agents.domain_classifier2 import _augment_generated_document_domains

    assert _augment_generated_document_domains(
        "ajouter un nouveau paragraphe qui parle de l'innovation technologique 2026",
        ["office"],
    ) == ["general", "office"]


def test_generated_paragraph_reorders_general_before_office() -> None:
    from server.agents.domain_classifier2 import _augment_generated_document_domains

    assert _augment_generated_document_domains(
        "ajouter un paragraphe qui parle des tendances 2026",
        ["office", "general"],
    ) == ["general", "office"]


def test_generated_paragraph_is_classified_as_redaction() -> None:
    actions = _predict(
        "ajouter un nouveau paragraphe qui parle de l'innovation technologique 2026",
        ("general",),
    )
    assert actions == ["redaction"]