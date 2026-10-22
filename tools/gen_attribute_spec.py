"""Generate ATTRIBUTE_EXTRACTION_SPEC.md from ACTIONS attributes.txt.

Normalizes the 3 source schemas (Odoo flat, REST method/path, JMAP, WOPI)
into a per-action catalog of fields classified as:
  - extract  : value is derivable from the user query (model extracts it)
  - entity   : the user names an entity; system resolves to id via lookup
  - target   : single existing record named in the query (no-arg actions)
  - system   : account id, auth, keys, tokens, secrets - system-injected

Curated user-facing definitions in `tools/attr_defs/<domain>.json` REPLACE the
technical parsed schemas and provide the fields for the domains the source file
does not document at all (`general`, `knowledge_kb`).

Model output = {"action": ..., "fields": {extractable keys only}}.
System merge  = endpoint/method/path_params/secrets/resolved ids.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ATTR_FILE = Path(r"C:\Bureau\activiity_ai_engine\ACTIONS attributes.txt")
OUT_FILE = Path(r"C:\Bureau\activiity_ai_engine\ATTRIBUTE_EXTRACTION_SPEC.md")
CURATED_DEFS_DIR = Path(__file__).resolve().parent / "attr_defs"

SYSTEM_FIELDS = {
    "key", "token", "password", "access_token", "Authorization",
    "apiUrl", "wopi_base", "callbackUrl", "accountId", "account_id",
    "X-WOPI-Override", "X-WOPI-Lock",
    "X-WOPI-Size", "X-WOPI-SessionContext", "X-WOPI-MaxExpectedSize",
    "X-WOPI-Editors", "X-WOPI-SuggestedTarget", "X-WOPI-FileConversion",
    "X-WOPI-RequestedName", "endpoint", "call", "http", "jmap_method",
    "using", "fromAccountId", "bearer", "auth", "webhook_secret",
    "id", "ids", "file_id", "container_id",
}

# record-target plumbing: replaced by a natural-noun target key
IDLIKE_FIELDS = {"id", "ids", "file_id", "container_id"}

ENTITY_SUFFIXES = ("_id", "_ids", "id", "ids")
ENTITY_PATH_NAMES = {
    "project", "task", "view", "conversation", "contact", "account",
    "inbox", "identity", "mailbox", "company", "partner", "employee",
    "lead", "move", "journal", "team", "user", "campaign",
    "automation_rule", "macro", "canned_response", "notification",
}

# Clear user-facing field names for the extraction contract. The schema keeps
# the technical name (used at execution time to resolve ids / build the call);
# the prompt and the model output use the clear name. TARGET keys are the
# Odoo/internal model names (move, blob, sieve_script, ...) that mean nothing
# to a user, so they are renamed to the natural noun.
CLEAR_FIELD_NAMES = {
    "move": "invoice",
    "blob": "attachment",
    "bank_statement_line": "statement_line",
    "sieve_script": "filter",
    "vacation_response": "auto_reply",
}


def clear_field_name(fname: str) -> str:
    return CLEAR_FIELD_NAMES.get(fname, fname)


# Reverse map for execution: clear name -> technical field name.
TECHNICAL_FIELD_NAMES: dict[str, str] = {v: k for k, v in CLEAR_FIELD_NAMES.items()}


def split_top_level_blocks(text: str) -> list[tuple[str, str]]:
    """Return [(name, body)] for each `NAME = { ... }` at column 0."""
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    body: list[str] = []
    depth = 0
    for line in lines:
        m = re.match(r"^([A-Z0-9_]+)\s*=\s*\{", line)
        if m and depth == 0:
            if current_name is not None:
                blocks.append((current_name, "\n".join(body)))
            current_name = m.group(1)
            body = []
            depth = 1
            continue
        if current_name is None:
            continue
        opens = line.count("{") - line.count("}")
        depth += opens
        body.append(line)
        if depth <= 0:
            blocks.append((current_name, "\n".join(body)))
            current_name = None
            body = []
            depth = 0
    if current_name is not None:
        blocks.append((current_name, "\n".join(body)))
    return blocks


def parse_field(value: str) -> tuple[str, bool]:
    """Return (type, is_required) from a 'type - required/optional - note' string."""
    value = value.strip()
    if not value:
        return "str", False
    type_part = value.split(" - ")[0].strip()
    lowered = value.lower()
    if "required" in lowered and "optional" not in lowered:
        return type_part, True
    if re.search(r"\(required\)", lowered):
        return type_part, True
    return type_part, False


def classify_field(name: str) -> str:
    base = name.split(".")[0].strip()
    if name in SYSTEM_FIELDS or base in SYSTEM_FIELDS:
        return "system"
    if name in ENTITY_PATH_NAMES:
        return "entity"
    if name.endswith(ENTITY_SUFFIXES):
        return "entity"
    if base in ("campaign", "conversation", "inbox", "macro", "notification",
                "message", "automation_rule", "canned_response", "contact",
                "identity", "mailbox"):
        return "entity"
    return "extract"


# ---------------------------------------------------------------------------
# Example generation (synthetic French query + concrete model output per action)
# ---------------------------------------------------------------------------

FRENCH_VERBS = {
    "create": "Créer", "add": "Ajouter", "write": "Modifier", "update": "Modifier",
    "set": "Renseigner", "delete": "Supprimer", "remove": "Supprimer",
    "unlink": "Supprimer", "action_post": "Valider", "action_confirm": "Confirmer",
    "action_cancel": "Annuler", "action_draft": "Passer en brouillon",
    "action_lock": "Verrouiller", "action_unlock": "Déverrouiller",
    "send": "Envoyer", "get": "Consulter", "read": "Consulter",
    "search_read": "Rechercher", "find_or_create": "Rechercher ou créer",
    "read_group": "Regrouper", "search": "Rechercher", "duplicate": "Dupliquer",
    "clone": "Dupliquer", "copy": "Dupliquer", "download": "Télécharger",
    "upload": "Téléverser", "export": "Exporter", "import": "Importer",
    "convert": "Convertir", "start": "Démarrer", "stop": "Arrêter",
    "archive": "Archiver", "merge": "Fusionner", "restore": "Restaurer",
    "refresh": "Rafraîchir", "connect": "Connecter", "disconnect": "Déconnecter",
    "verify": "Vérifier", "register": "Enregistrer", "submit": "Soumettre",
    "post": "Publier", "approve": "Approuver", "reject": "Rejeter",
    "invite": "Inviter", "leave": "Quitter", "join": "Rejoindre",
    "assign": "Assigner", "move": "Déplacer", "toggle": "Basculer",
    "reset": "Réinitialiser", "mark": "Marquer", "button_fetch": "Récupérer",
    "validate": "Valider", "done": "Marquer comme fait", "apply": "Appliquer",
    "confirm": "Confirmer", "approve": "Approuver", "open": "Ouvrir",
    "print": "Imprimer", "reopen": "Rouvrir", "send_mail": "Envoyer par email",
    "action_set_date": "Planifier", "action_show_details": "Afficher",
    "snooze": "Différer", "refuse": "Refuser", "undo": "Annuler",
    "set_lost": "Marquer comme perdu", "set_won": "Marquer comme gagné",
    "convert_opportunity": "Convertir", "merge_opportunity": "Fusionner",
    "log_meeting": "Journaliser la réunion", "address_get": "Obtenir l'adresse",
    "create_company": "Créer une société", "check_move_sequence_chain": "Vérifier la séquence",
    "compute_all": "Calculer", "get_currency_rate": "Obtenir le taux",
    "js_assign_outstanding_line": "Assigner", "mark_as_sent": "Marquer comme envoyé",
    "open_reconcile_view": "Ouvrir le rapprochement",
    "register_payment": "Enregistrer le paiement", "reverse": "Inverser",
    "switch_move_type": "Changer le type", "toggle_block_payment": "Bloquer le paiement",
    "undo_reconciliation": "Défaire le rapprochement",
    "create_vendor_bill": "Créer une facture fournisseur",
    "create_customer_payment": "Créer un paiement client",
    "create_supplier_payment": "Créer un paiement fournisseur",
    "fetch_in_einvoices": "Récupérer les factures électroniques",
    "create_bank_statement": "Créer un relevé bancaire",
    "add_from_catalog": "Ajouter depuis le catalogue",
    "action_validate": "Valider", "action_duplicate": "Dupliquer",
    "action_get": "Consulter", "action_reverse": "Inverser",
    "action_print_pdf": "Imprimer le PDF", "action_invoice_download_pdf": "Télécharger le PDF",
    "action_force_register_payment": "Enregistrer le paiement",
    "action_register_payment": "Enregistrer le paiement",
    "action_create_new": "Créer", "action_create_vendor_bill": "Créer une facture fournisseur",
    "action_undo_reconciliation": "Défaire le rapprochement",
    "action_assign_leads": "Assigner les leads", "action_reschedule_meeting": "Reprogrammer la réunion",
    "action_schedule_meeting": "Planifier une réunion",
    "action_show_potential_duplicates": "Afficher les doublons",
    "action_create_meeting": "Créer une réunion", "action_send_email": "Envoyer un email",
    "action_create_user": "Créer un utilisateur", "action_create_users": "Créer des utilisateurs",
    "action_related_contacts": "Afficher les contacts", "generate_random_barcode": "Générer un code-barres",
    "action_approve": "Approuver", "action_refuse": "Refuser", "action_pay": "Payer",
    "action_reset": "Réinitialiser", "action_split_wizard": "Fractionner",
    "action_submit": "Soumettre", "action_back_to_approval": "Renvoi à l'approbation",
    "action_approve_overtime": "Approuver les heures sup",
    "action_refuse_overtime": "Refuser les heures sup",
    "get_kiosk_url": "Obtenir l'URL du kiosque",
    "get_department_hierarchy": "Obtenir la hiérarchie",
    "create_employee_from_applicant": "Créer un employé", "archive_applicant": "Archiver",
    "action_archive": "Archiver", "action_restore": "Restaurer", "action_unarchive": "Désarchiver",
    "action_open_discount_wizard": "Ouvrir les remises",
    "action_preview_sale_order": "Aperçu de la commande",
    "action_quotation_send": "Envoyer le devis", "action_quotation_sent": "Marquer devis envoyé",
    "action_update_prices": "Mettre à jour les prix", "action_update_taxes": "Mettre à jour les taxes",
    "action_view_invoice": "Voir la facture",
    "create_document_from_attachment": "Créer un document",
    "get_portal_last_transaction": "Obtenir la dernière transaction",
    "payment_action_capture": "Capturer le paiement", "payment_action_void": "Annuler le paiement",
    "register_webhook": "Enregistrer le webhook",
    "button_cancel": "Annuler", "button_draft": "Passer en brouillon",
    "button_hash": "Hacher", "button_request_cancel": "Demander l'annulation",
    "open_payments": "Afficher les paiements", "reverse_moves": "Inverser les écritures",
    "create_payments": "Créer les paiements", "create_invoices": "Créer les factures",
    "action_lost_reason_apply": "Appliquer le motif de perte",
    "action_update_crm_lead_probabilities": "Mettre à jour les probabilités",
    "action_mass_convert": "Convertir en masse", "open_commercial_entity": "Afficher l'entité commerciale",
    "create_expense_from_attachments": "Créer une note de frais depuis les pièces jointes",
    "mute": "Mettre en sourdine", "unmute": "Rétablir le son",
    "mark_read": "Marquer comme lu", "mark_unread": "Marquer comme non lu",
    "toggle_priority": "Basculer la priorité", "toggle_status": "Basculer le statut",
    "toggle_typing": "Indiquer la frappe", "transcript": "Voir la transcription",
    "retry": "Réessayer", "bulk": "Appliquer en masse", "move_task": "Déplacer la tâche",
    "partial_update": "Mettre à jour", "link": "Partager par lien",
    "put": "Téléverser", "mkcol": "Créer le dossier",
    "propfind": "Consulter les métadonnées", "proppatch": "Modifier les métadonnées",
    "transfer": "Transférer", "list": "Lister", "direct_editing": "Ouvrir en édition directe",
    "query": "Rechercher", "parse": "Analyser", "query_changes": "Voir les changements",
    "get_changes": "Voir les changements", "search_snippet": "Rechercher un extrait",
    "rename": "Renommer", "data": "Consulter", "mail_from": "Voir l'expéditeur",
    "rcpt_to": "Voir le destinataire", "lookup": "Rechercher",
    "custom_attributes": "Gérer les attributs", "labels": "Gérer les étiquettes",
    "participants": "Gérer les participants", "generate": "Générer",
    "insert_image": "Insérer une image", "mail_merge": "Lancer le publipostage",
    "refresh_file": "Rafraîchir le fichier", "refresh_history": "Rafraîchir l'historique",
    "start_filling": "Démarrer le remplissage", "deny_editing": "Bloquer l'édition",
    "download_as": "Télécharger", "check_file_info": "Vérifier les infos fichier",
    "enumerate_children": "Lister les éléments", "get_file": "Télécharger",
    "put_file": "Téléverser", "put_relative": "Téléverser",
    "refresh_lock": "Renouveler le verrou", "lock": "Verrouiller", "unlock": "Déverrouiller",
    "drop": "Abandonner", "forcesave": "Forcer l'enregistrement", "info": "Afficher les infos",
    "meta": "Afficher les métadonnées", "error": "Signaler une erreur",
    "save_ready": "Confirmer la sauvegarde", "recurrence": "Gérer la récurrence",
    "multiget": "Consulter en masse", "free_busy": "Voir la disponibilité",
    "reminder": "Gérer les rappels", "sync": "Synchroniser",
    # knowledge_kb
    "grounded": "Répondre", "pdf": "Exporter en PDF", "image": "Générer une infographie",
    "podcast": "Générer un podcast", "report": "Générer un rapport", "video": "Générer une vidéo",
    "document": "Importer le document", "url": "Importer l'URL", "youtube": "Importer la vidéo YouTube",
    "note": "Créer une note", "fulltext": "Rechercher par mot-clé", "vector": "Rechercher par thème",
    "space": "Gérer l'espace", "batch": "Consulter le statut du lot",
    "processing": "Consulter le statut de traitement",
    # general
    "conseil": "Donner un conseil", "redaction": "Rédiger", "resume": "Résumer",
    "traduction": "Traduire",
}

FRENCH_NOUNS = {
    "move": "la facture", "journal": "le journal", "account": "le compte",
    "bank_statement": "le relevé bancaire", "invoice_line": "la ligne de facture",
    "payment": "le paiement", "tax": "la taxe", "currency": "le taux de change",
    "partner": "le contact", "lead": "le lead", "order": "la commande",
    "quotation": "le devis", "employee": "l'employé", "applicant": "le candidat",
    "department": "le département", "expense": "la note de frais",
    "event": "l'événement", "alarm": "l'alarme", "free_busy": "la disponibilité",
    "attendee": "le participant", "share": "le partage", "folder": "le dossier",
    "file": "le fichier", "conversation": "la conversation", "contact": "le contact",
    "campaign": "la campagne", "canned_response": "la réponse type",
    "macro": "la macro", "automation_rule": "la règle d'automatisation",
    "notification": "la notification", "task": "la tâche", "project": "le projet",
    "label": "l'étiquette", "bucket": "le panneau", "attachment": "la pièce jointe",
    "subscription": "l'abonnement", "submission": "l'envoi",
    "email_submission": "l'email", "identity": "l'identité",
    "mailbox": "la boîte mail", "mail": "l'email", "blob": "la pièce jointe",
    "vacation_response": "la réponse d'absence", "editor": "le document",
    "converter": "le document", "command": "la commande serveur",
    "document": "le document", "comment": "le commentaire", "line": "la ligne",
    "stage": "l'étape", "tag": "l'étiquette", "template": "le modèle",
    "schedule": "le planning", "session": "la session", "filter": "le filtre",
    "view": "la vue", "workspace": "l'espace de travail", "material": "le matériel",
    "product": "le produit", "car": "le véhicule", "property": "le bien",
    "member": "le membre", "guest": "l'invité", "page": "la page",
    "chapter": "le chapitre", "space": "l'espace", "domain": "le domaine",
    "trashbin": "la corbeille", "version": "la version", "calendar": "le calendrier",
    "wopi": "le document", "editor": "le document", "converter": "le document",
    "docbuilder": "le document", "command": "la commande serveur",
}

ENTITY_EXAMPLES = [
    ("partner", "TechNova"), ("customer", "TechNova"),("company","Technova"), ("vendor", "Acme Fournisseur"),
    ("supplier", "Acme Fournisseur"), ("company", "Acme SARL"), ("employee", "Jean Dupont"),
    ("user", "Jean Dupont"), ("assignee", "Jean Dupont"), ("responsible", "Jean Dupont"),
    ("contact", "Marie Durand"), ("conversation", "Conversation Acme"),
    ("inbox", "Inbox support"), ("campaign", "Campagne été"), ("calendar", "Calendrier pro"),
    ("journal", "Journal de vente"), ("account", "Compte 411000"),
    ("move", "Facture F2026-001"), ("order", "Commande C2026-001"), ("lead", "Lead TechNova"),
    ("project", "Site web"), ("task", "Relancer Acme"), ("team", "Équipe support"),
    ("mailbox", "INBOX"), ("identity", "contact@acme.fr"),
    ("canned_response", "Réponse standard paiement"), ("macro", "Macro relance"),
    ("automation_rule", "Règle tri entrant"), ("notification", "Notification relance"),
    ("department", "Finance"), ("job", "Comptable"), ("position", "Comptable"),
    ("category", "Prestations"), ("tag", "Urgent"), ("label", "Urgent"),
    ("session", "Session formation"), ("material", "Matériel"), ("workspace", "Workspace projet"),
    ("view", "Vue Kanban"), ("filter", "Filtre impayés"), ("folder", "Dossier clients"),
    ("template", "Modèle facture"), ("webhook", "Webhook CRM"), ("stage", "À faire"),
    ("bucket", "À faire"), ("member", "Jean Dupont"), ("guest", "Marie Durand"),
    ("product", "Prestation"), ("event", "Réunion hebdo"), ("file", "Document_facture.pdf"),
    ("document", "Rapport mensuel"), ("attachment", "Fichier joint"), ("property", "Appartement 12"),
    ("car", "Clio de société"), ("vehicle", "Clio de société"), ("schedule", "Planning 2026"),
    ("currency", "EUR"), ("country", "France"), ("payment_term", "30 jours fin de mois"),
    ("pricelist", "Tarif standard"), ("source", "Site web"), ("parent", "TechNova Holdings"),
    ("webhook", "Webhook CRM"), ("discount", "Remise 5 %"), ("payment_method", "Carte bancaire"),
    ("space", "Espace marketing"),
    # target-style keys (prefix patterns win over shorter generic ones)
    ("file_id", "Rapport mensuel"), ("document_id", "Rapport mensuel"),
    ("container_id", "Dossier clients"), ("path", "Rapport mensuel.pdf"),
    ("payment_id", "Paiement P2026-001"), ("tax_id", "TVA 20 %"),
    ("bank_statement", "Relevé bancaire mars"), ("bank_statement_line", "Ligne 1 du relevé mars"),
    ("attendance", "Pointage de Jean Dupont"), ("allocation", "Allocation congés 2026"),
    ("leave", "Congé de Jean Dupont"), ("applicant", "Candidat Marie Durand"),
    ("expense", "Note de frais déplacement"), ("opportunity", "Opportunité TechNova"),
    ("quotation", "Devis D2026-001"), ("submission", "Envoi S-2026-001"),
    ("thread", "Conversation T-001"), ("sieve_script", "Filtres perso"),
    ("push_subscription", "Abonnement push"), ("quota", "Compte contact@acme.fr"),
    ("smtp", "Serveur smtp.acme.fr"), ("vacation_response", "Réponse d'absence été"),
    ("blob", "B-2026-08-13-001"), ("trashbin", "Corbeille"), ("version", "Version v2 du rapport"),
    ("docbuilder", "Rapport mensuel"), ("macro", "Macro relance"),
    ("canned_response", "Réponse standard paiement"), ("automation_rule", "Règle tri entrant"),
    ("notification", "Notification 12"), ("callback", "Webhook CRM"),
]

COMMAND_C_VALUES = {"drop": "drop", "forcesave": "forcesave", "info": "info"}

# model token -> (natural target key, French object phrase incl. article)
TARGET_MAP = {
    "move": ("move", "la facture F2026-001"),
    "journal": ("journal", "le journal de vente"),
    "payment": ("payment", "le paiement P2026-001"),
    "tax": ("tax", "la taxe TVA 20 %"),
    "bank_statement_line": ("bank_statement_line", "la ligne de relevé 1"),
    "currency": ("currency", "le taux de change EUR"),
    "partner": ("partner", "le contact TechNova"),
    "company": ("company", "la société Acme SARL"),
    "lead": ("lead", "le lead TechNova"),
    "team": ("team", "l'équipe support"),
    "opportunity": ("opportunity", "l'opportunité TechNova"),
    "employee": ("employee", "Jean Dupont"),
    "applicant": ("applicant", "Marie Durand"),
    "attendance": ("attendance", "le pointage de Jean Dupont"),
    "department": ("department", "le département Finance"),
    "expense": ("expense", "la note de frais déplacement"),
    "leave": ("leave", "le congé de Jean Dupont"),
    "allocation": ("allocation", "l'allocation congés 2026"),
    "order": ("order", "la commande C2026-001"),
    "line": ("order_line", "la ligne de commande 1"),
    "automation_rule": ("automation_rule", "la règle tri entrant"),
    "campaign": ("campaign", "la campagne été"),
    "canned_response": ("canned_response", "la réponse standard paiement"),
    "inbox": ("inbox", "la boîte Inbox support"),
    "macro": ("macro", "la macro relance"),
    "conversation": ("conversation", "la conversation Acme"),
    "message": ("message", "le message Acme"),
    "notification": ("notification", "la notification 12"),
    "callback": ("callback", "le webhook CRM"),
    "label": ("label", "l'étiquette Urgent"),
    "file": ("path", "le fichier Rapport mensuel.pdf"),
    "trashbin": ("path", "la corbeille"),
    "version": ("path", "le fichier Rapport mensuel.pdf"),
    "editor": ("document", "le document Rapport mensuel"),
    "converter": ("document", "le document Rapport mensuel"),
    "command": ("document", "le document Rapport mensuel"),
    "wopi": ("document", "le document Rapport mensuel"),
    "docbuilder": ("document", "le document Rapport mensuel"),
    "task": ("task", "la tâche Relancer Acme"),
    "project": ("project", "le projet Site web"),
    "bucket": ("bucket", "le panneau À faire"),
    "view": ("view", "la vue Kanban"),
    "comment": ("comment", "le commentaire de Jean"),
    "attachment": ("attachment", "la pièce jointe Fichier joint"),
    "subscription": ("subscription", "l'abonnement tâche"),
    "relation": ("relation", "la relation liée"),
    "email_submission": ("submission", "l'envoi S-2026-001"),
    "thread": ("thread", "la conversation T-001"),
    "mailbox": ("mailbox", "la boîte mail INBOX"),
    "identity": ("identity", "l'identité contact@acme.fr"),
    "blob": ("blob", "la pièce jointe B-2026-08-13-001"),
    "sieve_script": ("sieve_script", "le script de filtrage Filtres perso"),
    "push_subscription": ("push_subscription", "l'abonnement push"),
    "quota": ("quota", "le quota du compte contact@acme.fr"),
    "smtp": ("smtp", "le serveur smtp.acme.fr"),
    "vacation_response": ("vacation_response", "la réponse d'absence été"),
    "email": ("email", "l'email Nouveau message"),
    "share": ("share", "le partage « Dossier clients »"),
}

ARTICLE_RE = re.compile(r"^(le|la|les|un|une|des)\s+|^l'")


def strip_article(phrase: str) -> str:
    """Turn a full French object phrase into an entity label: 'la facture F2026-001' ->
    'Facture F2026-001', 'l'employé Jean Dupont' -> 'Employé Jean Dupont'."""
    s = ARTICLE_RE.sub("", phrase).strip()
    return s[:1].upper() + s[1:] if s else s

# genuinely user-scoped / global actions: no target to extract
NO_TARGET_ALLOWLIST = {
    "tasking.notification.list",
    "file.trashbin.purge",
    "res.company.action_all_company_branches",
    "hr.attendance.get_kiosk_url",
    "crm.team.action_your_pipeline",
    "crm.team.action_opportunity_forecast",
}


def target_for(aname: str) -> tuple[str, str]:
    """Return (target key, French example) for the record an action operates on."""
    parts = aname.split(".")
    candidates = parts[1:-1] if len(parts) >= 3 else (parts[:1] if parts else [])
    if "bank" in candidates:
        model = "bank_statement_line"
    elif candidates:
        model = candidates[-1]
    else:
        model = parts[0] if parts else "resource"
    return TARGET_MAP.get(model, (model, "TechNova"))


def maybe_inject_target(aname: str, schema: dict) -> None:
    """Add a natural-noun target to record-target actions (those carrying an
    id/ids/file_id/container_id arg or with nothing else extractable)."""
    if aname in NO_TARGET_ALLOWLIST:
        return
    groups = ("fields", "path_params", "required_body", "optional_body")
    has_idlike = any(
        f in IDLIKE_FIELDS
        for grp in groups for f, _t, _k in schema.get(grp, []))
    has_extractable = any(
        k in ("extract", "entity")
        for grp in groups for _f, _t, k in schema.get(grp, []))
    if not has_idlike and has_extractable:
        return
    for grp in groups:
        schema[grp] = [(f, t, k) for f, t, k in schema.get(grp, [])
                       if f not in IDLIKE_FIELDS]
    key, phrase = target_for(aname)
    schema.setdefault("fields", []).append((key, phrase, "target"))


def french_verb(verb_tok: str) -> str:
    direct = FRENCH_VERBS.get(verb_tok)
    if direct:
        return direct
    base = verb_tok.split("_", 1)[1] if (verb_tok.startswith("button_")) else verb_tok
    base = base.removeprefix("action_")
    if base.startswith("conseil"):
        return FRENCH_VERBS.get("conseil", "Exécuter")
    return FRENCH_VERBS.get(base, "Exécuter")


def humanize(name: str) -> str:
    return name.replace("_", " ")


def date_fr(d: str) -> str:
    try:
        d = str(d).split("T")[0].rstrip("Z")
        y, m, day = d.split("-")
        return f"{day}/{m}/{y}"
    except Exception:
        return str(d)


def entity_example(fname: str) -> str:
    n = fname.lower()
    for pat, val in ENTITY_EXAMPLES:
        if pat in n:
            return val
    return "TechNova"


def example_value(fname: str, ftype: str, kind: str, aname: str) -> object:
    n = fname.lower()
    t = ftype.lower()
    if kind == "entity":
        if "line" in n:
            return [{"name": "Prestation", "price_unit": 1200.0, "quantity": 1}]
        if t.startswith("list") or n.endswith("ids"):
            return [entity_example(fname)]
        return entity_example(fname)
    if n == "envelope":
        return {"rcptTo": ["contact@technova.com"], "subject": "Nouveau message"}
    if ".set" in aname and n in ("create", "update", "destroy", "ifInState"):
        return {
            "create": {"M123": {"emailIds": ["M456"],
                                 "envelope": {"rcptTo": ["contact@technova.com"]}}},
            "update": {"M123": {"undoStatus": "pending"}},
            "destroy": ["M123"],
            "ifInState": "1f9c2eab7d",
        }[n]
    if aname.startswith("office.command") and n == "c":
        return COMMAND_C_VALUES.get(aname.split(".")[-1], "drop")
    if n == "c":
        return "pdf"
    if n in ("done", "publicUpload", "sendMail", "sendPasswordByTalk", "active", "archived",
             "is_company", "is_favorite", "favorite"):
        return True
    if n == "vals_list":
        return [{"name": "Prestation", "price_unit": 1200.0, "quantity": 1}]
    if n in ("vals", "write_values", "values"):
        return {"name": "Nouveau nom"}
    if n in ("order_line", "line_ids", "task_ids", "move_line_ids"):
        return [{"name": "Prestation", "price_unit": 1200.0, "quantity": 1}]
    if n in ("invoice_day", "day_done", "scheduled_day", "delivery_day", "day",
             "day_order", "validity_date", "invoice_day_due","due_day","start_day"):
        return "13"
    if n in ("invoice_month", "month_done", "scheduled_month", "delivery_month", "month",
             "month_order", "validity_month", "invoice_month_due","due_month","start_month"):
        return "aout"
    if n in ("invoice_year", "year_done", "scheduled_year", "delivery_year", "year",
             "year_order", "validity_year", "invoice_year_due","due_year","start_year"):
        return "2026"
    if n in ("due_day", "deadline", "end_day", "expireDay", "expiry_day", "send_day",
             "repeat_day", "date_deadline"):
        return "14"
    if n in("due_month","end_month","expireMonth","expiry_month","send_month","repeat_month","date_deadline_month"):
        return "aout"
    if n in("due_year","end_year","expireYear","expiry_year","send_year","repeat_year","date_deadline_year"):
        return "2026"
    if n == "snoozed_until":
        return "2026-08-21T09:00:00Z"

    if n in ("last_sync", "last_activity", "updated_at", "created_at"):
        return "2026-08-13T10:30:00Z"
    if "timezone" in n or n == "tz":
        return "Europe/Paris"
    if "priority" in n:
        return "high"
    if n == "move_type":
        return "out_invoice"
    if n in ("status", "state", "lifecycle"):
        return "open"
    if n == "type":
        return "standard"
    if "lang" in n:
        return "fr"
    if n in ("currency", "currency_code"):
        return "EUR"
    if "phone" in n:
        return "+33 1 23 45 67 89"
    if n in ("recipients", "to", "cc", "bcc", "mail_to", "mail_cc"):
        return ["contact@technova.com", "jean@dupont.fr"]
    if "email" in n or n in ("rcptTo", "mail_from"):
        return "contact@technova.com"
    if n in ("recipient", "from", "sender"):
        return "Jean Dupont"
    if n in ("format", "filetype", "outputtype", "convert_to", "ext"):
        return "pdf"
    if "url" in n or n in ("uri", "link", "callback", "webhook"):
        return "https://exemple.com/documents/facture.pdf"
    if "color" in n:
        return "#E53935"
    if n in ("percent_done", "percent"):
        return 100
    if "price" in n or "amount" in n or "cost" in n or "total" in n or "rate" in n:
        return 1200.0
    if "qty" in n or "quantity" in n or "count" in n or "size" in n:
        return 2
    if n == "repeat_after":
        return 604800
    if n == "repeat_mode":
        return "monthly"
    if n == "repeat_interval":
        return 1
    if "reminder" in n:
        return [{"unit": "days", "amount": 1}]
    if n == "assignees":
        return ["Jean Dupont"]
    if n in ("tags", "labels"):
        return ["Urgent"]
    if n == "custom_attributes":
        return {"sla": "gold", "region": "EU"}
    if n == "attributes":
        return {"permissions": ["read", "update"]}
    if n == "permissions":
        return ["read", "update"]
    if n == "title":
        return "Relancer Acme"
    if n == "name":
        return "Prestation"
    if n == "subject":
        return "Nouveau message"
    if n in ("description", "narration", "note", "notes", "body", "html_body", "plain_body",
             "comment", "comments", "content", "summary"):
        return "Relancer la facture F2026-001"
    if n in ("hex_color",):
        return "#E53935"
    if "int" in t:
        return 2
    if "float" in t:
        return 1200.5
    if "bool" in t:
        return True
    if "list" in t:
        return ["A", "B"]
    if "dict" in t:
        return {}
    return "exemple"


def _compact(value: object) -> str:
    if isinstance(value, list):
        if value and isinstance(value[0], (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return ", ".join(str(x) for x in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


BOOL_CLAUSES = {
    "done": "faite", "is_favorite": "en favori", "favorite": "en favori",
    "active": "active", "archived": "archivée", "is_company": "en tant que société",
    "publicUpload": "en téléversement public", "sendMail": "avec envoi d'email",
    "sendPasswordByTalk": "avec envoi du mot de passe par Talk",
}


def clause_for(fname: str, value: object, kind: str) -> str:
    n = fname.lower()
    v = value
    if kind == "entity":
        if isinstance(v, list):
            return f"{humanize(fname)} « {_compact(v)} »"
        if "partner" in n or "customer" in n or "vendor" in n or "supplier" in n:
            return f"client {v}"
        if "company" in n:
            return f"société {v}"
        if "employee" in n or "user" in n or "assignee" in n or "responsible" in n:
            return f"par {v}"
        if "project" in n:
            return f"projet {v}"
        if "task" in n:
            return f"tâche {v}"
        if "conversation" in n:
            return f"conversation {v}"
        if "campaign" in n:
            return f"campagne {v}"
        if "calendar" in n:
            return f"calendrier {v}"
        if "journal" in n:
            return f"journal {v}"
        if "account" in n:
            return f"compte {v}"
        if "inbox" in n:
            return f"boîte {v}"
        if "mailbox" in n:
            return f"boîte {v}"
        if "identity" in n:
            return f"identité {v}"
        if "space" in n:
            return f"espace {v}"
        if "team" in n:
            return f"équipe {v}"
        if "move" in n:
            return f"facture {v}"
        if "order" in n:
            return f"commande {v}"
        if "contact" in n:
            return f"contact {v}"
        if "lead" in n:
            return f"lead {v}"
        if "currency" in n:
            return f"en {v}"
        if "country" in n:
            return f"pays {v}"
        base = humanize(fname).removesuffix(" id")
        return f"{base} « {v} »"
    if n == "envelope":
        return f"à envoyer à {v.get('rcptTo', 'contact@technova.com')}"
    if n == "c":
        return f"au format {v}" if str(v) == "pdf" else f"commande {v}"
    if n == "title":
        return f"intitulé « {v} »"
    if n == "subject":
        return f"objet « {v} »"
    if n == "name":
        return f"nom « {v} »"
    if n in ("vals", "write_values", "values"):
        return f"avec les nouvelles valeurs {json.dumps(v, ensure_ascii=False)}"
    if n in ("description", "narration", "note", "notes", "body", "html_body", "plain_body",
             "comment", "content", "summary"):
        return f"description « {v} »"
    if n in ("due_date", "deadline", "end_date", "expireDate", "expiry_date", "send_date"):
        return f"échéance le {date_fr(v)}"
    if n in ("start_date", "date_start", "start", "begin"):
        return f"du {date_fr(v)}"
    if n in ("invoice_date", "date", "date_order", "validity_date", "invoice_date_due",
             "snoozed_until"):
        return f"le {date_fr(v)}"
    if "timezone" in n or n == "tz":
        return f"fuseau horaire {v}"
    if "priority" in n:
        return f"priorité {v}"
    if n in ("status", "state"):
        return f"statut « {v} »"
    if "lang" in n:
        return f"langue « {v} »"
    if n in ("currency", "currency_code"):
        return f"en {v}"
    if "phone" in n:
        return f"téléphone {v}"
    if n in ("recipients", "to", "cc", "bcc", "mail_to", "mail_cc"):
        if isinstance(v, list):
            return f"email {', '.join(v)}"
        return f"email {v}"
    if "email" in n or n in ("rcptTo", "mail_from"):
        if isinstance(v, list):
            return f"email {', '.join(v)}"
        return f"email {v}"
    if n in ("recipient", "from", "sender"):
        return f"à {v}"
    if n in ("format", "filetype", "outputtype", "convert_to", "ext"):
        return f"au format {v}"
    if "url" in n or n in ("uri", "link", "callback", "webhook"):
        return f"à {v}"
    if "color" in n:
        return f"couleur {v}"
    if "price" in n or "amount" in n or "cost" in n or "total" in n or "rate" in n:
        return f"montant {v} €"
    if "qty" in n or "quantity" in n:
        return f"quantité {v}"
    if "count" in n or "size" in n:
        return f"{humanize(fname)} {v}"
    if isinstance(v, bool):
        return BOOL_CLAUSES.get(n, f"{'avec' if v else 'sans'} {humanize(fname)}")
    if n == "move_type":
        return f"de type {v}"
    if isinstance(v, (int, float)):
        return f"{humanize(fname)} {v}"
    if isinstance(v, (list, dict)):
        return f"{humanize(fname)} « {_compact(v)} »"
    return f"{humanize(fname)} « {v} »"


def collect_fields(schema: dict) -> dict[str, tuple[str, str]]:
    """Return {fname: (ftype, kind)} for all classified fields, dedup, sorted.

    Field names are mapped to their clear user-facing name (see
    CLEAR_FIELD_NAMES) so that prompts, examples, the spec and the eval ground
    truth all share the same extraction contract.
    """
    out: dict[str, tuple[str, str]] = {}
    for grp in ("fields", "path_params", "required_body", "optional_body"):
        for fname, ftype, kind in schema.get(grp, []):
            out.setdefault(clear_field_name(fname), (ftype, kind))
    return dict(sorted(out.items()))


EXAMPLE_PHRASE_OVERRIDES = {
    "email.email_submission.set": ("Envoyer", "l'email"),
    "file.mkcol": ("Créer le dossier", "dans l'espace de travail"),
    "file.propfind": ("Consulter les métadonnées", "du fichier"),
    "file.proppatch": ("Modifier les métadonnées", "du fichier"),
    "kb.answer.grounded": ("Répondre", "avec sources"),
    "kb.export.pdf": ("Exporter", "le rapport"),
    "kb.generate.image": ("Générer", "une infographie"),
    "kb.generate.podcast": ("Générer", "un podcast"),
    "kb.generate.report": ("Générer", "un rapport"),
    "kb.generate.video": ("Générer", "une vidéo"),
    "kb.ingest.document": ("Importer", "les documents"),
    "kb.ingest.url": ("Importer", "l'URL"),
    "kb.ingest.youtube": ("Importer", "la vidéo YouTube"),
    "kb.note.create": ("Créer", "une note"),
    "kb.search.fulltext": ("Rechercher", "les documents contenant le terme"),
    "kb.search.vector": ("Rechercher", "les documents abordant le thème"),
    "kb.space.create": ("Créer", "un espace de connaissance"),
    "kb.space.delete": ("Supprimer", "l'espace"),
    "kb.space.read": ("Consulter", "l'espace"),
    "kb.space.update": ("Modifier", "l'espace de connaissance"),
    "kb.status.batch": ("Consulter", "le statut du lot"),
    "kb.status.processing": ("Consulter", "le statut de traitement"),
    "conseil personnel": ("Donner", "un conseil"),
    "conseil professionnel - rh": ("Donner", "un conseil"),
    "conseil professionnel - accounting": ("Donner", "un conseil"),
    "conseil professionnel - administration": ("Donner", "un conseil"),
    "conseil professionnel - artisanat": ("Donner", "un conseil"),
    "conseil professionnel - automobile": ("Donner", "un conseil"),
    "conseil professionnel - finance": ("Donner", "un conseil"),
    "conseil professionnel - fiscalité": ("Donner", "un conseil"),
    "conseil professionnel - juridique": ("Donner", "un conseil"),
    "conseil professionnel - management": ("Donner", "un conseil"),
    "conseil professionnel - marketing": ("Donner", "un conseil"),
    "conseil professionnel - édition": ("Donner", "un conseil"),
    "general": ("Répondre", "à la question"),
    "redaction": ("Rédiger", "un texte"),
    "resume": ("Résumer", "le contenu"),
    "traduction": ("Traduire", "le texte"),
}


def build_example(aname: str, schema: dict, domain: str) -> tuple[str, dict]:
    parts = aname.split(".")
    verb_tok = parts[-1]
    noun_tok = parts[-2] if len(parts) >= 2 else "resource"
    verb, noun = EXAMPLE_PHRASE_OVERRIDES.get(
        aname, (french_verb(verb_tok),
                FRENCH_NOUNS.get(noun_tok, f"la ressource « {noun_tok} »")))
    clauses: list[str] = []
    fields: dict = {}
    target_phrase: str | None = None
    for fname, (ftype, kind) in collect_fields(schema).items():
        if kind == "system":
            continue
        if kind == "target":
            target_phrase = ftype
            fields[fname] = strip_article(ftype)
            continue
        val = example_value(fname, ftype, kind, aname)
        fields[fname] = val
        clauses.append(clause_for(fname, val, kind))
    if target_phrase is not None:
        query = f"{verb} {target_phrase}"
        if clauses:
            query += ", " + ", ".join(clauses)
    else:
        query = f"{verb} {noun}"
        if clauses:
            query += ", " + ", ".join(clauses)
    query = query[0].upper() + query[1:]
    return query, fields


def normalize_rest_action(body: str) -> dict:
    """Parse method/path/path_params/required/optional style blocks."""
    result = {"method": "", "path": "", "path_params": [], "required_body": [],
              "optional_body": [], "notes": ""}
    for key in ("method", "path", "endpoint", "jmap_method", "notes"):
        m = re.search(rf"['\"]{key}['\"]\s*:\s*['\"]([^'\"]*)['\"]", body)
        if m:
            result[key] = m.group(1)
    if not result["path"] and result.get("endpoint"):
        result["path"] = result["endpoint"]
    for grp, key in (("path_params", "path_params"),
                     ("required_body", "required_body"),
                     ("optional_body", "optional_body"),
                     ("required", "required"),
                     ("optional", "optional")):
        target = {"required": "required_body", "optional": "optional_body"}.get(grp, grp)
        m = re.search(rf"['\"]{key}['\"]\s*:\s*set\(\)", body)
        if m:
            continue
        m = re.search(rf"['\"]{key}['\"]\s*:\s*\{{(.*?)\}}", body, re.S)
        if not m:
            continue
        inner = m.group(1)
        if "same keys" in inner:
            continue
        if ":" in inner:
            # dict-style: 'key': 'value'
            for fm in re.finditer(r"['\"]([a-zA-Z0-9_\[\].]+)['\"]\s*:\s*['\"]([^'\"]*)['\"]", inner):
                result[target].append((fm.group(1), fm.group(2), classify_field(fm.group(1))))
        else:
            # set-style: { 'key', 'key2' }
            for fm in re.finditer(r"['\"]([a-zA-Z0-9_\[\].]+)['\"]", inner):
                result[target].append((fm.group(1), "str", classify_field(fm.group(1))))
    if not result["required_body"] and not result["optional_body"] and not result["path_params"]:
        # office-style: flat fields with endpoint/method line
        flat: list[tuple[str, str, str]] = []
        for fm in re.finditer(r"^            ['\"]([a-zA-Z0-9_.]+)['\"]\s*:\s*['\"]([^'\"]*)['\"]", body, re.M):
            fname, fdesc = fm.group(1), fm.group(2)
            if fname in ("endpoint", "method", "path", "notes"):
                continue
            if fname == "" or "no args" in fdesc:
                continue
            ftype, req = parse_field(fdesc)
            flat.append((fname, ftype, classify_field(fname)))
            _ = req
        result["fields"] = flat
    return result


def parse_action_block(name: str, body: str) -> dict | None:
    """Parse one action dict body -> normalized schema dict."""
    has_structured = ("'required'" in body or '"required"' in body
                      or "required_body" in body or "jmap_method" in body
                      or "path_params" in body)
    has_endpoint = "endpoint" in body
    if has_structured or has_endpoint:
        return normalize_rest_action(body)
    fields: list[tuple[str, str, str]] = []
    for fm in re.finditer(r"['\"]([a-zA-Z0-9_.]+)['\"]\s*:\s*['\"]([^'\"]*)['\"]", body):
        fname, fdesc = fm.group(1), fm.group(2)
        if fname in ("endpoint", "method", "path", "notes"):
            continue
        if fname == "" or "no args" in fdesc:
            continue
        ftype, req = parse_field(fdesc)
        kind = "required_body" if req else "optional_body"
        fields.append((fname, ftype, classify_field(fname)))
        _ = kind
    return {"method": "", "path": "", "path_params": [], "required_body": [],
            "optional_body": [], "fields": fields, "notes": ""}


def action_to_text(name: str, schema: dict, domain: str) -> str:
    extract = [clear_field_name(f[0]) for f in schema.get("fields", []) if f[2] == "extract"]
    entity = [clear_field_name(f[0]) for f in schema.get("fields", []) if f[2] == "entity"]
    system = [clear_field_name(f[0]) for f in schema.get("fields", []) if f[2] == "system"]
    target = [clear_field_name(f[0]) for f in schema.get("fields", []) if f[2] == "target"]
    for grp in ("required_body", "optional_body", "path_params"):
        for fname, ftype, kind in schema.get(grp, []):
            if kind == "extract":
                extract.append(clear_field_name(fname))
            elif kind == "entity":
                entity.append(clear_field_name(fname))
            elif kind == "target":
                target.append(clear_field_name(fname))
            else:
                system.append(clear_field_name(fname))
    for grp in ("required_body", "optional_body", "path_params"):
        for fname, ftype, kind in schema.get(grp, []):
            _ = (fname, ftype, kind)

    method = schema.get("method", "")
    path = schema.get("path", "")
    endpoint = f"{method} {path}".strip()
    if not endpoint:
        fallback = {
            "accounting": "odoo RPC (accounting)",
            "contacts": "odoo RPC (res.partner)",
            "crm": "odoo RPC (crm.lead)",
            "hr": "odoo RPC (hr.*)",
            "sales": "odoo RPC (sale.order)",
            "calendaring": "CalDAV (calendar server)",
            "file_management": "WebDAV (Nextcloud)",
            "knowledge_kb": "SurfSense KB API",
        }
        endpoint = fallback.get(domain, "odoo RPC")

    lines = [f"### `{name}`  ", ""]
    lines.append(f"- **endpoint**: `{endpoint}`  ")
    if schema.get("notes"):
        lines.append(f"- **notes**: {schema['notes']}  ")
    lines.append("- **extractable from query** (model extracts): "
                 + (", ".join(sorted(set(extract))) if extract else "*none*") + "  ")
    lines.append("- **entity → id lookup** (system resolves): "
                 + (", ".join(sorted(set(entity))) if entity else "*none*") + "  ")
    if target:
        lines.append("- **target (no-arg action)**: model extracts the record name, "
                     "system resolves id: `" + ", ".join(sorted(target)) + "`  ")
    lines.append("- **system-injected** (auth/id/key/secret): "
                 + (", ".join(sorted(set(system))) if system else "*none*") + "  ")
    lines.append("")
    lines.append("```jsonc")
    sample_keys = list(sorted(set(extract))) + list(sorted(set(target)))
    sample = {"action": name, "fields": {f: "<extracted from query>" for f in sample_keys[:4]}}
    if not sample["fields"]:
        sample["fields"] = {}
    lines.append(json.dumps(sample, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    query, ex_fields = build_example(name, schema, domain)
    lines.append("**Exemple (query → model output)**")
    lines.append("")
    lines.append("```jsonc")
    lines.append(f"// Query: « {query} »")
    lines.append(json.dumps({"action": name, "fields": ex_fields}, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def load_curated_defs() -> dict[str, dict[str, list[list[str]]]]:
    """Load curated user-facing field definitions per domain.

    Each file is `{"<domain>": {"<action_id>": [["field","type","kind"], ...]}}`.
    The curated defs REPLACE the technical parsed schemas (Odoo flat / REST /
    JMAP / WOPI plumbing) with a user-facing extraction contract, and provide
    the schemas that the source file does not document at all (`general`,
    `knowledge_kb`).
    """
    defs: dict[str, dict[str, list[list[str]]]] = {}
    if not CURATED_DEFS_DIR.is_dir():
        return defs
    for path in sorted(CURATED_DEFS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for domain, actions in data.items():
            if not isinstance(actions, dict):
                continue
            defs.setdefault(domain, {})
            for aname, fields in actions.items():
                if not isinstance(fields, list):
                    continue
                defs[domain][aname] = fields
    return defs


def apply_curated_defs(actions: dict[str, dict[str, dict]]) -> None:
    """Replace every action schema with its curated user-facing field list.

    Curated defs exist for all 13 domains (including `general` and
    `knowledge_kb` which the source file does not document). Any action in the
    catalog without a curated def keeps its parsed schema, so a later catalog
    addition still degrades gracefully.
    """
    curated = load_curated_defs()
    for domain, actions_in_domain in curated.items():
        for aname, fields in actions_in_domain.items():
            schema = {
                "method": "",
                "path": "",
                "path_params": [],
                "required_body": [],
                "optional_body": [],
                "fields": [tuple(f) for f in fields if len(f) == 3],
                "notes": "Champs définis manuellement (curated) : "
                         "interface utilisateur au lieu du schéma technique source.",
            }
            actions.setdefault(domain, {})[aname] = schema


def parse_all_actions(verbose: bool = False) -> dict[str, dict[str, dict]]:
    """Load {domain: {action: schema}} from curated JSON defs in tools/attr_defs/.

    Each file is {"<domain>": {"<action_id>": [["field","type","kind"], ...]}}.
    These REPLACE the technical parsed schemas with a user-facing extraction contract.
    """
    actions: dict[str, dict[str, dict]] = {}
    if not CURATED_DEFS_DIR.is_dir():
        return actions
    for path in sorted(CURATED_DEFS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for domain, domain_actions in data.items():
            if not isinstance(domain_actions, dict):
                continue
            actions.setdefault(domain, {})
            for aname, fields in domain_actions.items():
                if not isinstance(fields, list):
                    continue
                schema = {
                    "method": "",
                    "path": "",
                    "path_params": [],
                    "required_body": [],
                    "optional_body": [],
                    "fields": [tuple(f) for f in fields if len(f) == 3],
                    "notes": "Champs definis manuellement (curated) : "
                             "interface utilisateur au lieu du schema technique source.",
                }
                actions[domain][aname] = schema
    if verbose:
        for dom, acts in sorted(actions.items()):
            print(f"{dom}: {len(acts)} actions")
    return actions


def main() -> None:
    actions = parse_all_actions(verbose=True)

    # cross-check with the action classifier catalog
    import sys
    sys.path.insert(0, r"C:\Bureau\activiity_ai_engine")
    from server.agents.action_classifier import ACTION_IDS_BY_DOMAIN

    print("=== coverage vs ACTION_IDS_BY_DOMAIN ===")
    total_actions = 0
    for dom, ids in ACTION_IDS_BY_DOMAIN.items():
        have = set(actions.get(dom, {}))
        missing = [a for a in ids if a not in have]
        total_actions += len(ids)
        print(f"{dom:16s} {len(ids):3d} actions  missing={len(missing)}")
        if missing:
            print("   ", missing[:10])
    print("total actions:", total_actions)

    md = ["# Attribute Extraction Spec (model ↔ system contract)", ""]
    md.append("_Generated from `ACTIONS attributes.txt` (3 schemas normalized: Odoo flat / "
              "REST method-path / JMAP / WOPI) plus the curated user-facing field defs in "
              "`tools/attr_defs/` (which cover all 13 domains, including `general` and "
              "`knowledge_kb` that the source file does not document). Source: "
              "`tools/gen_attribute_spec.py`. "
              "Each action shows a synthetic French example (query → model output)._")
    md.append("")
    md.append("## Pipeline")
    md.append("`domain → action → attribute extraction (per domain, joint over its actions)`")
    md.append("")
    md.append("## Contract")
    md.append("""
- **Model** emits ONLY what is derivable from the query text, keyed by schema field name.
- **System merge** (deterministic, after all extraction calls) injects: endpoint, method,
  path_params, auth (account_id, Authorization, access_token, JWT), keys, secrets,
  and resolves entity names to ids via lookup actions.
- `entity` fields: user names an entity (e.g. "TechNova"); the model passes the text
  value, the system resolves the id (e.g. `res.partner.search_read`) and substitutes it.
- `system` fields are never in the prompt as generate-able values.
- Stage 3 (attribute extraction) is the **final step**, so its output is the **terminal
  contract**. Each domain call returns `{action_id: {"fields": {...}}}` grouped per domain;
  the orchestrator wraps the per-domain outputs into a single envelope:
  `{"domains": [...], "actions": [...], "fields": {<domain>: {<action>: {"fields": {...}}}}}`.
""")
    md.append("## Field classification (per action)")
    md.append("""
Each field in the reference below is one of:
- **extractable from query** → the model must emit it (value copied from the text).
- **entity → id lookup** → the model emits the entity text; the system substitutes the id.
- **system-injected** → account id / auth / key / token / secret / endpoint plumbing;
  never emitted by the model, added by the merge step.
""")
    md.append("")
    md.append("## Single-intent example")
    md.append("""```jsonc
// Query: « Crée la tâche "Relancer Acme" pour demain, priorité haute »
// Pipeline: general? no -> tasking -> [tasking.task.create] -> attributes

// Stage-3 output (terminal contract: domains + actions + per-domain/per-action fields)
{
  "domains": ["tasking"],
  "actions": ["tasking.task.create"],
  "fields": {
    "tasking": {
      "tasking.task.create": {
        "fields": { "title": "Relancer Acme", "due_date": "2026-08-14", "priority": "high" }
      }
    }
  }
}

// System merge (final executable payload)
{
  "action": "tasking.task.create",
  "endpoint": "POST /api/v2/projects/{project}/tasks",
  "method": "POST",
  "path_params": { "project": "<system: current project>" },
  "required_body": { "title": "Relancer Acme" },
  "optional_body": { "due_date": "2026-08-14", "priority": "high" },
  "headers": { "Authorization": "<session token>" }
}
```""")
    md.append("")
    md.append("## Multi-intent example (multi-domain, multi-action)")
    md.append("""```jsonc
// Query: « Crée la facture client à TechNova pour 1200€ HT, valide-la,
//          envoie-la par email et crée une tâche de suivi »
// Pipeline: [accounting, email_action, tasking]
//   accounting : [account.move.create, account.move.action_post]
//   email_action: [email.email_submission.set]
//   tasking    : [tasking.task.create]

// Stage-3 outputs (one model call per domain)
{
  "accounting": {
    "account.move.create": {
      "fields": { "move_type": "out_invoice", "partner_id": "TechNova",
                  "invoice_line_ids": [{"name": "Prestation", "price_unit": 1200.0, "quantity": 1}] }
    },
    "account.move.action_post": { "fields": {} }
  },
  "email_action": {
    "email.email_submission.set": {
      "fields": { "envelope": { "rcptTo": ["contact@technova.com"] } }
    }
  },
  "tasking": {
    "tasking.task.create": { "fields": { "title": "Suivi facture TechNova" } }
  }
}

// Terminal output (envelope added by the orchestrator)
{
  "domains": ["accounting", "email_action", "tasking"],
  "actions": ["account.move.create", "account.move.action_post",
              "email.email_submission.set", "tasking.task.create"],
  "fields": {
    "accounting": {
      "account.move.create": { "fields": { "move_type": "out_invoice", "partner_id": "TechNova",
                                            "invoice_line_ids": [{ "name": "Prestation",
                                                                   "price_unit": 1200.0,
                                                                   "quantity": 1 }] } },
      "account.move.action_post": { "fields": {} }
    },
    "email_action": {
      "email.email_submission.set": { "fields": { "envelope": { "rcptTo": ["contact@technova.com"] } } }
    },
    "tasking": {
      "tasking.task.create": { "fields": { "title": "Suivi facture TechNova" } }
    }
  }
}

// System merge
//  - account.move.create -> odoo RPC, partner_id resolved to 47
//  - account.move.action_post -> ids = ref: account.move.create.result_id
//  - email.email_submission.set -> accountId/Authorization injected,
//       emailIds = ref: account.move.create (attachment)
//  - tasking.task.create -> project id + Authorization injected
```""")
    md.append("")
    md.append("## Evaluation")
    md.append("""
- Ground truth per row = `{domain: {action: {fields: {...}}}}` with ONLY extractable keys.
- **Exact** = predicted field key-set == true key-set for every action of the row.
- **Partial** = every true extractable key is present in prediction (recall = 1), per row.
- Per-field precision/recall/f1 computed over all rows.
- `entity` fields ARE scored: the model must emit the entity **text** (e.g. "TechNova");
  only the id **substitution** is system-side and therefore not scored.
- `system` fields are never scored (auth/ids/endpoint plumbing).
""")
    md.append("")
    md.append("## Preferred test-data format (this phase)")
    md.append("""
`attribute_classification_data_v1.xlsx`, one row per query:

| id | query | tool | domains | actions | expected_fields |
|----|-------|------|---------|---------|-----------------|
| 1  | « Crée la tâche "Relancer Acme" pour demain » | Vikunja | tasking | tasking.task.create | `{"tasking.task.create":{"fields":{"title":"Relacer Acme","due_date":"2026-08-14"}}}` |

- `actions`: comma-separated, ordered.
- `expected_fields`: JSON keyed by action_id → `{"fields": {<schema field>: <value>}}`.
- Values stored for future strict eval; phase-1 metric uses key presence only.
- Suggested split: ~70% single-intent, ~30% multi-intent (multi-domain + multi-action-per-domain).
""")
    md.append("")
    md.append("---")
    md.append("")
    md.append("# Per-action output format reference")
    md.append("")

    for dom in sorted(actions.keys()):
        md.append(f"## {dom}  ")
        md.append("")
        for aname in sorted(actions[dom].keys()):
            md.append(action_to_text(aname, actions[dom][aname], dom))
            md.append("")

    OUT_FILE.write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {OUT_FILE} ({OUT_FILE.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
