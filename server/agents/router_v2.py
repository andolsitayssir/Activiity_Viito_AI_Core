from __future__ import annotations

import os
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from server.config import load_environment

load_environment()

ROUTING_LABELS: tuple[str, ...] = (
    "odoo", "chatwoot", "vikunja", "onlyoffice",
    "nextcloud", "surfsence", "stalwart", "general_question",
)
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "prompt_router_v3_fr_fs.md"


LABEL_ALIASES = {
    "odoo": "odoo",
    "crm": "odoo",
    "erp": "odoo",
    "finance": "odoo",
    "oddo": "odoo",
    "chatwoot": "chatwoot",
    "vikunja": "vikunja",
    "task": "vikunja",
    "tasks": "vikunja",
    "project": "vikunja",
    "onlyoffice": "onlyoffice",
    "only_office": "onlyoffice",
    "only-office": "onlyoffice",
    "document": "onlyoffice",
    "documents": "onlyoffice",
    "doc": "onlyoffice",
    "spreadsheet": "onlyoffice",
    "presentation": "onlyoffice",
    "nextcloud": "nextcloud",
    "calendar": "nextcloud",
    "calendrier": "nextcloud",
    "fichiers": "nextcloud",
    "files": "nextcloud",
    "surfsence": "surfsence",
    "rag": "surfsence",
    "retrieval": "surfsence",
    "retrieve": "surfsence",
    "knowledge": "surfsence",
    "knowledge_base": "surfsence",
    "base_de_connaissance": "surfsence",
    "documentation": "surfsence",
    "stalwart": "stalwart",
    "email": "stalwart",
    "mail": "stalwart",
    "smtp": "stalwart",
    "general": "general_question",
    "general_question": "general_question",
    "general-question": "general_question",
    "question": "general_question",
    "faq": "general_question",
}

ODOO_HINTS_EN = (
    "odoo", "erp", "crm", "finance", "lead", "leads", "opportunity",
    "opportunities", "deal", "deals", "invoice", "invoices", "delivery",
    "deliveries", "payment", "payments", "purchase", "purchases", "sale",
    "sales", "billing", "subscription", "marketing campaign", "mass mailing",
    "customer", "partner", "company", "product", "products", "commercial",
    "amount", "currency", "tax", "pipeline", "quote", "quotation", "order",
    "orders", "price", "journal", "accounting", "timesheet", "expense",
    "expenses", "manufacturing", "inventory", "warehouse", "hr",
    "purchase order", "rfq", "bill of materials", "stock",
)
ODOO_HINTS_FR = (
    "odoo", "erp", "crm", "finance", "prospect", "prospects", "opportunité",
    "opportunités", "affaire", "affaires", "facture", "factures", "livraison",
    "livraisons", "paiement", "paiements", "achat", "achats", "vente", "ventes",
    "facturation", "abonnement", "rapport commercial", "rapport de vente",
    "campagne mailing", "client", "clients", "partenaire", "partenaires",
    "entreprise", "produit", "produits", "commercial", "montant", "devise",
    "taxe", "pipeline", "devis", "commande", "commandes", "prix", "journal",
    "comptabilité", "feuille de temps", "dépense", "dépenses", "fabrication",
    "inventaire", "entrepôt", "rh", "bon de commande", "rfq", "nomenclature",
    "stock", "mouvement de stock",
)
VIKUNJA_HINTS = (
    "vikunja", "task", "tasks", "project", "projects", "todo", "todos",
    "list", "lists", "checklist", "checklists", "subtask", "subtasks",
    "due date", "due dates", "priority", "priorities", "kanban", "bucket",
    "buckets", "comment", "comments", "attachment", "attachments", "relation",
    "relations", "blocking", "completed", "done",
)
VIKUNJA_HINTS_FR = (
    "vikunja", "tâche", "tâches", "projet", "projets", "à faire", "listes",
    "liste", "checklist", "checklists", "sous-tâche", "sous-tâches",
    "date d'échéance", "dates d'échéance", "priorité", "priorités", "kanban",
    "bucket", "buckets", "commentaire", "commentaires", "pièce jointe",
    "pièces jointes", "relation", "relations", "bloquant", "terminé", "terminée",
)
CHATWOOT_HINTS = (
    "chatwoot", "conversation", "conversations", "ticket", "inbox", "inboxes",
    "agent", "agents", "canned response", "canned responses", "macro", "macros",
    "contact client", "profil client", "boîte de réception", "réponse préenregistrée",
    "assigner conversation", "rapport agent", "temps de réponse",
)
CHATWOOT_HINTS_FR = (
    "chatwoot", "conversation", "conversations", "ticket", "boîte de réception",
    "boîtes de réception", "agent", "agents", "macro", "macros",
    "réponse préenregistrée", "réponses préenregistrées", "contact client",
    "profil client", "historique échanges", "assigner conversation",
    "rapport agent", "temps de réponse", "répondre client", "support client",
    "chat", "whatsapp", "telegram", "réseaux",
)
ONLYOFFICE_HINTS = (
    "onlyoffice", "document", "documents", "spreadsheet", "presentation",
    "pdf", "formulaire", "tableur", "diapositive", "mise en forme",
    "police", "gras", "tableau", "graphique", "tcd", "macro document",
    "corriger orthographe", "traduire texte", "convertir pdf", "sauvegarder document",
    "supprimer document", "partager document", "modifier contenu",
)
ONLYOFFICE_HINTS_FR = (
    "onlyoffice", "document", "documents", "tableur", "présentation",
    "pdf", "formulaire", "diapositives", "mise en forme", "police",
    "gras", "tableau", "graphique", "tcd", "macro", "corriger orthographe",
    "traduire texte", "convertir pdf", "sauvegarder document", "supprimer document",
    "partager document", "modifier contenu", "rédiger", "rédiger document",
    "créer document", "contenu document",
)
NEXTCLOUD_HINTS = (
    "nextcloud", "calendar", "calendrier", "event", "événement", "agenda",
    "meeting", "réunion récurrente", "rendez-vous", "heure précise",
    "inviter participants", "consulter agenda",
    "upload", "téléverser", "déplacer fichier", "renommer fichier",
    "chercher fichier", "verrouiller fichier", "taguer fichier",
    "lien de partage", "restaurer version", "synchronisation", "règle accès",
    "fichier", "fichiers", "dossier", "dossiers",
)
NEXTCLOUD_HINTS_FR = (
    "nextcloud", "calendrier", "événement", "événements", "agenda",
    "réunion récurrente", "rendez-vous", "heure précise", "inviter participants",
    "consulter agenda", "créer événement", "déplacer événement", "supprimer événement",
    "téléverser", "déplacer fichier", "renommer fichier", "chercher fichier",
    "verrouiller fichier", "taguer fichier", "lien de partage", "restaurer version",
    "synchronisation", "règle d'accès", "fichier", "fichiers", "dossier", "dossiers",
)
SURFSENCE_HINTS = (
    "surfsence", "knowledge base", "base de connaissance", "documentation interne",
    "politique rh", "procédure interne", "charte", "grille tarifaire",
    "compte-rendu archivé", "documentation produit", "guide d'utilisation",
    "base interne", "recherche documentaire",
)
SURFSENCE_HINTS_FR = (
    "surfsence", "base de connaissance", "documentation interne",
    "politique rh", "procédure interne", "charte", "grille tarifaire",
    "compte-rendu archivé", "documentation produit", "guide d'utilisation",
    "base interne", "recherche documentaire", "notre base", "notre documentation",
    "notre politique", "notre procédure", "notre charte", "notre grille",
)
STALWART_HINTS = (
    "stalwart", "email", "emails", "mail", "smtp", "inbox email",
    "send email", "reply email", "forward email", "search email",
    "email folder", "email rule", "email signature", "out of office",
    "unsubscribe", "cc", "bcc", "schedule send", "delegate access",
    "email attachment", "email absense",
)
STALWART_HINTS_FR = (
    "stalwart", "email", "emails", "mail", "envoyer email", "répondre email",
    "transférer email", "chercher email", "dossier email", "règle tri",
    "pièce jointe email", "message absence", "signature", "désabonnement",
    "cc", "bcc", "programmer envoi", "déléguer accès", "boîte mail",
    "email professionnel",
)


@dataclass(frozen=True)
class RoutePrediction:
    intent: str
    predicted_labels: list[str]
    raw_response: str

    @property
    def predicted_label(self) -> str:
        return self.predicted_labels[0] if self.predicted_labels else "invalid"


@dataclass(frozen=True)
class OpenRouterConfig:
    model: str
    api_key: str
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 128
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    seed: int | None = 42
    base_url: str = "https://openrouter.ai/api/v1"
    site_url: str | None = None
    app_name: str = "activity-ai-engine"
    json_mode: bool = True


ROUTING_SYSTEM_PROMPT_FR_ZEROSHOT = (
    "Tu es le module de ROUTAGE de Viito, l'assistant intégré à Activiity Center.\n"
    "Ton unique rôle : lire la demande de l'utilisateur et déterminer QUEL(S) SYSTÈME(S)\n"
    "doivent la traiter. Tu ne réponds JAMAIS à la demande toi-même. Tu ne fais QUE classer.\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "RÈGLE DE SORTIE — À RESPECTER STRICTEMENT\n"
    "═══════════════════════════════════════════════════════════════\n"
    "Réponds UNIQUEMENT avec un objet JSON valide, sans texte avant ni après,\n"
    "sans balises markdown, sans explication :\n\n"
    "{\"classes\": [\"<classe1>\", \"<classe2>\", \"...\"]}\n\n"
    "- 1 seule classe → tableau à 1 élément.\n"
    "- Plusieurs actions dans des systèmes différents → liste TOUTES les classes\n"
    "  concernées, dans l'ORDRE où les actions doivent être exécutées.\n"
    "- Choisis toujours une classe dans la liste fermée ci-dessous. N'invente jamais\n"
    "  de nouvelle classe. En cas de doute total → \"general_question\".\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "LES 8 CLASSES (liste fermée)\n"
    "═══════════════════════════════════════════════════════════════\n\n"
    "1. odoo — CRM / ERP / gestion d'entreprise\n"
    "   Déclencheurs : prospect, opportunité, pipeline commercial, devis, commande de\n"
    "   vente, facture, paiement, commande d'achat fournisseur, déclaration, livraison, stock,\n"
    "   inventaire, ordre de fabrication, nomenclature, congés, note de frais,\n"
    "   campagne mailing (envoi en masse via le CRM).\n"
    "   Verbes typiques : créer/convertir/valider/facturer/commander/approuver/livrer.\n\n"
    "2. chatwoot — Conversations client en direct (chat, WhatsApp, Telegram, email support, réseaux, SMS)\n"
    "   Déclencheurs : conversation, ticket, ticket client, répondre à un client,\n"
    "   assigner une conversation, macro de support, boîte de réception (inbox),\n"
    "   agent, réponse standardisée (canned response), contact client (profil,\n"
    "template de mail, template de message, canal de conversation\n"
    "   historique des échanges), rapport d'agents/temps de réponse,\n"
    "   chercher un message dans une conversation/ticket, pièce jointe d'un ticket.\n"
    "    Ne concerne QUE les échanges avec un client via un canal de support\n"
    "   (chat, WhatsApp, Telegram, email entrant client). Pas l'email professionnel\n"
    "   classique (→ stalwart).\n\n"
    "3. vikunja — Gestion de tâches et de projets internes\n"
    "   Déclencheurs : tâche, projet, kanban, tableau de bord des taches, tableau de bord projets, graphique des taches\n"
     " colonne, étiquette (label), assigner\n"
    "   un collègue à une tâche, échéance, sous-tâche, relation entre tâches\n"
    "   (bloque/bloqué par), commentaire sur une tâche, vue projet,\n"
    "   téléverser un fichier/image POUR UN PROJET (image de fond, pièce jointe de tâche).\n"
    "    \"Réunion\" n'est vikunja QUE s'il s'agit d'une tâche de suivi dans un\n"
    "   projet, sans notion d'heure/agenda. Si une heure précise et des invités\n"
    "   sont mentionnés pour un événement d'agenda → nextcloud.\n"
    "    RÈGLE CLÉ : si l'ACTION principale est de créer/modifier/compléter une\n"
    "   TÂCHE ou un PROJET, c'est vikunja PEU IMPORTE le contenu de la tâche.\n"
    "   \"Marquer la tâche 'Envoyer la facture' comme terminée\" → vikunja (pas odoo).\n"
    "   \"Crée une tâche 'Relancer le client par email'\" → vikunja (pas stalwart).\n"
    "   Le mot-clé est l'ACTION sur la tâche (créer, assigner, déplacer, terminer),\n"
    "   PAS le contenu décrit dans le titre de la tâche.\n\n"
    "4. onlyoffice — Édition du CONTENU d'un document (texte, tableur, présentation, PDF, formulaire)\n"
    "   Déclencheurs : rédiger/créer un document, mettre en gras, changer la police,\n"
    "   ajouter un tableau/graphique/TCD/macro dans une feuille de calcul, créer des\n"
    "   diapositives, mettre en forme,remplire formulaire, corriger l'orthographe, traduire un texte,\n"
    "   remplir un formulaire PDF, convertir en PDF., donner l'accés(lecture/écriture) pour  un document, un tableur OU une présentation\n"
    "   AUSSI : ouvrir/supprimer/partager/sauvegarder/restaurer version/donner l'accés(lecture/écriture) pour  un document, un tableur OU une présentation\n"
    "   (même sans modifier le contenu) → onlyoffice. Un document, un tableur et une\n"
    "   présentation sont des OBJETS D'ÉDITION, pas de simples fichiers.\n"
    "    Concerne le CONTENU du document (ce qu'il y a dedans), pas un fichier\n"
    "   générique ou un dossier (→ nextcloud).\n\n"
    "5. nextcloud — Calendrier + explorateur de fichiers\n"
    "   Déclencheurs :\n"
    "   - Calendrier : créer/déplacer/supprimer un événement, inviter des\n"
    "     participants, consulter l'agenda, réunion récurrente, rendez-vous à une\n"
    "     heure précise, rappel réunion.\n"
    "   - Fichiers : téléverser, déplacer, renommer, chercher, verrouiller, taguer\n"
    "     un fichier/dossier ; créer un lien de partage ; restaurer une version ;\n"
    "     demande de dépôt de fichiers ; synchronisation ; règle d'accès.\n"
    "    Partager/déplacer/uploader un FICHIER (sans toucher son contenu) → nextcloud.\n"
    "   Modifier ce qu'il y a DANS le fichier → onlyoffice.\n\n"
    "6. surfsence — Base de connaissance interne  (recherche documentaire) et notebook intelligent\n"
    "   Déclencheurs : \"cherche dans notre base/documentation interne\", politique\n"
    "   RH interne, procédure interne, charte interne, grille tarifaire interne,\n"
    "   compte-rendu archivé, documentation produit interne, guide d'utilisation\n"
    "   interne, spécifications techniques internes, normes internes, espace, workspace, accée au espace,\n"
    " gérer l'espace, ajouter/supprimer de l'espace\n"
    "   chercher dans l'internet, ajouter vidéo youtube, ajouter page web, génére une vidéo, génére un podcast, génére une image,\n"
    " génére un visuel, infograpfie, snapshot : à partir/ accompagnant d'un document\n"
    "   RÈGLE : tout mot-clé \"interne\" + demande d'INFORMATION (que dit, où\n"
    "   se trouve, cherche, résume, quel est) → surfsence.\n"
    "    DIFFÉRENCE AVEC general_question :\n"
    "   surfsence = chercher dans la BASE DE CONNAISSANCES DE L'ENTREPRISE\n"
    "   (notre politique, notre procédure, nos sources internes, dans notre base).\n"
    "   general_question = question de culture générale, conseil métier externe,\n"
    "   résumé/écriture/création qui n'interroge PAS une base interne.\n"
    
    "   \"Quelle est la procédure d'onboarding\" = surfsence (base interne).\n"
    "   \"Quels sont les étapes clés du recrutement en général\" = general_question.\n"
    "   PAS de recherche dans des fichiers/dossiers du cloud (→ nextcloud).\n"
    "ajouter document/lien/video/image dans la base de connaissance interne → surfsence.\n"
    "   PAS d'ouverture/modification de documents (→ onlyoffice).\n"
    "    \"note interne sur un ticket\" = chatwoot. \"dossier interne\" = nextcloud.\n"
    "   \"email interne\" = stalwart. Le mot \"interne\" seul ne suffit pas.\n\n"
    "creer dossier/sous dossier dans l'espace -> onlyoffice.\n\n"
    "7. stalwart — Gestion des emails professionnels\n"
    "   Déclencheurs : envoyer/répondre/transférer un email, chercher un email,\n"
    "   dossiers/règles de tri, pièce jointe email, message d'absence, signature,\n"
    "   désabonnement (newsletter, liste de diffusion), CC/BCC, programmer l'envoi,\n"
    "   déléguer l'accès à la boîte mail.\n"
    "    Email = communication professionnelle classique (SMTP). Si le message\n"
    "   provient/part d'une conversation de support client (chat, WhatsApp) →\n"
    "   chatwoot.\n\n"
    "8. general_question — Tout ce qui n'est PAS une action sur un des 7 systèmes ci-dessus\n"
    "   Inclut : résumé, réécriture, traduction générique (hors éditeur), brainstorming,\n"
    "   écriture créative, culture générale, ET les questions de conseil/expertise\n"
    "   métier (juridique, comptable, marketing, RH, technique du métier) ou\n"
    "   personnelles (patrimoine, rénovation, sport, voyage, art, écriture...) qui\n"
    "   ne demandent AUCUNE action concrète dans un des outils.\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "RÈGLES DE DÉSAMBIGUÏSATION\n"
    "═══════════════════════════════════════════════════════════════\n"
    "Ces règles tranchent les cas limites les plus courants :\n\n"
    "• \"partager un PROJET\" / \"partager le projet\" → vikunja\n"
    "  \"partager un FICHIER\" / \"partager un document\" → nextcloud (ou onlyoffice si édition)\n\n"
    "• \"fichiers attachés à une tâche\" / \"pièces jointes d'une tâche\" → vikunja\n"
    "  \"fichiers dans le cloud\" / \"déposer des fichiers\" → nextcloud\n\n"
    "• \"ouvre le document\" / \"ouvre le tableur\" / \"ouvre la présentation\"\n"
    "  (pour voir/modifier le contenu) → onlyoffice\n"
    "\"affiche le rapport d'un client\" / \"affiche le devis\" / \"affiche la facture\"\n"
    "-> odoo(pas onlyoffice ou general_question)\n"
    "  \"ouvre le fichier\" / \"affiche le fichier\" (pour consulter un fichier\n"
    "  générique) → nextcloud\n\n"
    "• \"supprime le document\" / \"efface le document\" / \"supprime le tableur\"\n"
    "  / \"efface la présentation\" / \"supprime la feuille\" → onlyoffice\n"
    "  \"supprime le fichier\" / \"efface le fichier\" / \"supprime le dossier\"\n"
    "  → nextcloud\n\n"
    "\"resume le document\" / \"résume le compte-rendu\" / \"résume la procédure\"\n"
    "  →  general_question (pas onlyoffice)\n\n"
    "• \"partage le document\" / \"donne accès au document\" / \"partage la\n"
    "  présentation\" / \"donne accès à la présentation\" → onlyoffice\n"
    "  \"partage le dossier\" / \"donne accès au dossier\" → nextcloud\n\n"
    "• \"crée un document\" / \"crée un tableau\" / \"rédige\" → onlyoffice\n"
    "  \"crée un dossier\" / \"crée un dossier partagé\" → nextcloud\n\n"
    "• \"équipe support\" / \"équipe ticket\" → chatwoot\n"
    "  \"équipe projet\" / \"équipe projet\" → vikunja\n"
    "  \"équipe commerciale\" / \"équipe ventes\" → odoo\n\n"
    "• \"notifications\" (sans contexte d'app spécifique) → chatwoot\n"
    "  \"notifications Vikunja\" / \"notifications du projet\" → vikunja\n"
    "• \"répondre à un email\" / \"réponds à l'e-mail\" → stalwart\n"
    "  SAUF si le contexte montre clairement un échange de support client\n"
    "  (ex. : \"réponds au client via le chat\") → chatwoot\n\n"
    "• \"cherche le message de [personne]\" dans un ticket/conversation → chatwoot\n"
    "  \"cherche l'email de [personne]\" / \"retrouve l'email\" → stalwart\n\n"
    "• \"téléverse une image/fichier POUR UN PROJET\" (image de fond, pièce jointe\n"
    "  de tâche) → vikunja\n"
    "  \"téléverse un fichier dans un dossier\" → nextcloud\n\n"
    "• \"cherche dans notre base\" / \"où se trouve la procédure\" / \"que dit la\n"
    "  politique\" / \"résume le compte-rendu\" → surfsence\n"
    "  \"cherche le fichier\" / \"téléverse\" / \"déplace le dossier\" / \"partage le\n"
    "  dossier\" / \"renomme le fichier\" / \"verrouille\" / \"tag\" / \"lien de partage\"\n"
    "  / \"compare les versions\" / \"restaure la version\" → nextcloud\n"
    "  RÈGLE CLÉ : si l'utilisateur veut LIRE/CONSULTER une information écrite\n"
    "  (procédure, politique, compte-rendu, guide) → surfsence.\n"
    "  Si l'utilisateur veut MANIPULER un fichier/dossier (créer, déplacer,\n"
    "  renommer, supprimer, partager, verrouiller, comparer versions) → nextcloud.\n"
    "  Les mots \"documentation\", \"procédure\", \"archive\", \"compte-rendu\",\n"
    "  \"notes\" peuvent apparaître dans les DEUX sens : regarder le VERBE.\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "MÉTHODE (applique ces étapes mentalement, ne les écris pas dans ta réponse)\n"
    "═══════════════════════════════════════════════════════════════\n"
    "1. Repère chaque ACTION distincte demandée dans le message.\n"
    "2. Pour chaque action, trouve le système propriétaire de cette action grâce\n"
    "   aux déclencheurs ci-dessus.\n"
    "3. Si toutes les actions pointent vers le même système → une seule classe.\n"
    "4. Si les actions pointent vers des systèmes différents → liste toutes les\n"
    "   classes concernées, dans l'ordre d'exécution logique du scénario.\n"
    "5. Si le message ne demande aucune action dans un outil (juste une question,\n"
    "   un conseil, un texte à produire) → \"general_question\".\n"
    "6. En cas d'ambiguïté persistante entre deux classes proches, relis les règles\n"
    "    ci-dessus : elles tranchent tous les cas limites courants.\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "RAPPEL FINAL\n"
    "═══════════════════════════════════════════════════════════════\n"
    "Sortie = UNIQUEMENT le JSON {\"classes\": [...]}. Rien d'autre."
)

ROUTING_SYSTEM_PROMPT_FR_FEWSHOT = ROUTING_SYSTEM_PROMPT_FR_ZEROSHOT + (
    "\n\n═══════════════════════════════════════════════════════════════\n"
    "EXEMPLES (few-shot)\n"
    "═══════════════════════════════════════════════════════════════\n"
    "Q: \"Crée un devis pour l'affaire Acme Corp\"\n"
    "{\"classes\": [\"odoo\"]}\n\n"
    "Q: \"Assigne la conversation avec Ahmed à Lisa\"\n"
    "{\"classes\": [\"chatwoot\"]}\n\n"
    "Q: \"Marque la tâche 'Rapport Q3' comme terminée\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "Q: \"Ajoute une table des matières au début du document 'Cahier des charges'\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Crée une réunion 'Point hebdo' demain de 9h à 10h avec Marie et Jean\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Que dit notre politique de télétravail sur le nombre de jours autorisés ?\"\n"
    "{\"classes\": [\"surfsence\"]}\n\n"
    "Q: \"Programme l'envoi de cet email à Sophie pour vendredi 9h\"\n"
    "{\"classes\": [\"stalwart\"]}\n\n"
    "Q: \"Explique-moi la différence entre une SASU et une EURL\"\n"
    "{\"classes\": [\"general_question\"]}\n\n"
    "Q: \"Crée le projet 'Chantier Mairie Caen' et assigne-le à Rémi, génère le\n"
    "contrat client depuis notre modèle standard, et planifie une réunion de\n"
    "démarrage lundi prochain avec toute l'équipe\"\n"
    "{\"classes\": [\"vikunja\", \"onlyoffice\", \"nextcloud\"]}\n\n"
    "Q: \"Notre client TechCorp signale un bug critique. Ouvre un ticket, crée une\n"
    "tâche urgente dans le projet, et envoie-lui un email de confirmation\"\n"
    "{\"classes\": [\"chatwoot\", \"vikunja\", \"stalwart\"]}\n\n"
    "Q: \"Cherche notre grille tarifaire interne pour ce client, prépare une\n"
    "présentation avec notre offre, et envoie-la lui par email\"\n"
    "{\"classes\": [\"surfsence\", \"onlyoffice\", \"stalwart\"]}\n\n"
    "--- DÉSAMBIGUÏSATION (exemples clés) ---\n"
    "Q: \"Partage le projet 'Refonte site' avec Marie\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "Q: \"Ajoute le PDF du contrat à la tâche 'Signature'\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "Q: \"Ouvre le document 'Compte rendu Q3' pour le modifier\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Crée un document Word pour la proposition commerciale\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Marie gère l'équipe support, ajoute-la aux tickets\"\n"
    "{\"classes\": [\"chatwoot\"]}\n\n"
    "Q: \"Réponds à l'e-mail de Thomas concernant le devis\"\n"
    "{\"classes\": [\"stalwart\"]}\n\n"
    "Q: \"Marque toutes tes notifications comme lues\"\n"
    "{\"classes\": [\"chatwoot\"]}\n\n"
    "Q: \"Marquer toutes mes notifications Vikunja comme lues\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "--- DÉSAMBIGUÏSATION ONLYOFFICE vs NEXTCLOUD ---\n"
    "Q: \"Ouvre le document 'Rapport annuel 2026'\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Ouvre le tableur 'Suivi des ventes'\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Ouvre la présentation 'Pitch deck investisseurs'\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Efface définitivement le document 'Plan marketing'\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Supprime la présentation 'Offre Green Solutions'\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Partage le document 'Bilan financier' avec Marie en lecture seule\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Donne accès en modification à Camille sur le tableur 'Budget'\"\n"
    "{\"classes\": [\"onlyoffice\"]}\n\n"
    "Q: \"Crée un dossier 'Contrats' et déplace les fichiers dedans\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Téléverse le PDF dans le dossier 'Documentation technique'\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Compare les deux versions du document 'Cahier des charges'\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Téléverse une image de fond pour le projet 'Refonte site'\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "--- DÉSAMBIGUÏSATION CHATWOOT vs STALWART ---\n"
    "Q: \"Trouve le message de Thomas concernant le robinet\"\n"
    "{\"classes\": [\"chatwoot\"]}\n\n"
    "Q: \"Trouve l'email de Martin Lefèvre concernant la facture\"\n"
    "{\"classes\": [\"stalwart\"]}\n\n"
    "Q: \"Supprime-moi de la liste de diffusion marketing\"\n"
    "{\"classes\": [\"stalwart\"]}\n\n"
    "--- DÉSAMBIGUÏSATION VIKUNJA vs ODOO/STALWART ---\n"
    "Q: \"Marquer la tâche 'Envoyer la facture' comme terminée\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "Q: \"Crée une tâche 'Relancer le fournisseur par email'\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "Q: \"Crée une tâche 'Préparer le devis client'\"\n"
    "{\"classes\": [\"vikunja\"]}\n\n"
    "--- DÉSAMBIGUÏSATION SURFSENCE vs GENERAL_QUESTION ---\n"
    "Q: \"Quelle est notre procédure interne de recrutement ?\"\n"
    "{\"classes\": [\"surfsence\"]}\n\n"
    "Q: \"Quels sont les étapes clés du recrutement en général ?\"\n"
    "{\"classes\": [\"general_question\"]}\n\n"
    "Q: \"Résume notre politique de congés\"\n"
    "{\"classes\": [\"surfsence\"]}\n\n"
    "Q: \"Quel est le cadre légal des congés payés en France ?\"\n"
    "{\"classes\": [\"general_question\"]}\n\n"
    "--- DÉSAMBIGUÏSATION SURFSENCE vs NEXTCLOUD ---\n"
    "Q: \"Que dit notre politique de télétravail ?\"\n"
    "{\"classes\": [\"surfsence\"]}\n\n"
    "Q: \"Où se trouve la procédure d'onboarding ?\"\n"
    "{\"classes\": [\"surfsence\"]}\n\n"
    "Q: \"Résume le compte-rendu de la réunion avec Client A\"\n"
    "{\"classes\": [\"surfsence\"]}\n\n"
    "Q: \"Téléverse le fichier dans le dossier 'Documentation technique'\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Cherche le fichier 'Procédure onboarding' dans mes dossiers\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Partage le dossier 'Ressources RH' avec Marie\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Compare les versions du fichier 'Procédure onboarding'\"\n"
    "{\"classes\": [\"nextcloud\"]}\n\n"
    "Q: \"Restaure la version précédente du document 'Contrat'\"\n"
    "{\"classes\": [\"nextcloud\"]}"
)


ROUTING_SYSTEM_PROMPTS = {
    "zeroshot": ROUTING_SYSTEM_PROMPT_FR_ZEROSHOT,
    "fewshot": ROUTING_SYSTEM_PROMPT_FR_FEWSHOT,
}


class OpenRouterCompletionClient:
    def __init__(self, config: OpenRouterConfig, system_prompt: str | None = None) -> None:
        self.config = config
        self.system_prompt = system_prompt or ROUTING_SYSTEM_PROMPT_FR_FEWSHOT

    def complete(self, prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "frequency_penalty": self.config.frequency_penalty,
            "presence_penalty": self.config.presence_penalty,
        }
        if self.config.seed is not None:
            payload["seed"] = self.config.seed
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.config.site_url or "http://localhost",
            "X-Title": self.config.app_name,
        }
        last_error = None
        max_attempts = 10
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    f"{self.config.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60,
                )
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 15))
                    if attempt < max_attempts:
                        time.sleep(retry_after)
                        continue
                response.raise_for_status()
                content = response.json()["choices"][0]["message"].get("content")
                if content is None:
                    if attempt < max_attempts:
                        time.sleep(2)
                        continue
                    return ""
                raw = str(content).strip()
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                return raw
            except (requests.RequestException, KeyError, ValueError) as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(min(2 ** attempt, 60))
                    continue
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 401:
                    raise RuntimeError(
                        "OpenRouter rejected the API key loaded from .env. "
                        "Check that OPENROUTER_API_KEY is present, valid, and not expired."
                    ) from exc
                raise RuntimeError(
                    f"OpenRouter request failed after {attempt} attempts: {last_error}"
                ) from last_error


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def normalize_label(value: str | None) -> str | None:
    if value is None:
        return None

    token = _normalize_token(str(value))
    if not token:
        return None

    if token in ROUTING_LABELS:
        return token

    return LABEL_ALIASES.get(token)


def normalize_labels(value: str | None) -> list[str]:
    if value is None:
        return []
    raw = str(value).strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[,;]+", raw) if p.strip()]
    result: list[str] = []
    for part in parts:
        normalized = normalize_label(part)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    return result


def _extract_text_from_json(raw_response: str) -> str | None:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, str):
        return parsed

    if isinstance(parsed, dict):
        for key in ("label", "prediction", "route", "class", "intent_class", "category"):
            value = parsed.get(key)
            if value is not None:
                return str(value)

    return None


def _extract_labels_from_json(raw_response: str) -> list[str] | None:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict):
        classes = parsed.get("classes")
        if isinstance(classes, list):
            labels: list[str] = []
            for item in classes:
                normalized = normalize_label(str(item))
                if normalized is not None and normalized not in labels:
                    labels.append(normalized)
            return labels if labels else None

        label = parsed.get("label")
        if label is not None:
            normalized = normalize_label(str(label))
            return [normalized] if normalized else None

    return None


def extract_labels_from_response(raw_response: str) -> list[str]:
    labels_from_json = _extract_labels_from_json(raw_response)
    if labels_from_json is not None:
        return labels_from_json

    text_from_json = _extract_text_from_json(raw_response)
    if text_from_json is not None:
        labels_from_csv = normalize_labels(text_from_json)
        if labels_from_csv:
            return labels_from_csv

    labels_from_raw = normalize_labels(raw_response)
    if labels_from_raw:
        return labels_from_raw

    lowered = raw_response.lower()
    ordered_patterns = (
        (r"\bgeneral[_\s-]?question\b", "general_question"),
        (r"\bchatwoot\b", "chatwoot"),
        (r"\bvikunja\b", "vikunja"),
        (r"\bonlyoffice\b", "onlyoffice"),
        (r"\bnextcloud\b", "nextcloud"),
        (r"\bsurfsence\b", "surfsence"),
        (r"\bstalwart\b", "stalwart"),
        (r"\bodoo\b", "odoo"),
        (r"\bcrm\b", "odoo"),
        (r"\berp\b", "odoo"),
    )
    found: list[str] = []
    for pattern, label in ordered_patterns:
        if re.search(pattern, lowered) and label not in found:
            found.append(label)
    return found if found else ["invalid"]


def extract_label_from_response(raw_response: str) -> str:
    labels = extract_labels_from_response(raw_response)
    return labels[0] if labels else "invalid"


def load_prompt_template(prompt_path: Path | str | None = None) -> str:
    path = Path(prompt_path) if prompt_path is not None else DEFAULT_PROMPT_PATH
    if not path.exists():
        raise FileNotFoundError(f"Router prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def render_router_prompt(intent: str, prompt_template: str) -> str:
    rendered = prompt_template.replace("__LABELS__", ", ".join(ROUTING_LABELS))
    return rendered.replace("__INTENT__", intent)


def build_openrouter_client(
    *,
    model: str,
    api_key: str | None = None,
    temperature: float = 0.0,
    top_p: float = 1.0,
    max_tokens: int = 128,
    frequency_penalty: float = 0.0,
    presence_penalty: float = 0.0,
    seed: int | None = 42,
    base_url: str = "https://openrouter.ai/api/v1",
    site_url: str | None = None,
    app_name: str = "activity-ai-engine",
    json_mode: bool = True,
    prompt_version: str = "fewshot",
) -> OpenRouterCompletionClient:
    resolved_api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not resolved_api_key:
        raise ValueError("OPENROUTER_API_KEY is required to use an OpenRouter client")

    system_prompt = ROUTING_SYSTEM_PROMPTS.get(prompt_version, ROUTING_SYSTEM_PROMPT_FR_FEWSHOT)

    return OpenRouterCompletionClient(
        OpenRouterConfig(
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
        system_prompt=system_prompt,
    )


class RuleBasedRouterClient:
    def complete(self, prompt: str) -> str:
        match = re.search(r"(?:Intent|Input):\s*(.+)", prompt, flags=re.IGNORECASE | re.DOTALL)
        intent = match.group(1).strip() if match else prompt
        lowered = intent.lower()

        found: list[str] = []
        if any(kw in lowered for kw in ODOO_HINTS_EN + ODOO_HINTS_FR):
            found.append("odoo")
        if any(kw in lowered for kw in CHATWOOT_HINTS + CHATWOOT_HINTS_FR):
            found.append("chatwoot")
        if any(kw in lowered for kw in VIKUNJA_HINTS + VIKUNJA_HINTS_FR):
            found.append("vikunja")
        if any(kw in lowered for kw in ONLYOFFICE_HINTS + ONLYOFFICE_HINTS_FR):
            found.append("onlyoffice")
        if any(kw in lowered for kw in NEXTCLOUD_HINTS + NEXTCLOUD_HINTS_FR):
            found.append("nextcloud")
        if any(kw in lowered for kw in SURFSENCE_HINTS + SURFSENCE_HINTS_FR):
            found.append("surfsence")
        if any(kw in lowered for kw in STALWART_HINTS + STALWART_HINTS_FR):
            found.append("stalwart")

        if not found:
            found.append("general_question")

        return json.dumps({"classes": found})


def coerce_completion_client(model_client: Any | None) -> Callable[[str], str]:
    if model_client is None:
        return RuleBasedRouterClient().complete

    if callable(model_client):
        return model_client

    for method_name in ("complete", "generate", "predict", "invoke"):
        method = getattr(model_client, method_name, None)
        if callable(method):
            return method

    raise TypeError(
        "Unsupported model client. Pass a callable or an object exposing complete(), generate(), predict(), or invoke()."
    )


class RouterAgent:
    def __init__(self, model_client: Any | None = None, prompt_path: Path | str | None = None) -> None:
        self._completion = coerce_completion_client(model_client)
        self.prompt_path = Path(prompt_path) if prompt_path is not None else DEFAULT_PROMPT_PATH
        self.prompt_template = load_prompt_template(self.prompt_path)

    def build_prompt(self, intent: str) -> str:
        return render_router_prompt(intent=intent, prompt_template=self.prompt_template)

    def predict(self, intent: str) -> RoutePrediction:
        prompt = self.build_prompt(intent)
        raw_response = str(self._completion(prompt)).strip()
        predicted_labels = extract_labels_from_response(raw_response)

        if predicted_labels == ["invalid"] or not predicted_labels:
            time.sleep(2)
            raw_response = str(self._completion(prompt)).strip()
            predicted_labels = extract_labels_from_response(raw_response)

        if predicted_labels == ["invalid"] or not predicted_labels:
            predicted_labels = ["general_question"]

        return RoutePrediction(intent=intent, predicted_labels=predicted_labels, raw_response=raw_response)


def route_intent(intent: str, model_client: Any | None = None, prompt_path: Path | str | None = None) -> RoutePrediction:
    return RouterAgent(model_client=model_client, prompt_path=prompt_path).predict(intent)
