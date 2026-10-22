from __future__ import annotations

import importlib.util
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from server.config import load_environment
from server.agents.domain_classifier import (
    DomainClassifierAgent,
    DomainClassifierClient,
)
from server.agents.action_classifier import (
    _DOMAIN_TOOLS,
    ACTION_IDS_BY_DOMAIN,
    ActionClassifierAgent,
    normalize_action,
)

load_environment()

_GENERATOR_PATH = Path(__file__).resolve().parents[2] / "tools" / "gen_attribute_spec.py"


def _load_generator_module():
    spec = importlib.util.spec_from_file_location("gen_attribute_spec", _GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GEN = _load_generator_module()

# {domain: {action: schema}} — single source of truth for extraction prompts.
ALL_ACTIONS: dict[str, dict[str, dict]] = _GEN.parse_all_actions()

_FIELD_KIND_LABELS = {
    "entity": " (entite → texte exact tel qu'ecrit, sans prefixe ni determinant)",
    "target": " (objet de l'action → texte exact, sans prefixe ni determinant)",
    "extract": " (valeur a extraire telle qu'ecrite dans la demande)",
}


def schema_for_action(action_id: str) -> dict | None:
    for domain_actions in ALL_ACTIONS.values():
        schema = domain_actions.get(action_id)
        if schema is not None:
            return schema
    return None


def collect_extractable_fields(schema: dict) -> dict[str, tuple[str, str]]:
    """Return {fname: (ftype, kind)} for non-system fields, in deterministic order."""
    return {
        fname: (ftype, kind)
        for fname, (ftype, kind) in _GEN.collect_fields(schema).items()
        if kind != "system"
    }


# Lookup/delegation actions that belong to a primary (home) domain. They are
# also listed in other domain catalogs as a "délégation" (e.g. accounting
# exposes res.partner.search_read to resolve a partner id for a payment), but
# when the home domain is part of the request the action must be extracted
# there, with the home domain schema, so the terminal envelope stays coherent
# (res.partner.search_read → contacts, never accounting).
HOME_DOMAIN_BY_ACTION: dict[str, str] = {
    "res.partner.search_read": "contacts",
}

# Field name aliases: maps GT/alias field names to canonical schema field names.
# When the model predicts an alias name, it gets remapped to the schema name
# before validation. When the eval scores, both sides are canonicalized.
FIELD_NAME_ALIASES: dict[str, dict[str, str]] = {
    "res.partner.search_read": {"query": "search_query"},
    "crm.lead.create": {"assigne": "contact", "revenue": "expected_revenue"},
    "office.editor.insert_image": {"document": "target_doc","image_url":"image_url"},
    "tasking.task.update": {"month": "due_month"},
    "event.create": {
        "meeting_day": "start_day",
        "meeting_month": "start_month",
        "meeting_year": "start_year",
        "start_time": "start_hour",
    },
    "event.modify": {
        "meeting_day": "start_day",
        "meeting_month": "start_month",
        "meeting_year": "start_year",
        "start_time": "start_hour",
    },
    "event.update": {
        "meeting_day": "start_day",
        "meeting_month": "start_month",
        "meeting_year": "start_year",
        "start_time": "start_hour",
    },
}

# Curated (query, example_fields) overrides for a few actions whose synthetic
# example is noisy (e.g. the generator fills `name` with the search term for a
# lookup). Fields are filtered against the domain schema, so a curated example
# only applies where its fields are valid (search_query exists in the contacts
# res.partner.search_read schema, not in the accounting delegation).
_CURATED_ATTRIBUTE_EXAMPLES: dict[str, tuple[str, dict]] = {
    "res.partner.search_read": (
        "Cherche le contact Karim Belhaj.",
        {"search_query": "Karim Belhaj"},
    ),
    "res.partner.write": (
        "Modifie l'adresse du contact Emma Rousseau a ariana, Tunisie.",
        {"name": "Emma Rousseau", "name": "Emma Rousseau", "city": "ariana", "country": "tunisie"},
    ),
    "res.partner.create": (
        "Cree la fiche contact de Sophie Durand, email sophie@test.fr.",
        {"name": "Sophie Durand", "email": "sophie@test.fr"},
    ),
    # "tasking.task.create" — replaced by richer example below (with project)
    "event.create": (
        "Cree la reunion ce soir avec l'equipe",
        {"title": "reunion", "start_date": "ce soir", "participants": "l'equipe"},
    ),
    "event.modify": (
        "Modifie la date de la reunion TechNova au 21 septembre a 9h.",
        {"title": "TechNova", "start_day": "21", "start_month": "septembre", "start_hour": "9"},
    ),
    "event.update": (
        "Reporte la reunion equipe dev a lundi 14h.",
        {"title": "equipe dev", "start_day": "lundi", "start_hour": "14"},
    ),
    "calendar.create": (
        "Cree un calendrier 'Projet demenagement bureaux'.",
        {"name": "Projet demenagement bureaux"},
    ),
    "crm.lead.action_archive": (
        "Archive le lead Dupont & Fils.",
        {"lead": "Dupont & Fils"},
    ),
    "crm.lead.action_set_won": (
        "Marque le lead TechNova comme gagne.",
        {"lead": "TechNova"},
    ),
    "crm.lead.convert_opportunity": (
        "Convertis le lead BatiNord en opportunite.",
        {"lead": "BatiNord"},
    ),
    "crm.lead.action_schedule_meeting": (
        "Planifie une reunion avec le lead Cora Group vendredi.",
        {"lead": "Cora Group"},
    ),
    "hr.applicant.action_create_meeting": (
        "Planifie un entretien avec le candidat Marie Lambert.",
        {"applicant": "Marie Lambert"},
    ),
    "hr.applicant.action_send_email": (
        "Envoie un email au candidat Lucas Girard.",
        {"applicant": "Lucas Girard"},
    ),
    "hr.applicant.archive_applicant": (
        "Archive la candidature de Jean Dupont",
        {"applicant": "Jean Dupont"},
    ),
    "hr.expense.action_pay": (
        "Paye la note de frais de Jean Dupont",
        {"expense": "Jean Dupont"},
    ),
    "hr.leave.action_approve": (
        "Approuve le conge de Marie Curie apartir de 1 septembre pour une periode de 3 jours",
        {"employee":"Marie Curie","start_day": "1","start_month":"septembre","period":"3"},
    ),
    "hr.expense.action_approve": (
        "Approuve la note de frais 'frais 2026' de Paul Martin",
        {"employee": "Paul Martin","expense":"frais 2026"},
    ),
    "hr.attendance.action_approve_overtime": (
        "Approuve le pointage de Sophie Durand de 15 heurs",
        {"employee": "Sophie Durand","period":"15"},
    ),
    "kb.answer.grounded": (
        "Quel est le bareme interne ?",
        {"question": "Quel est le bareme interne ?"},
    ),
    # "kb.ingest.document" — replaced by richer example below (with space + multiple docs)
    "kb.search.vector": (
        "Recherche les documents sur le budget 2026.",
        {"search_query": "budget 2026"},
    ),
    "email.email_submission.set": (
        "Envoie la facture devis_2026-118.pdf a Jean Dupont depuis contact@entreprise.fr en pience jointe.",
        {"recipients": "Jean Dupont", "mailbox": "contact@entreprise.fr", "attachment_names": "devis_2026-118.pdf"},
    ),
    "email.blob.get": (
        "Recupere la piece jointe de l'email envoye a ahmed@gmail.com.",
        {"recipients": "ahmed@gmail.com"},
    ),
    "email.blob.lookup": (
        "Cherche l'email envoye a mohamed@gmail.com.",
        {"recipients": "mohamed@gmail.com"},
    ),
    "tasking.assignee.add": (
        "Assigne Jean Dupont a la tache Relancer Acme.",
        {"task": "Relancer Acme", "assignee": "Jean Dupont"},
    ),
    "email.blob.copy":(
        "copie la piece jointe du mail envoyé a Jean Dupont le 1 aout",
        {"recipient":"Jean Dupont","day":"1","month":"aout"}
    ),
    "tasking.assignee.bulk": (
        "Assigne Jean Dupont et Marie Durand a la tache Relancer Acme.",
        {"task":"Relancer Acme","assignees": ["Jean Dupont", "Marie Durand"]},
    ),
    "tasking.task.create": (
        "Cree la tache 'Relire le contrat Solenco' pour le projet Gestion Contrats jusqu'a 25 septembre.",
        {"task": "Relire le contrat Solenco", "project": "Gestion Contrats","due_day":"25","due_month":"septembre"},
    ),
    "office.callback.force_save": (
        "Force l'enregistrement du document note_interne.docx.",
        {"document": "note_interne.docx"},
    ),
    "office.command.info":(
        "Recupere les informations du document rapport_annuel.pdf.",
        {"document":"rapport_annuel.pdf"},
    ),
    "office.converter.convert":(
      "convertis doc.docx en tant que  pdf avec le nom doc.pdf",
      {"document":"doc.docx","target_format":"pdf","target_name":"doc.pdf"}
    ),
    "office.editor.add_content":(
        "Ajoute 'ceci est une lettre de motivation' en rouge au debut du document file.docx",
        {"target_doc":"file.docx","text":"ceci est une lettre de motivation","position":"debut du document","text_color":"rouge"}
    ),
    "office.editor.create":(
        "Cree un fichier test.docx dans le dossier test contenant test creation fichier.",
        {"name":"test.docx","folder":"test","content":"test creation fichier"}
    ),
    "office.editor.edit_content":(
        "modifie le mot buziness en business du fichier mon_fichier.xlsx",
        {"target_doc":"mon_fichier.xlsx","text":"buziness","target_text":"business"}
    ),
 "office.editor.insert_table":(
     "ajouter un tableau de deux colonne col1, col2 et deux lignes vides dans le fichier mon_fichier.docx",
     {"target_doc":"mon_fichier.docx","cols_num":"2","raws_num":"2","colomns_titles":["col1","col2"]}
 ),

    "office.editor.modify_table":(
        "ajouter une nouvelle ligne C, 300 dans le tableau du fichier mon_fichier.xlsx",
        {"target_doc":"mon_fichier.xlsx","rows_data":[["C","300"]]}
    ),

    "event.free_busy": (
        "Quelle est ma diponiblité à partir de 12 aout  ?",
        {"from_day": "12","from_month":"aout"},
    ),
    "event.delete": (
        "Supprime la reunion 'TechNova meeting' de mardi 14 aout.",
        {"title":"TechNova meeting","start_day":"14","start_month":"aout"},
    ),
    "event.reminder": (
        "Programme une alerte 30 minutes avant la Reunion 'dev daily meeting'.",
        {"minutes_before": "30", "event": "dev daily meeting"},
    ),
    "event.recurrence": (
        "Mets en place une reunion 'dev daily meeting' hebdomadaire qui se repete 12 fois.",
        {"title":"dev daily meeting","recurrence": "hebdomadaire", "count": "12"},
    ),
    "kb.ingest.document": (
        "Ajoute note_interne.docx et rapport_annuel.pdf dans l'espace marketing.",
        {"document_names": ["note_interne.docx", "rapport_annuel.pdf"], "space": "marketing"},
    ),
    "kb.generate.report": (
        "Genere un rapport sur les risques",
        {"subject": "les risques"},
    ),
    "kb.generate.image": (
        "Genere une infographie sur les ventes",
        {"subject": "les ventes", "format": "infographie"},
    ),
    "kb.note.create": (
        "Cree une note intitulee Compte-rendu reunion avec le contenu La reunion a porte sur le budget 2026 et taggee budget, 2026.",
        {"title": "Compte-rendu reunion", "content": "La reunion a porte sur le budget 2026", "tags": ["budget", "2026"]},
    ),
    "kb.search.fulltext": (
        "Recherche les documents contenant le terme budget 2026 dans l'espace finance avec une limite de 10 resultats.",
        {"search_query": "budget 2026", "space": "finance", "limit": "10"},
    ),
    "email.email.copy": (
        "Copie l'email de Medisys vers la boite Archives.",
        {"email": "Medisys", "mailbox": "Archives"},
    ),
    "email.sieve_script.set": (
        "Cree un filtre Spam pour les emails de LogiTrans.",
        {"filter_name": "Spam", "filter": "LogiTrans"},
    ),
    "sale.order.action_confirm": (
        "Confirme la commande 'comande technova'",
        {"order": "comande technova"},
    ),
    "sale.order.line.action_add_from_catalog": (
        "Ajoute un produit a la commande Dupont",
        {"order": "Dupont"},
    ),
    "general": (
        "Bonjour s'il te plaît.",
        {"message": "Bonjour s'il te plaît."},
    ),
    "conseil professionnel - marketing": (
        "comment grandire mon image linkedin?",
        {"message": "comment grandire mon image linkedin?"},
    ),
    "traduction": (
        "Traduis ce paragraphe en français.",
        {"target_language": "français"},
    ),
    "redaction": (
        "Récris ce paragraphe pour qu'il soit plus formel.",
        {"tone": "formel","message":"Récris ce paragraphe pour qu'il soit plus formel."},
    ),
    "messaging.conversation.create": (
        "Crée une conversation avec Emma Rousseau.",
        {"contact": "Emma Rousseau"},
    ),
    "messaging.message.send": (
        "Envoie 'Bonjour' à @emma dans la conversation Dupont & Fils.",
        {"conversation": "Dupont & Fils", "message_text": "Bonjour"},
    ),
    "messaging.message.update": (
        "Modifie mon dernier message dans la conversation Acme avec le texte Nouvelle version.",
        {"conversation": "Acme", "message": "dernier message", "message_text": "Nouvelle version"},
    ),
    "messaging.message.delete": (
        "Supprime le message numero 12 dans la conversation Acme.",
        {"conversation": "Acme", "message": "12"},
    ),
    "messaging.conversation.delete": (
        "Supprime la conversation Acme.",
        {"conversation": "Acme"},
    ),
    "messaging.conversation.mute": (
        "Coupe les notifications de la conversation Dupont & Fils.",
        {"conversation": "Dupont & Fils"},
    ),
    "messaging.conversation.unmute": (
        "Reactive les notifications de la conversation Dupont & Fils.",
        {"conversation": "Dupont & Fils"},
    ),
    "messaging.conversation.participants": (
        "Ajoute Karim Benali a la conversation BureauPlus.",
        {"conversation": "BureauPlus", "participants": "Karim Benali"},
    ),
    "messaging.group.create": (
        "Crée un groupe 'Projet Alpha' avec @emma et @karim.",
        {"title": "Projet Alpha", "participants": ["@emma", "@karim"]},
    ),
    "messaging.channel.create": (
        "Crée un canal 'Actu' et publie Bonjour.",
        {"title": "Actu", "message_text": "Bonjour"},
    ),
}

_EMAIL_RECIPIENT_FIELDS = {"recipient", "recipients", "to", "cc", "bcc"}
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ATTACHMENT_PATTERN = re.compile(r"\b[^\s,;:]+(?:\.[A-Za-z0-9]{2,5})\b")


def partition_actions_by_domain(domains: list[str], actions: list[str]) -> dict[str, list[str]]:
    """Assign each action to a domain, preferring the home domain for shared
    lookup actions (see HOME_DOMAIN_BY_ACTION), then the first requested domain
    that owns it. Ownership follows ACTION_IDS_BY_DOMAIN (the canonical catalog
    of all 13 domains, including general and knowledge_kb). Actions shared by
    several domains (same schema) are handled once, under the home domain, so a
    duplicated action never triggers a second model call or a duplicate field
    entry. Preserves the requested action order. Returns only the domains that
    ended up with actions.
    """
    result: dict[str, list[str]] = {domain: [] for domain in domains}
    assigned: set[str] = set()
    domain_set = set(domains)
    for action_id in actions:
        if action_id in assigned:
            continue
        home = HOME_DOMAIN_BY_ACTION.get(action_id)
        if home in domain_set:
            result[home].append(action_id)
            assigned.add(action_id)
            continue
        for domain in domains:
            if action_id in ACTION_IDS_BY_DOMAIN.get(domain, ()):
                result[domain].append(action_id)
                assigned.add(action_id)
                break
    return {domain: acts for domain, acts in result.items() if acts}


def _actions_block(domain: str, actions: list[str]) -> str:
    lines: list[str] = []
    for action_id in actions:
        schema = ALL_ACTIONS.get(domain, {}).get(action_id)
        if schema is None:
            continue
        fields = collect_extractable_fields(schema)
        if not fields:
            lines.append(f"- {action_id} : aucun champ à extraire")
            continue
        labels = [f"{fname}{_FIELD_KIND_LABELS.get(kind, '')}" for fname, (_ftype, kind) in fields.items()]
        lines.append(f"- {action_id} : {', '.join(labels)}")
    return "\n".join(lines)


def _examples_block(domain: str, actions: list[str], max_examples: int = 3) -> str:
    lines: list[str] = []

    for action_id in actions[:max_examples]:
        schema = ALL_ACTIONS.get(domain, {}).get(action_id)
        if schema is None:
            continue
        curated = _CURATED_ATTRIBUTE_EXAMPLES.get(action_id)
        if curated is not None:
            query, example_fields = curated
            allowed = collect_extractable_fields(schema)
            example_fields = {f: v for f, v in example_fields.items() if f in allowed}
            if not example_fields:
                curated = None
        if curated is None:
            query, example_fields = _GEN.build_example(action_id, schema, domain)
        response = json.dumps({action_id: {"fields": example_fields}}, ensure_ascii=False)
        lines.append(f"Demande : « {query} »")
        lines.append(f"Réponse : {response}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _domain_guidance(domain: str) -> str:
    if domain == "calendaring":
        return (
            "Rappel calendaring : les valeurs de date/heure ne doivent pas être fusionnées dans un seul "
            "champ; sépare exactement la date et l'heure quand la demande les donne séparément. "
            "Pour un titre d'événement, garde le nom ou l'entreprise, pas un mot générique comme 'réunion'."
        )
    if domain == "email_action":
        return (
            "Rappel email : n'invente jamais des destinataires ni des boîtes. Si la demande donne "
            "un expéditeur ou des destinataires dans le texte, remplit exactement ces champs sans préfixe. "
            "Le champ destinataire peut être un nom de personne ou une adresse email; s'il y a plusieurs "
            "adresses email, renvoie-les sous forme de liste JSON dans « recipients »."
        )
    if domain == "messaging":
        return (
            "Rappel messaging : la personne ou le chat ciblé doit ABSOLUMENT être extrait. Pour "
            "« créer/discuter avec <une personne> », mets son nom ou @username dans « contact » "
            "(ou « title »). Pour un chat cible, mets son nom/titre dans « conversation ». "
            "Pour un envoi, « message_text » contient le message à envoyer."
        )
    if domain == "tasking":
        return (
            "Rappel tasking : si la demande crée un nouveau projet PUIS une tâche ou une colonne dans "
            "le même tour, renvoie le titre exact du projet dans le champ « project » de CHAQUE action "
            "qui en dépend (task.create, bucket.create, view.create) — pas seulement dans « title » "
            "du projet. Ex. « crée le projet X et la tâche Y » → task.create.project: « X », "
            "task.create.task: « Y »."
        )
    return ""


_ATTRIBUTE_PROMPT_TEMPLATE = """# Rôle

Tu es le module d'extraction d'attributs de Viito pour le domaine {domain} ({tool}).
Tu reçois la demande de l'utilisateur et la liste des actions de CE domaine à exécuter, avec les
champs à extraire pour chacune. Ta seule tâche : extraire, depuis la demande, les valeurs des
champs de chaque action. Tu ne réponds JAMAIS à la demande : tu ne fais qu'extraire des attributs.

# Format de sortie (obligatoire)

Réponds UNIQUEMENT avec un objet JSON valide, sans texte avant ni après, sans balise markdown,
sans explication :

{{"<action_id>": {{"fields": {{"<champ>": <valeur>}}}}}}

# Règles de valeur (CRITIQUE — erreurs fréquentes à éviter)

1. NE JAMAIS ajouter de préfixe descriptif ou de déterminant. Extrais UNIQUEMENT le texte brut de
   l'entité nommée telle qu'elle apparaît dans la demande.
   - « la facture de Solenco » → « Solenco » (PAS « facture Solenco », PAS « la facture Solenco »)
   - « le contact Jean Dupont » → « Jean Dupont » (PAS « contact Jean Dupont »)
   - « la tâche Refonte site web » → « Refonte site web » (PAS « tâche Refonte site web »)
   - « le devis DEV-2026-042 » → « DEV-2026-042 » (PAS « devis DEV-2026-042 »)
   - « le congé de Marie Curie » → « Marie Curie » (PAS « congé de Marie Curie »)
   - « la note de frais de Paul Martin » → « Paul Martin » (PAS « note de frais de Paul Martin »)
   - « le pointage de Sophie Durand » → « Sophie Durand » (PAS « pointage de Sophie Durand »)

2. NE JAMAIS inventer un nom de champ. Utilise UNIQUEMENT les champs listés pour chaque action.
   Chaque champ a un nom précis — ne le remplace pas par un synonyme ni par un champ d'une autre action.
   - « journal » ≠ « customer » ≠ « supplier » ≠ « partner »
   - « expense » ≠ « employee » ≠ « leave »
   - « title » ≠ « attendees » ≠ « subject »

3. Copie EXACTEMENT le texte tel qu'écrit, sans reformuler ni traduire.
   - « 25 000 € » → « 25 000 € » (PAS « 25000.0 »)
   - « Cora Group » → « Cora Group » (PAS « la société Cora Group »)

4. Pour CHAQUE action listée, extrais TOUS les champs dont la valeur apparaît dans la demande.
   Ne laisse JAMAIS un champ vide si sa valeur est clairement mentionnée. Si tu vois un nom de
   personne, un nom de fichier, un nom d'entreprise, une date, un montant, etc., extrais-le dans
   le champ correspondant.

5. Pour les titres d'événements et de tâches, privilégie le nom de la personne ou de l'entreprise
   plutôt que le type d'objet générique :
   - « Planifie une réunion avec Verdeco » → title: « Verdeco » (PAS « réunion »)
   - « crée une tâche de suivi pour LogiTrans » → title: « LogiTrans » (PAS « suivi »)
   - « confirme la commande de BatiNord » → order: « BatiNord » (PAS « commande »)
   - « Valide la note de frais de Jean Dupont » → invoice: « note de frais de Jean Dupont »

6. QUAND PLUSIEURS actions sont listées, traite CHAQUE action INDIVIDUELLEMENT.
   Ne saute JAMAIS une action même si elle semble secondaire. La demande peut contenir des
   informations pour TOUTES les actions — analyse la demande entière pour CHACUNE d'entre elles.

7. Quand la demande fournit une date et une heure séparément, n'écris pas une seule chaîne comme
    "lundi 10h" dans un seul champ si le schéma attend date et heure distincts. Sépare les valeurs
    selon le champ exact, sans ajouter de mots parasites.

8. Si une valeur est absente ou non explicite, ne la devine pas. Un champ omis vaut mieux qu'un
    champ inventé. Pour chaque action, n'ajoute que des champs réellement documentés dans la demande.

9. Pour un montant avec une devise (ex. « 14500 EUR », « 25 000 € », « 120 USD ») : si l'action
    déclare séparément un champ de montant (type « amount »/« number », ex. « amount ») ET un champ
    « currency », mets UNIQUEMENT le chiffre dans le champ de montant (ex. « 14500 ») et le code de
    devise dans « currency » (ex. « EUR », « USD »). Ne mélange JAMAIS la devise avec le montant dans
    un seul champ.

# Règles de sortie

- N'émets QUE les actions listées dans la demande de CE domaine, avec l'action_id recopié TEL QUEL.
- N'inclus QUE les champs listés pour chaque action.
- Champ marqué "entité" : renvoie le TEXTE exact de l'entité nommée dans la demande, sans préfixe.
- Champ marqué "objet de l'action" : renvoie l'objet désigné par la demande, sans déterminant.
- Ne renvoie JAMAIS de valeurs système (identifiants internes, clés, jetons, auth, endpoint).
- Ne remplis un champ que si la demande contient réellement sa valeur. Champ non mentionné → omis.
- Renvoie TOUTES les actions listées, sans exception : une action listée ne doit JAMAIS être absente
  du JSON de sortie. Pour une action sans valeur extractible, renvoie {{"<action_id>": {{"fields": {{}}}}}}.
- Un seul JSON par réponse : l'objet complet, jamais par action.
- Pour les champs de type liste (ex: document_names, recipients quand plusieurs emails sont donnés),
  utilise un tableau JSON valide (ex: ["a@x.com", "b@y.com"]).

# Auto-vérification (OBLIGATOIRE avant de répondre)

Vérifie CHAQUE action de ta sortie :
1. L'action est-elle bien présente dans le JSON ? (jamais oubliée)
2. Pour CHAQUE champ listé de cette action, as-tu vérifié si sa valeur apparaît dans la demande ?
3. As-tu retiré tout préfixe/déterminant des valeurs extraites ?
4. Les valeurs sont-elles copiées EXACTEMENT telles qu'écrites dans la demande ?
5. Pour les titres d'événements/tâches : as-tu utilisé le nom de la personne/entreprise plutôt que le type d'objet générique ?
6. As-tu traité TOUTES les actions listées, même celles qui semblent secondaires ?
7. Pour email.email_submission.set : as-tu rempli « mailbox » si la demande mentionne une adresse email d'expéditeur (« depuis X@Y.fr ») ?
8. S'il y a plusieurs adresses destinataires, les as-tu mises dans « recipients » sous forme de liste JSON ?

Sortie = UNIQUEMENT le JSON {{"<action_id>": {{"fields": {{...}}}}}}. Rien d'autre.
"""

ATTRIBUTE_SYSTEM_PROMPTS_BY_DOMAIN: dict[str, str] = {
    domain: _ATTRIBUTE_PROMPT_TEMPLATE.format(
        domain=domain,
        tool=_DOMAIN_TOOLS.get(domain, "Viito"),
    )
    for domain in sorted(ALL_ACTIONS)
}


def build_attributes_prompt(
    intent: str, domain: str, actions: list[str], history: str | None = None
) -> str:
    lines = [f"Domaine : {domain}", ""]
    lines.append(f"Actions à traiter ({domain}) :")
    block = _actions_block(domain, actions)
    lines.append(block if block else "- aucune action avec des champs à extraire")
    lines.append("")
    examples = _examples_block(domain, actions)
    if examples:
        lines.append(f"Exemples ({domain}) :")
        lines.append("")
        lines.append(examples)
    if history:
        lines.append("Contexte de la conversation précédente :")
        lines.append("")
        lines.append(history)
        lines.append("")
        lines.append(
            "Les pronoms/anaphores de la demande (ex. « ce fichier », « le même ») "
            "se réfèrent à ce contexte. Résous-les pour remplir les champs."
        )
        lines.append("")
    lines.append(f"Demande : {intent}")
    guidance = _domain_guidance(domain)
    if guidance:
        lines.append("")
        lines.append(f"Rappel spécifique : {guidance}")
    return "\n".join(lines)


@dataclass(frozen=True)
class AttributesClassifierConfig:
    model: str
    api_key: str
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 1024
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    seed: int | None = 42
    base_url: str = "https://openrouter.ai/api/v1"
    site_url: str | None = None
    app_name: str = "activity-ai-engine"
    json_mode: bool = True


class AttributesClassifierClient(DomainClassifierClient):
    def __init__(self, config: AttributesClassifierConfig, system_prompt: str | None = None) -> None:
        super().__init__(config, system_prompt)


def build_attributes_classifier_client(
    *,
    model: str,
    api_key: str | None = None,
    temperature: float = 0.0,
    top_p: float = 1.0,
    max_tokens: int = 1024,
    frequency_penalty: float = 0.0,
    presence_penalty: float = 0.0,
    seed: int | None = 42,
    base_url: str = "https://openrouter.ai/api/v1",
    site_url: str | None = None,
    app_name: str = "activity-ai-engine",
    json_mode: bool = True,
) -> AttributesClassifierClient:
    resolved_api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not resolved_api_key:
        raise ValueError("OPENROUTER_API_KEY is required to use an OpenRouter client")

    return AttributesClassifierClient(
        AttributesClassifierConfig(
            model=model,
            api_key=resolved_api_key,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            seed=seed,
            base_url=base_url,
            site_url=site_url,
            app_name=app_name,
            json_mode=json_mode,
        ),
    )


def _validate_extracted_fields(action_id: str, domain: str, raw_fields: dict[str, Any]) -> dict[str, Any]:
    schema = ALL_ACTIONS.get(domain, {}).get(action_id) or schema_for_action(action_id)
    if schema is None:
        return {}
    allowed = collect_extractable_fields(schema)
    aliases = FIELD_NAME_ALIASES.get(action_id, {})
    cleaned: dict[str, Any] = {}
    for fname, val in raw_fields.items():
        if not isinstance(fname, str):
            continue
        canonical = aliases.get(fname, fname)
        if action_id.startswith("email.") and canonical in _EMAIL_RECIPIENT_FIELDS:
            val = _coerce_email_recipients_value(val)
        if canonical in allowed and val is not None:
            cleaned[canonical] = val
        elif fname in allowed and val is not None:
            cleaned[fname] = val
    return cleaned


def _coerce_email_recipients_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_coerce_email_recipients_value(item) for item in value]
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if not stripped:
        return stripped

    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [_coerce_email_recipients_value(item) for item in parsed]

    matches = [match.group(0) for match in _EMAIL_PATTERN.finditer(stripped)]
    unique_matches: list[str] = []
    for item in matches:
        if item not in unique_matches:
            unique_matches.append(item)
    if len(unique_matches) > 1:
        return unique_matches
    if len(unique_matches) == 1:
        if "<" in stripped and ">" in stripped:
            return unique_matches[0]
        if stripped.lower() == unique_matches[0].lower():
            return unique_matches[0]

    candidate_parts = re.split(r"\s*(?:,|;|\bet\b)\s*", stripped)
    cleaned_parts: list[str] = []
    for part in candidate_parts:
        cleaned = part.strip().strip("'\"[]()")
        if not cleaned:
            continue
        if cleaned.lower() in {"a", "à", "vers", "pour", "destinataire", "destinataires", "recipient", "recipients"}:
            continue
        if cleaned not in cleaned_parts:
            cleaned_parts.append(cleaned)
    if len(cleaned_parts) > 1:
        return cleaned_parts
    if len(cleaned_parts) == 1 and "@" not in cleaned_parts[0]:
        return cleaned_parts[0]
    return stripped


def _extract_intent_emails(intent: str) -> list[str]:
    emails: list[str] = []
    for match in _EMAIL_PATTERN.finditer(intent or ""):
        email = match.group(0)
        if email not in emails:
            emails.append(email)
    return emails


def _extract_sender_mailbox(intent: str) -> str | None:
    sender_match = re.search(
        r"(?:depuis|from|expediteur|expéditeur)\s*[:=]?\s*(" + _EMAIL_PATTERN.pattern + r")",
        intent or "",
        flags=re.IGNORECASE,
    )
    return sender_match.group(1) if sender_match else None


def _extract_subject(intent: str) -> str | None:
    quoted = re.search(r'\b(?:objet|subject)\b[^"\n\r]*"([^"]+)"', intent or "", flags=re.IGNORECASE)
    if quoted:
        return quoted.group(1).strip()
    plain = re.search(r"\b(?:objet|subject)\b\s*[:=]?\s*([^\n\r,;.]+)", intent or "", flags=re.IGNORECASE)
    if plain:
        return plain.group(1).strip().strip("'\"")
    return None


def _strip_date_suffix(value: str) -> str:
    if not value:
        return value

    candidates = [
        r"\s+(?:ce|le|la|cette|à|au|dans|en)\s+(?:soir|matin|après-midi|apres-midi|demain|aujourd'hui|hier|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre|\d{1,2}(?:[:hH]|h)?(?:\s*\d{1,2}m?)?)",
        r"\s+(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|aujourd'hui|demain|hier)\b.*$",
        r"\s+\d{1,2}(?::\d{2})?\s*(?:h|heure|heures)\b.*$",
    ]
    for pattern in candidates:
        result = re.split(pattern, value, flags=re.IGNORECASE)
        if result and result[0].strip() != value.strip():
            value = result[0]
            break
    return value.strip(" .;,;:()[]\"")


def _extract_people_list(intent: str) -> list[str]:
    patterns = [
        r"\b(?:à|a|vers|pour)\s+([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]+(?:\s*(?:,|et)\s*[A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]+)*)",
        r"\b(?:destinataire|destinataires|recipient|recipients)\s*[:=]?\s*([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]+(?:\s*(?:,|et)\s*[A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]+)*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, intent or "", flags=re.IGNORECASE)
        if not match:
            continue
        raw_fragment = match.group(0)
        phrase = match.group(1).strip(" .;,;:()[]\"")
        if "@" in raw_fragment:
            phrase = re.sub(r"^(?:.*?\b(?:à|a|vers|pour|destinataire|destinataires|recipient|recipients)\s*[:=]?\s*)", "", raw_fragment, count=1, flags=re.IGNORECASE)
        if not phrase:
            continue
        phrase = re.split(r"\s+(?:depuis|from|expediteur|expéditeur)\b.*$", phrase, flags=re.IGNORECASE)[0].strip()

        email_matches = [m.group(0) for m in _EMAIL_PATTERN.finditer(phrase)]
        if email_matches:
            unique_emails: list[str] = []
            for email in email_matches:
                if email not in unique_emails:
                    unique_emails.append(email)
            if unique_emails:
                return unique_emails

        people = _split_list_like_text(phrase)
        cleaned_people: list[str] = []
        for person in people:
            candidate = _strip_date_suffix(person.strip())
            candidate = candidate.strip(" .;,;:()[]\"")
            if not candidate or "@" in candidate or _EMAIL_PATTERN.fullmatch(candidate):
                continue
            if candidate not in cleaned_people:
                cleaned_people.append(candidate)
        if cleaned_people:
            return cleaned_people
    return []


def _extract_attachment_names(intent: str) -> list[str]:
    files: list[str] = []
    for match in _ATTACHMENT_PATTERN.finditer(intent or ""):
        token = match.group(0).strip().strip("'\"")
        if "@" in token:
            continue
        lowered = token.lower()
        if lowered.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg", ".txt")):
            if token not in files:
                files.append(token)
    return files


def _split_list_like_text(value: str) -> list[str]:
    candidates = re.split(r"\s*(?:,|;|&|\bet\b)\s*", value.strip())
    items: list[str] = []
    for item in candidates:
        cleaned = re.sub(r"^[\-–—:;\(\[]+|[\-–—:;\)\]]+$", "", item).strip()
        if not cleaned:
            continue
        if cleaned.lower() in {"l'équipe", "l'equipe", "l'equipe de travail", "l'équipe de travail"}:
            cleaned = "l'équipe"
        if cleaned not in items:
            items.append(cleaned)
    return items


def _normalize_calendar_value(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower().strip(" '\"")


def _is_generic_calendar_title(value: str | None) -> bool:
    if not value:
        return False
    compact = re.sub(r"[^a-zà-ö0-9\s'-]", " ", value.lower())
    tokens = [tok for tok in re.split(r"\s+", compact.strip()) if tok]
    if not tokens:
        return False
    generic_words = {
        "reunion", "réunion", "rdv", "rendez", "rendez-vous", "rendezvous", "meeting",
        "rencontre", "agenda", "evenement", "évènement", "tache", "tâche", "appel",
        "briefing", "atelier", "sessions", "session", "interview", "entretien", "atelier",
        "discussion", "point", "suivi"
    }
    # Plural / inflected forms count as generic too ("les réunions", "mes rdvs",
    # "les événements"): otherwise the classifier keeps the plural as a title and
    # the query filters on name ilike 'réunions'.
    return all(
        token in generic_words
        or token.rstrip("s") in generic_words
        or token.rstrip("x") in generic_words
        or token.rstrip("es") in generic_words
        for token in tokens
    )


#: Relative-time and question words: they describe WHEN or WHICH events, never
#: the name of an event ("les reunions de cette semaine" -> title must stay empty).
_TITLE_NOISE_RE = re.compile(
    r"\b(?:aujourd'?hui|demain|apr[èe]s[- ]demain|apr[èe]s[- ]midi|ce\s+(?:soir|matin)|"
    r"cette\s+(?:semaine|semaine[- ]l[àa]|mois|semaine)|semaine\s+(?:prochaine|derni[èe]re)|"
    r"mois\s+(?:prochain|dernier|pass[ée])|an\s+(?:prochain|dernier)|maintenant|[àa]\s*venir|"
    r"prochaines?|pass[ée]es?|r[ée]centes?|combien|quelles?|quels?|quand|o[ùu]\s+|liste|lister|"
    r"affich\w*|montre|tous|toutes|tout|mes|mon|ma|mes|nos|notre|je|j'|ai\b|j'|jeune|"
    r"actuels?|actuelles?|en cours)\b",
    re.IGNORECASE,
)


#: Articles, prepositions and pronouns: they carry no event name either.
_TITLE_STOPWORDS = frozenset({
    "de", "du", "des", "d", "la", "le", "les", "l", "au", "aux", "a", "en", "pour",
    "sur", "avec", "et", "y", "il", "elle", "on", "nous", "vous", "ils", "elles",
    "plus", "moins", "tres", "bien", "ou", "que", "qui", "quoi", "dont", "par",
    "dans", "sous", "entre", "vers", "chez", "ce", "cet", "cette", "ces", "the",
    "of", "my", "this", "these", "s", "y", "c", "est", "sont", "ai", "as",
})


def _looks_like_relative_phrase(text: str) -> bool:
    """True when a 'title' is only relative time / question wording, not a name."""
    without_noise = _TITLE_NOISE_RE.sub(" ", text or "")
    tokens = [
        token
        for token in re.split(r"\W+", without_noise, flags=re.UNICODE)
        if token and token.lower() not in _TITLE_STOPWORDS
    ]
    if not tokens:
        return True
    return _is_generic_calendar_title(" ".join(tokens))


def _extract_quoted_title(intent: str) -> str | None:
    patterns = [
        r'(?:titre|intitul(?:é|e)|nom|nomm(?:e|ée)|objet)\s*(?:[:=]|de)?\s*["\']([^"\']+)["\']',
        r'["\']([^"\']{2,80})["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, intent or "", flags=re.IGNORECASE)
        if match:
            title = match.group(1).strip(" .;,;:()[]\"")
            if title and not _is_generic_calendar_title(title):
                return title
    return None


def _extract_title_from_intent(intent: str) -> str | None:
    quoted = _extract_quoted_title(intent)
    if quoted:
        return quoted

    patterns = [
        r"\b(?:avec|pour|chez|de|sur)\s+([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]{2,80})",
        r"\b(?:reunion|réunion|rdv|rendez-vous|meeting|rencontre)\s+(?:avec|pour)\s+([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]{2,80})",
        r"\b(?:appel|interview|entretien|briefing|session|atelier)\s+(?:avec|pour)\s+([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]{2,80})",
        r"\b(?:planifie|programme|crée|cree|ajoute)\s+(?:une|la|le)?\s*(?:reunion|réunion|rdv|meeting|rencontre)\s+(?:avec|pour)\s+([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]{2,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, intent or "", flags=re.IGNORECASE)
        if not match:
            continue
        title = match.group(1).strip(" .;,;:()[]\"")
        title = _strip_date_suffix(title)
        if not title or _is_generic_calendar_title(title):
            continue
        people = _split_list_like_text(title)
        if len(people) > 1:
            continue
        if re.search(r"(?:,|\s+et\s+)", title):
            continue
        return title
    return None


def _extract_attendees(intent: str) -> list[str] | None:
    patterns = [
        r"\b(?:avec|participants?|avec les participants?)\s+(.+?)(?=\s+(?:le|la|ce|cette|à|dans|de|du|au|en|pour)\b|$)",
        r"\b(?:pour|avec)\s+([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]+(?:\s*(?:,|et)\s*[A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9&' .\-]+)*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, intent or "", flags=re.IGNORECASE)
        if not match:
            continue
        phrase = match.group(1).strip(" .;,;:()[]\"")
        if not phrase:
            continue
        phrase = re.split(r"\s+(?:depuis|from|expediteur|expéditeur)\b.*$", phrase, flags=re.IGNORECASE)[0].strip()
        phrase = _strip_date_suffix(phrase)
        people = _split_list_like_text(phrase)
        if people:
            return people
    return None


def _extract_location(intent: str) -> str | None:
    match = re.search(r"\b(?:à|au|dans|en)\s+([A-ZÀ-Ö][A-Za-zÀ-Öà-ö0-9'\- .]{2,80})\b(?=\s*(?:le|avec|ce|de|pour|$))", intent or "", flags=re.IGNORECASE)
    if match:
        return match.group(1).strip(" .;,;:()[]\"")
    return None


def _post_process_calendaring_fields(intent: str, action_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fields)

    title = str(updated.get("title", "")).strip()
    if not title or _is_generic_calendar_title(title) or _looks_like_relative_phrase(title):
        # "reunions ai je cette semaine" is the question itself, not an event name:
        # keeping it would filter the agenda on name ilike '<whole sentence>'.
        updated.pop("title", None)
        inferred_title = _extract_quoted_title(intent) or _extract_title_from_intent(intent)
        if inferred_title and not _is_generic_calendar_title(inferred_title) and not _looks_like_relative_phrase(inferred_title):
            updated["title"] = inferred_title

    attendees = updated.get("attendees")
    if not attendees:
        inferred_attendees = _extract_attendees(intent)
        if inferred_attendees:
            title_value = str(updated.get("title", "")).strip()
            if len(inferred_attendees) == 1 and title_value and _normalize_calendar_value(inferred_attendees[0]) == _normalize_calendar_value(title_value):
                inferred_attendees = []
            if inferred_attendees:
                updated["attendees"] = inferred_attendees

    title_value = str(updated.get("title", "")).strip()
    attendees_value = updated.get("attendees")
    if isinstance(attendees_value, list) and title_value:
        normalized_title = _normalize_calendar_value(title_value)
        deduped = []
        for person in attendees_value:
            if _normalize_calendar_value(str(person)) == normalized_title:
                continue
            deduped.append(person)
        if deduped != attendees_value:
            updated["attendees"] = deduped or None

    location = updated.get("location")
    if not location:
        inferred_location = _extract_location(intent)
        if inferred_location:
            updated["location"] = inferred_location

    if "start_day" in updated and "start_month" in updated and "start_year" in updated:
        date_values = [updated.get("start_day"), updated.get("start_month"), updated.get("start_year")]
        if all(isinstance(v, str) and v.strip() for v in date_values):
            combined = " ".join(v.strip() for v in date_values if v)
            if combined and len(combined.split()) > 3:
                updated["start_day"] = date_values[0]
                updated["start_month"] = date_values[1]
                updated["start_year"] = date_values[2]

    return updated


def _post_process_email_fields(intent: str, action_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    schema = schema_for_action(action_id)
    allowed = collect_extractable_fields(schema) if schema else {}
    if not allowed:
        return fields

    updated = dict(fields)

    if "mailbox" in allowed and not updated.get("mailbox"):
        mailbox = _extract_sender_mailbox(intent)
        if mailbox:
            updated["mailbox"] = mailbox

    recipient_field = "recipients" if "recipients" in allowed else ("recipient" if "recipient" in allowed else None)
    if recipient_field:
        sender = str(updated.get("mailbox") or "").strip().lower()
        updated[recipient_field] = _resolve_email_recipients(
            intent, updated.get(recipient_field), sender
        )

    if "subject" in allowed and not updated.get("subject"):
        subject = _extract_subject(intent)
        if subject:
            updated["subject"] = subject

    if "attachment_names" in allowed and not updated.get("attachment_names"):
        attachments = _extract_attachment_names(intent)
        if attachments:
            updated["attachment_names"] = attachments[0] if len(attachments) == 1 else attachments

    return updated


def _is_valid_recipient_value(value: Any) -> bool:
    """A recipient value is trustworthy when every item is an email address or
    a short, name-like fragment. Long prose snippets mean the model failed to
    extract (e.g. it copied a whole clause of the request as a "recipient")."""
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, str):
            return False
        stripped = item.strip()
        if not stripped:
            return False
        if "@" not in stripped and len(stripped.split()) > 3:
            return False
    return True


def _resolve_email_recipients(intent: str, existing: Any, sender: str) -> Any:
    """Preference-ordered resolution of the recipients of an email action.

    1. Email addresses explicitly present in the intent (excluding the sender
       mailbox): an address in the raw text is an unambiguous signal, and the
       attribute LLM has been observed to corrupt this exact field (copying
       whole clauses instead of the address).
    2. The model's own value, once coerced, provided it looks like addresses
       or short names.
    3. Rule-based person/party inference from the intent.

    Returns a single string (one recipient), a list, or None (unknown).
    """
    sender = (sender or "").strip().lower()
    intent_emails = [email for email in _extract_intent_emails(intent) if email.lower() != sender]
    if intent_emails:
        return intent_emails[0] if len(intent_emails) == 1 else intent_emails

    if isinstance(existing, str):
        existing = _coerce_email_recipients_value(existing)
    if _is_valid_recipient_value(existing):
        if isinstance(existing, list) and len(existing) == 1:
            return existing[0]
        return existing

    people: list[str] = []
    for person in _extract_people_list(intent):
        stripped_person = person.strip()
        if not stripped_person or stripped_person.lower() == sender:
            continue
        if stripped_person.lower() in {"l'équipe", "l'equipe", "destinataire", "destinataires", "recipient", "recipients"}:
            continue
        people.append(stripped_person)
    if len(people) == 1:
        return people[0]
    if people:
        return people
    return None


_CURRENCY_TOKEN_RE = re.compile(
    r"(?:\b(?:eur|euro|euros|usd|us\$|dollar|dollars|cad|aud|gbp|chf|jpy|yen|dt|tnd|dinar|dinars)\b|€|£|\$)",
    re.IGNORECASE,
)

_CURRENCY_CANONICAL: dict[str, str] = {
    "eur": "EUR", "euro": "EUR", "euros": "EUR", "€": "EUR",
    "usd": "USD", "us$": "USD", "$": "USD", "dollar": "USD", "dollars": "USD",
    "cad": "CAD", "aud": "AUD", "gbp": "GBP", "chf": "CHF",
    "jpy": "JPY", "yen": "JPY",
    "tnd": "TND", "dt": "TND", "dinar": "TND", "dinars": "TND",
}


def _post_process_amount_currency_fields(intent: str, action_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Split a monetary value such as ``"14500 EUR"`` into the amount and the
    dedicated ``currency`` field declared by the schema, when one exists.

    The attribute LLM copies amount values verbatim (prompt rule: text is
    kept exactly as written), so ``"14500 EUR"`` lands in ``amount`` while the
    schema's separate ``currency`` field stays empty. This deterministic pass
    moves the recognised currency token to ``currency`` and leaves the bare
    figure in the amount field. No-op when the schema has no currency field or
    the value carries no currency token.
    """
    schema = schema_for_action(action_id)
    if schema is None:
        return fields
    kinds = collect_extractable_fields(schema)

    currency_field = next(
        (
            fname
            for fname, (ftype, _k) in kinds.items()
            if ftype == "currency" or fname.lower() in {"currency", "currency_code"}
        ),
        None,
    )
    if currency_field is None or str(fields.get(currency_field) or "").strip():
        return fields

    for field_name, (ftype, _k) in kinds.items():
        if ftype not in ("amount", "number"):
            continue
        value = fields.get(field_name)
        if not isinstance(value, str):
            continue
        match = _CURRENCY_TOKEN_RE.search(value)
        if match is None:
            continue
        token = match.group(0)
        code = _CURRENCY_CANONICAL.get(token.lower(), token.upper())
        stripped = _CURRENCY_TOKEN_RE.sub("", value)
        stripped = " ".join(stripped.split()).strip(" ,;")
        fields[currency_field] = code
        if stripped:
            fields[field_name] = stripped
        else:
            fields.pop(field_name, None)
        break
    return fields


def _post_process_extracted_fields(intent: str, domain: str, parsed: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    updated: dict[str, dict[str, Any]] = {}
    for action_id, payload in parsed.items():
        fields = payload.get("fields") if isinstance(payload, dict) else {}
        if not isinstance(fields, dict):
            fields = {}

        if domain == "email_action" and action_id.startswith("email."):
            fields = _post_process_email_fields(intent, action_id, fields)
            fields = _validate_extracted_fields(action_id, domain, fields)
        elif domain == "calendaring":
            fields = _post_process_calendaring_fields(intent, action_id, fields)
            fields = _validate_extracted_fields(action_id, domain, fields)

        fields = _post_process_amount_currency_fields(intent, action_id, fields)

        updated[action_id] = {"fields": fields}
    return updated


def extract_fields_from_response(
    raw_response: str,
    domain: str,
    requested_actions: list[str],
) -> dict[str, dict[str, Any]]:
    """Parse a per-domain model output into {action_id: {"fields": {...}}}.

    Only requested actions and only their declared non-system fields are kept.
    """
    result: dict[str, dict[str, Any]] = {}
    requested = list(dict.fromkeys(requested_actions))
    canonical_by_norm = {normalize_action(a): a for a in requested if a}

    try:
        parsed = json.loads(raw_response)
    except (json.JSONDecodeError, TypeError):
        return result
    if not isinstance(parsed, dict):
        return result

    for key, value in parsed.items():
        normalized = normalize_action(str(key))
        if normalized is None or normalized not in canonical_by_norm:
            continue
        action_id = canonical_by_norm[normalized]
        if not isinstance(value, dict):
            result[action_id] = {"fields": {}}
            continue
        raw_fields = value.get("fields") if isinstance(value.get("fields"), dict) else value
        if not isinstance(raw_fields, dict):
            result[action_id] = {"fields": {}}
            continue
        cleaned = _validate_extracted_fields(action_id, domain, raw_fields)
        result[action_id] = {"fields": cleaned}
    return result


class RuleBasedAttributesClassifierClient:
    """Baseline: parses the requested actions from the prompt and returns empty
    field extraction (nothing is actually extracted)."""

    def complete(self, prompt: str) -> str:
        actions: list[str] = []
        in_block = False
        for line in prompt.splitlines():
            stripped = line.strip()
            if stripped.startswith("Actions à traiter"):
                in_block = True
                continue
            if in_block:
                if stripped.startswith("- "):
                    candidate = stripped[2:].split(":")[0].strip()
                    if _looks_like_action(candidate):
                        actions.append(candidate)
                elif stripped.startswith(("Exemples", "Demande")):
                    break
        return json.dumps({a: {"fields": {}} for a in actions}, ensure_ascii=False)


def _looks_like_action(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9_]+)+", value))


def coerce_attribute_completion_client(model_client: Any | None) -> Callable[[str], str]:
    if model_client is None:
        return RuleBasedAttributesClassifierClient().complete

    if callable(model_client):
        return model_client

    for method_name in ("complete", "generate", "predict", "invoke"):
        method = getattr(model_client, method_name, None)
        if callable(method):
            return method

    raise TypeError(
        "Unsupported model client. Pass a callable or an object exposing complete(), generate(), predict(), or invoke()."
    )


@dataclass(frozen=True)
class AttributesPrediction:
    intent: str
    domains: list[str]
    actions: list[str]
    fields: dict[str, dict[str, dict]]
    raw_response: str

    def to_terminal(self) -> dict[str, Any]:
        """Stage-3 terminal envelope carrying domains + actions + per-domain fields."""
        return {
            "domains": list(self.domains),
            "actions": list(self.actions),
            "fields": self.fields,
        }


class AttributesClassifierAgent:
    def __init__(
        self,
        model_client: Any | None = None,
        domain_client: Any | None = None,
        action_client: Any | None = None,
        use_pipeline: bool = False,
    ) -> None:
        self._completion = coerce_attribute_completion_client(model_client)
        self._domain_agent = DomainClassifierAgent(model_client=domain_client) if use_pipeline else None
        self._action_agent = (
            ActionClassifierAgent(model_client=action_client, use_domain_pipeline=False)
            if use_pipeline
            else None
        )

    def _call_completion(self, prompt: str, system_prompt: str | None = None) -> str:
        try:
            return str(self._completion(prompt, system_prompt=system_prompt)).strip()
        except TypeError:
            return str(self._completion(prompt)).strip()

    def _predict_for_domain(
        self, intent: str, domain: str, actions: list[str], history: str | None = None
    ) -> tuple[dict, str]:
        prompt = build_attributes_prompt(intent, domain, actions, history) if history else build_attributes_prompt(intent, domain, actions)
        system_prompt = ATTRIBUTE_SYSTEM_PROMPTS_BY_DOMAIN.get(domain, "")
        raw_response = self._call_completion(prompt, system_prompt)
        parsed = extract_fields_from_response(raw_response, domain, actions)
        if not parsed:
            print(f"  [attributes] empty/invalid field output for {domain}, retrying in 2s", flush=True)
            time.sleep(2)
            raw_response = self._call_completion(prompt, system_prompt)
            parsed = extract_fields_from_response(raw_response, domain, actions)

        missing_actions = [a for a in actions if a not in parsed]
        if missing_actions:
            retry_prompt = (
                prompt
                + "\n\n# Rappel URGENT\n"
                + "Toutes les actions listées doivent figurer dans le JSON de sortie. "
                + "Si une action ne contient pas de valeur explicite, renvoie {\"fields\": {}} pour cette action."
            )
            time.sleep(1)
            retry_raw = self._call_completion(retry_prompt, system_prompt)
            retry_parsed = extract_fields_from_response(retry_raw, domain, actions)
            for action_id in actions:
                if action_id in retry_parsed:
                    parsed[action_id] = retry_parsed[action_id]

        if len(actions) > 1 and parsed:
            empty_actions = [a for a in actions if a in parsed and not parsed[a].get("fields")]
            if empty_actions:
                retry_prompt = (
                    prompt
                    + "\n\n# Rappel URGENT\n"
                    + f"L'action {empty_actions[0]} n'a aucun champ rempli. "
                    + "La demande contient une valeur pour cette action — extrais-la maintenant."
                )
                time.sleep(1)
                retry_raw = self._call_completion(retry_prompt, system_prompt)
                retry_parsed = extract_fields_from_response(retry_raw, domain, actions)
                for action_id in empty_actions:
                    if action_id in retry_parsed and retry_parsed[action_id].get("fields"):
                        parsed[action_id] = retry_parsed[action_id]

        parsed = _post_process_extracted_fields(intent, domain, parsed)
        for action_id in actions:
            if action_id not in parsed:
                parsed[action_id] = {"fields": {}}
        return parsed, raw_response

    def predict(
        self,
        intent: str,
        domains: list[str] | None = None,
        actions: list[str] | None = None,
        history: str | None = None,
    ) -> AttributesPrediction:
        if actions is None:
            if self._action_agent is None:
                actions = []
            else:
                actions = self._action_agent.predict(intent, domains=domains).predicted_actions
        if domains is None:
            if self._domain_agent is None:
                domains = []
            else:
                domains = self._domain_agent.predict(intent).predicted_domains

        domains = list(dict.fromkeys(domains or []))
        actions = list(dict.fromkeys(actions or []))
        if not domains or not actions:
            return AttributesPrediction(
                intent=intent, domains=domains, actions=actions, fields={}, raw_response=""
            )

        fields: dict[str, dict[str, dict]] = {}
        raw_parts: list[str] = []
        for domain, domain_actions in partition_actions_by_domain(domains, actions).items():
            catalog = ALL_ACTIONS.get(domain, {})
            with_schema = [a for a in domain_actions if a in catalog]
            without_schema = [a for a in domain_actions if a not in catalog]
            per_action: dict[str, dict] = {}
            if with_schema:
                extracted, raw = self._predict_for_domain(intent, domain, with_schema, history) if history else self._predict_for_domain(intent, domain, with_schema)
                per_action.update(extracted)
                # Deterministic completeness: every requested action must appear
                # in the envelope, even when the model omits it from its JSON
                # (e.g. a lookup action whose entity text was already captured by
                # a sibling action). Missing actions fall back to empty fields.
                for action_id in with_schema:
                    per_action.setdefault(action_id, {"fields": {}})
                if raw:
                    raw_parts.append(raw)
            for action_id in without_schema:
                per_action[action_id] = {"fields": {}}
            fields[domain] = per_action

        return AttributesPrediction(
            intent=intent,
            domains=domains,
            actions=actions,
            fields=fields,
            raw_response="\n".join(raw_parts),
        )


def classify_attributes(
    intent: str,
    domains: list[str] | None = None,
    actions: list[str] | None = None,
    model_client: Any | None = None,
    domain_client: Any | None = None,
    action_client: Any | None = None,
    history: str | None = None,
) -> AttributesPrediction:
    return AttributesClassifierAgent(
        model_client=model_client,
        domain_client=domain_client,
        action_client=action_client,
        use_pipeline=domains is None or actions is None,
    ).predict(intent, domains=domains, actions=actions, history=history)
