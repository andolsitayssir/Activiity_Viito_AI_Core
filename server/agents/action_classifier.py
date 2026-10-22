from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from server.config import load_environment
from server.agents.domain_classifier2 import (
    DOMAIN_LABELS,
    DomainClassifierAgent,
    DomainClassifierClient,
)

load_environment()

ACTION_IDS_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    'accounting': ('account.bank.statement.line.action_undo_reconciliation', 'account.journal.action_create_new', 'account.journal.action_create_vendor_bill', 'account.journal.button_fetch_in_einvoices', 'account.journal.create_bank_statement', 'account.journal.create_customer_payment', 'account.journal.create_supplier_payment', 'account.move.action_duplicate', 'account.move.action_force_register_payment', 'account.move.action_get', 'account.move.action_invoice_download_pdf', 'account.move.action_post', 'account.move.action_print_pdf', 'account.move.action_register_payment', 'account.move.action_reverse', 'account.move.action_switch_move_type', 'account.move.action_toggle_block_payment', 'account.move.button_cancel', 'account.move.button_draft', 'account.move.button_hash', 'account.move.button_request_cancel', 'account.move.check_move_sequence_chain', 'account.move.create', 'account.move.get_currency_rate', 'account.move.js_assign_outstanding_line', 'account.move.open_payments', 'account.move.open_reconcile_view', 'account.move.reversal.reverse_moves', 'account.move.search_read', 'account.move.unlink', 'account.move.write', 'account.payment.action_cancel', 'account.payment.action_draft', 'account.payment.action_post', 'account.payment.action_reject', 'account.payment.action_validate', 'account.payment.mark_as_sent', 'account.payment.register.create_payments', 'account.tax.compute_all', 'res.partner.search_read',),
    'calendaring': ('calendar.create', 'calendar.list', 'event.create', 'event.delete', 'event.free_busy', 'event.modify', 'event.update', 'event.multiget', 'event.query', 'event.recurrence', 'event.reminder', 'event.sync',),
    'contacts': ('res.company.action_all_company_branches', 'res.partner.address_get', 'res.partner.copy', 'res.partner.create', 'res.partner.create_company', 'res.partner.find_or_create', 'res.partner.open_commercial_entity', 'res.partner.read_group', 'res.partner.search_read', 'res.partner.unlink', 'res.partner.write',),
    'crm': ('crm.lead.action_archive', 'crm.lead.action_reschedule_meeting', 'crm.lead.action_restore', 'crm.lead.action_schedule_meeting', 'crm.lead.action_set_lost', 'crm.lead.action_set_won', 'crm.lead.action_show_potential_duplicates', 'crm.lead.action_unarchive', 'crm.lead.convert_opportunity', 'crm.lead.create', 'crm.lead.log_meeting', 'crm.lead.lost.action_lost_reason_apply', 'crm.lead.merge_opportunity', 'crm.lead.pls.update.action_update_crm_lead_probabilities', 'crm.lead.search_read', 'crm.lead.unlink', 'crm.lead.write', 'crm.lead2opportunity.partner.action_apply', 'crm.lead2opportunity.partner.mass.action_mass_convert', 'crm.merge.opportunity.action_merge', 'crm.team.action_assign_leads', 'crm.team.action_opportunity_forecast', 'crm.team.action_your_pipeline',),
    'email_action': ('email.blob.copy', 'email.blob.get', 'email.blob.lookup', 'email.blob.upload', 'email.email.copy', 'email.email.get', 'email.email.import', 'email.email.parse', 'email.email.query', 'email.email.query_changes', 'email.email.reply', 'email.email.search_snippet', 'email.email.set', 'email.email_submission.query', 'email.email_submission.set', 'email.identity.get', 'email.identity.set', 'email.mailbox.create', 'email.mailbox.delete', 'email.mailbox.get', 'email.mailbox.query', 'email.mailbox.rename', 'email.mailbox.set', 'email.push_subscription.get', 'email.push_subscription.set', 'email.quota.get', 'email.sieve_script.query', 'email.sieve_script.set', 'email.sieve_script.validate', 'email.smtp.data', 'email.smtp.mail_from', 'email.smtp.rcpt_to', 'email.thread.get', 'email.thread.get_changes', 'email.vacation_response.set',),
    'file_management': ('file.copy', 'file.delete', 'file.direct_editing', 'file.get', 'file.mkcol', 'file.move', 'file.propfind', 'file.proppatch', 'file.put', 'file.share.create', 'file.share.delete', 'file.share.transfer', 'file.share.update', 'file.trashbin.list', 'file.trashbin.purge', 'file.trashbin.restore', 'file.version.list', 'file.version.restore',),
    'general': ('conseil personnel', 'conseil professionnel - rh', 'conseil professionnel - accounting', 'conseil professionnel - administration', 'conseil professionnel - artisanat', 'conseil professionnel - automobile', 'conseil professionnel - finance', 'conseil professionnel - fiscalité', 'conseil professionnel - juridique', 'conseil professionnel - management', 'conseil professionnel - marketing', 'conseil professionnel - édition', 'general', 'redaction', 'resume', 'traduction',),
    'hr': ('hr.applicant.action_create_meeting', 'hr.applicant.action_send_email', 'hr.applicant.archive_applicant', 'hr.applicant.create_employee_from_applicant', 'hr.attendance.action_approve_overtime', 'hr.attendance.action_refuse_overtime', 'hr.attendance.get_kiosk_url', 'hr.department.get_department_hierarchy', 'hr.employee.action_archive', 'hr.employee.action_create_user', 'hr.employee.action_create_users', 'hr.employee.action_related_contacts', 'hr.employee.action_unarchive', 'hr.employee.create', 'hr.employee.generate_random_barcode', 'hr.employee.search_read', 'hr.employee.unlink', 'hr.employee.write', 'hr.expense.action_approve', 'hr.expense.action_pay', 'hr.expense.action_post', 'hr.expense.action_refuse', 'hr.expense.action_reset', 'hr.expense.action_split_wizard', 'hr.expense.action_submit', 'hr.expense.create_expense_from_attachments', 'hr.leave.action_approve', 'hr.leave.action_back_to_approval', 'hr.leave.action_cancel', 'hr.leave.action_refuse', 'hr.leave.allocation.action_approve', 'hr.leave.allocation.action_refuse',),
    'knowledge_kb': ('kb.answer.grounded', 'kb.export.pdf', 'kb.generate.image', 'kb.generate.podcast', 'kb.generate.report', 'kb.generate.video', 'kb.ingest.document', 'kb.ingest.url', 'kb.ingest.youtube', 'kb.note.create', 'kb.search.fulltext', 'kb.search.vector', 'kb.space.create', 'kb.space.delete', 'kb.space.read', 'kb.space.update', 'kb.status.batch', 'kb.status.processing',),
    'messaging': ('messaging.automation_rule.clone', 'messaging.automation_rule.create', 'messaging.automation_rule.delete', 'messaging.automation_rule.update', 'messaging.campaign.create', 'messaging.campaign.delete', 'messaging.campaign.update', 'messaging.canned_response.create', 'messaging.canned_response.delete', 'messaging.canned_response.update', 'messaging.channel.create', 'messaging.conversation.assign', 'messaging.conversation.create', 'messaging.conversation.custom_attributes', 'messaging.conversation.delete', 'messaging.conversation.labels', 'messaging.conversation.mute', 'messaging.conversation.participants', 'messaging.conversation.read', 'messaging.conversation.toggle_priority', 'messaging.conversation.toggle_status', 'messaging.conversation.toggle_typing', 'messaging.conversation.transcript', 'messaging.conversation.unmute', 'messaging.conversation.update', 'messaging.group.create', 'messaging.inbox.create', 'messaging.inbox.delete', 'messaging.inbox.register_webhook', 'messaging.inbox.update', 'messaging.macro.create', 'messaging.macro.delete', 'messaging.macro.execute', 'messaging.macro.update', 'messaging.message.delete', 'messaging.message.retry', 'messaging.message.send', 'messaging.message.update', 'messaging.notification.list', 'messaging.notification.mark_read', 'messaging.notification.snooze',),
    'office': ('office.callback.error', 'office.callback.force_save', 'office.callback.save_ready', 'office.command.drop', 'office.command.forcesave', 'office.command.info', 'office.command.meta', 'office.command.version', 'office.converter.convert', 'office.docbuilder.generate', 'office.editor.add_title', 'office.editor.create', 'office.editor.deny_editing', 'office.editor.download_as','office.editor.add_content', 'office.editor.edit_content', 'office.editor.insert_image', 'office.editor.insert_table', 'office.editor.remove_image','office.editor.remove_table','office.editor.modify_table', 'office.editor.mail_merge', 'office.editor.refresh_file', 'office.editor.refresh_history', 'office.editor.start_filling', 'office.wopi.check_file_info', 'office.wopi.enumerate_children', 'office.wopi.get_file', 'office.wopi.lock', 'office.wopi.put_file', 'office.wopi.put_relative', 'office.wopi.refresh_lock', 'office.wopi.rename', 'office.wopi.unlock',),
    'sales': ('sale.advance.payment.inv.create_invoices', 'sale.order.action_cancel', 'sale.order.action_confirm', 'sale.order.action_draft', 'sale.order.action_lock', 'sale.order.action_open_discount_wizard', 'sale.order.action_preview_sale_order', 'sale.order.action_quotation_send', 'sale.order.action_quotation_sent', 'sale.order.action_unlock', 'sale.order.action_update_prices', 'sale.order.action_update_taxes', 'sale.order.action_view_invoice', 'sale.order.create', 'sale.order.create_document_from_attachment', 'sale.order.get_portal_last_transaction', 'sale.order.line.action_add_from_catalog', 'sale.order.payment_action_capture', 'sale.order.payment_action_void', 'sale.order.search_read', 'sale.order.unlink', 'sale.order.write',),
    'tasking': ('tasking.assignee.add', 'tasking.assignee.bulk', 'tasking.assignee.remove', 'tasking.attachment.delete', 'tasking.attachment.download', 'tasking.attachment.upload', 'tasking.bucket.create', 'tasking.bucket.delete', 'tasking.bucket.move_task', 'tasking.bucket.update', 'tasking.comment.create', 'tasking.comment.delete', 'tasking.comment.update', 'tasking.label.add', 'tasking.label.bulk', 'tasking.label.remove', 'tasking.notification.list', 'tasking.notification.mark_read', 'tasking.project.create', 'tasking.project.delete', 'tasking.project.duplicate', 'tasking.project.read', 'tasking.project.update', 'tasking.relation.create', 'tasking.relation.delete', 'tasking.share.link', 'tasking.share.project', 'tasking.task.create', 'tasking.task.delete', 'tasking.task.duplicate', 'tasking.task.partial_update', 'tasking.task.update', 'tasking.view.create', 'tasking.view.delete', 'tasking.view.update',),
}

ACTION_LABELS: tuple[str, ...] = tuple(
    sorted({action_id for action_ids in ACTION_IDS_BY_DOMAIN.values() for action_id in action_ids})
)


def catalog_for_domains(domains: list[str]) -> list[str]:
    catalog: list[str] = []
    for domain in domains:
        for action_id in ACTION_IDS_BY_DOMAIN.get(domain, ()):
            if action_id not in catalog:
                catalog.append(action_id)
    return catalog


def _repair_mojibake(value: str) -> str:
    try:
        return value.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _normalize_action_token(value: str) -> str:
    repaired = _repair_mojibake(value)
    return re.sub(r"\s+", " ", repaired.strip().lower())


def normalize_action(value: str | None) -> str | None:
    if value is None:
        return None
    token = _normalize_action_token(str(value))
    return token or None


def normalize_actions(value: str | None) -> list[str]:
    if value is None:
        return []
    raw = str(value).strip()
    if not raw:
        return []
    parts = [part.strip() for part in re.split(r"[,;]+", raw) if part.strip()]
    result: list[str] = []
    for part in parts:
        normalized = normalize_action(part)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    return result


def _extract_actions_from_json(raw_response: str) -> list[str] | None:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, str):
        actions = normalize_actions(parsed)
        return actions or None

    if isinstance(parsed, dict):
        actions_value = parsed.get("actions")
        if actions_value is not None:
            if isinstance(actions_value, list):
                result: list[str] = []
                for item in actions_value:
                    normalized = normalize_action(str(item))
                    if normalized is not None and normalized not in result:
                        result.append(normalized)
                return result or None
            actions = normalize_actions(str(actions_value))
            if actions:
                return actions

        for key in ("action", "prediction", "labels", "label", "result"):
            value = parsed.get(key)
            if value is not None:
                if isinstance(value, list):
                    result = []
                    for item in value:
                        normalized = normalize_action(str(item))
                        if normalized is not None and normalized not in result:
                            result.append(normalized)
                    return result or None
                normalized = normalize_action(str(value))
                return [normalized] if normalized else None

    return None


def extract_actions_from_response(raw_response: str) -> list[str]:
    actions_from_json = _extract_actions_from_json(raw_response)
    if actions_from_json is not None:
        return actions_from_json

    actions_from_csv = normalize_actions(raw_response)
    if actions_from_csv:
        return actions_from_csv

    lowered = raw_response.lower()
    dotted = re.findall(r"\b[a-z][a-z0-9]*(?:\.[a-z0-9_]+)+\b", lowered)
    result: list[str] = []
    for action_id in dotted:
        normalized = normalize_action(action_id)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    return result


@dataclass(frozen=True)
class ActionPrediction:
    intent: str
    predicted_actions: list[str]
    domains: list[str]
    raw_response: str

    @property
    def predicted_action(self) -> str:
        return self.predicted_actions[0] if self.predicted_actions else "invalid"


@dataclass(frozen=True)
class ActionClassifierConfig:
    model: str
    api_key: str
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 256
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    seed: int | None = 42
    base_url: str = "https://openrouter.ai/api/v1"
    site_url: str | None = None
    app_name: str = "activity-ai-engine"
    json_mode: bool = True


_DOMAIN_TOOLS = {
    "accounting": "Odoo — Facturation et comptabilité",
    "calendaring": "Nextcloud Calendar / CalDAV — Agenda et événements",
    "contacts": "Odoo — Carnet d'adresses / partenaires",
    "crm": "Odoo CRM — Pipeline commercial",
    "email_action": "Stalwart — Emails professionnels",
    "file_management": "Nextcloud — Fichiers et dossiers",
    "general": "Réponse libre (conseil, rédaction, résumé, traduction)",
    "hr": "Odoo HR — Ressources humaines",
    "knowledge_kb": "SurfSense — Base de connaissance (RAG)",
    "messaging": "Telegram — Messagerie (compte personnel)",
    "office": "OnlyOffice — Édition de documents bureautiques",
    "sales": "Odoo Sale — Devis et commandes",
    "tasking": "Vikunja — Tâches et projets",
}

_ACCOUNTING_CATALOG = """\
- account.bank.statement.line.action_undo_reconciliation : annuler le rapprochement d'une ligne de relevé bancaire.
- account.journal.action_create_new : créer un nouveau journal.
- account.journal.action_create_vendor_bill : créer une facture fournisseur.
- account.journal.button_fetch_in_einvoices : récupérer/importer les e-factures.
- account.journal.create_bank_statement : créer un relevé bancaire.
- account.journal.create_customer_payment : encaisser un paiement client.
- account.journal.create_supplier_payment : régler un fournisseur.
- account.move.action_duplicate : dupliquer une facture ou écriture.
- account.move.action_force_register_payment : forcer l'enregistrement du paiement d'une facture.
- account.move.action_get : accéder/transmettre la facture.
- account.move.action_invoice_download_pdf : télécharger la facture en PDF.
- account.move.action_post : valider/comptabiliser la facture ou l'écriture.
- account.move.action_print_pdf : imprimer la facture.
- account.move.action_register_payment : enregistrer le paiement/règlement de la facture.
- account.move.action_reverse : reverser la facture (créer un avoir).
- account.move.action_switch_move_type : basculer le type d'écriture (facture ↔ avoir).
- account.move.action_toggle_block_payment : bloquer/débloquer le paiement de la facture.
- account.move.button_cancel : annuler la facture.
- account.move.button_draft : repasser la facture en brouillon.
- account.move.button_hash : figer (hacher) l'écriture.
- account.move.button_request_cancel : demander l'annulation de la facture.
- account.move.check_move_sequence_chain : vérifier la séquence de numérotation des écritures.
- account.move.create : créer une écriture comptable / facture.
- account.move.get_currency_rate : obtenir le taux de change.
- account.move.js_assign_outstanding_line : affecter/assigne/rapproche une ligne  sur la facture.
- account.move.open_payments : ouvrir les paiements liés à la facture.
- account.move.open_reconcile_view : ouvrir/lancer le rapprochement bancaire.
- account.move.reversal.reverse_moves : inverser des écritures.
- account.move.search_read : rechercher/lister des écritures ou factures.
- account.move.unlink : supprimer une écriture.
- account.move.write : modifier une écriture.
- account.payment.action_cancel : annuler/clôturer un paiement ou règlement.
- account.payment.action_draft : repasser un paiement en brouillon.
- account.payment.action_post : valider un paiement.
- account.payment.action_reject : rejeter un paiement.
- account.payment.action_validate : valider un paiement.
- account.payment.mark_as_sent : marquer le paiement comme envoyé.
- account.payment.register.create_payments : créer les paiements via l'assistant d'enregistrement.
- account.tax.compute_all : calculer les taxes (TVA).
- res.partner.search_read : rechercher un partenaire (délégation).

Disambiguation : « paiement client » = create_customer_payment, « paiement fournisseur » =
create_supplier_payment, « enregistre le paiement » sur une facture = action_register_payment.
« Valider » une facture = action_post. « Récupère/télécharge le PDF » =
action_invoice_download_pdf, « imprime » = action_print_pdf. « Envoie la facture au client »
(transmettre la facture, objet = la facture) = action_get. « Envoie-la / envoie-le au client /
envoie-leur par email » (l'ENVOI EMAIL, pronom objet) = email.email_submission.set. Quand la
phrase combine valider/enregistrer + envoyer par email, action_get n'est PAS ajouté. « Enregistre
le remboursement dans la comptabilité » = account.payment.register.create_payments."""

_CRM_CATALOG = """\
- crm.lead.action_archive : archiver un lead/opportunité.
- crm.lead.action_reschedule_meeting : reprogrammer un appel/réunion lié au lead.
- crm.lead.action_restore : restaurer un lead archivé.
- crm.lead.action_schedule_meeting : planifier un appel/réunion avec le lead.
- crm.lead.action_set_lost : marquer l'opportunité comme perdue.
- crm.lead.action_set_won : marquer l'opportunité comme gagnée.
- crm.lead.action_show_potential_duplicates : afficher les doublons potentiels.
- crm.lead.action_unarchive : désarchiver un lead.
- crm.lead.convert_opportunity : convertir/transformer un lead en opportunité (l'action de
  conversion elle-même : « Convertit le lead X en opportunité », « Transforme le prospect X »).
- crm.lead.create : créer un lead/opportunité.
- crm.lead.log_meeting : journaliser un échange/réunion sur le lead.
- crm.lead.lost.action_lost_reason_apply : appliquer un motif de perte.
- crm.lead.merge_opportunity : fusionner DEUX leads/opportunités NOMMÉS en une seule (« fusionne les
  deux opportunités X en une seule », « regroupe les leads en double de X »).
- crm.lead.pls.update.action_update_crm_lead_probabilities : mettre à jour les probabilités.
- crm.lead.search_read : rechercher/lister des leads.
- crm.lead.unlink : supprimer un lead.
- crm.lead.write : modifier un lead/opportunité.

- crm.lead2opportunity.partner.action_apply : appliquer/valider la conversion d'un lead PRÉCIS en
  opportunité (bouton « Appliquer/Valider la conversion » du wizard lead2opportunity : « Applique
  la conversion du lead X en opportunité », « Valide le passage de X en opportunité »).
  Seulement quand la phrase dit APPLIQUER/VALIDER une conversion, JAMAIS « convertit/transforme ».
- crm.lead2opportunity.partner.mass.action_mass_convert : conversion en masse de leads.
- crm.merge.opportunity.action_merge : fusion de MASSE d'opportunités sélectionnées/doublons via
  l'assistant (« fusionne les opportunités selectionnees de X », « combine les doublons
  d'opportunités de X »).
- crm.team.action_assign_leads : assigner des leads à un membre d'équipe.
- crm.team.action_opportunity_forecast : ouvrir les prévisions d'opportunités.
- crm.team.action_your_pipeline : ouvrir le pipeline de l'équipe.

Disambiguation : « Convertit le lead X en opportunité » = convert_opportunity, « Applique/Valide
la conversion du lead X » = action_apply. Fusionner DEUX fiches nommées = merge_opportunity ;
fusion en MASSE de sélectionnées/doublons = action_merge. « Bloque un créneau / planifie un
rendez-vous / un appel avec le lead » = action_schedule_meeting ; mais « bloque un créneau » SANS
lien avec un appel avec le lead (créneau calendrier pour un rendez-vous) = calendaring
(event.create), pas action_schedule_meeting.
"""

_SALES_CATALOG = """\
- sale.advance.payment.inv.create_invoices : créer une facture d'acompte.
- sale.order.action_cancel : annuler une commande/devis.
- sale.order.action_confirm : confirmer la commande.
- sale.order.action_draft : repasser la commande en brouillon.
- sale.order.action_lock : verrouiller la commande.
- sale.order.action_open_discount_wizard : ouvrir l'assistant de remise.
- sale.order.action_preview_sale_order : prévisualiser la commande.
- sale.order.action_quotation_send : envoyer le devis au client.
- sale.order.action_quotation_sent : marquer le devis envoyé.
- sale.order.action_unlock : déverrouiller la commande.
- sale.order.action_update_prices : mettre à jour les prix.
- sale.order.action_update_taxes : mettre à jour les taxes.
- sale.order.action_view_invoice : voir la facture liée à la commande.
- sale.order.create : créer un devis/commande.
- sale.order.create_document_from_attachment : créer une commande depuis une pièce jointe.
- sale.order.get_portal_last_transaction : obtenir la dernière transaction portail.
- sale.order.line.action_add_from_catalog : ajouter une ligne produit depuis le catalogue.
- sale.order.payment_action_capture : capturer le paiement.
- sale.order.payment_action_void : annuler le paiement.
- sale.order.search_read : rechercher/lister des commandes.
- sale.order.unlink : supprimer une commande.
- sale.order.write : modifier une commande.

Disambiguation : devis/commande = sales. Facture/paiement = accounting. Confirmer un devis =
action_confirm. Envoyer un devis via Odoo = action_quotation_send (sales seul)."""

_HR_CATALOG = """\
- hr.applicant.action_create_meeting : planifier une réunion avec un candidat.
- hr.applicant.action_send_email : envoyer un email au candidat.
- hr.applicant.archive_applicant : archiver/clôturer le dossier d'un candidat.
- hr.applicant.create_employee_from_applicant : embaucher un candidat (créer l'employé DEPUIS une
  candidature).
- hr.attendance.action_approve_overtime : approuver des heures supplémentaires.
- hr.attendance.action_refuse_overtime : refuser des heures supplémentaires.
- hr.attendance.get_kiosk_url : obtenir l'URL du kiosque de pointage.
- hr.department.get_department_hierarchy : obtenir la hiérarchie des départements.
- hr.employee.action_archive : archiver un employé.
- hr.employee.action_create_user : créer un compte utilisateur pour un employé.
- hr.employee.action_create_users : créer des comptes utilisateurs.
- hr.employee.action_related_contacts : ouvrir les contacts liés.
- hr.employee.action_unarchive : désarchiver un employé.
- hr.employee.create : créer une fiche employé (nouvel employé, sans candidature).
- hr.employee.generate_random_barcode : générer un badge (code-barres).
- hr.employee.search_read : rechercher/lister des employés.
- hr.employee.unlink : supprimer un employé.
- hr.employee.write : modifier une fiche employé.
- hr.expense.action_approve : approuver une note de frais.
- hr.expense.action_pay : payer une note de frais.
- hr.expense.action_post : comptabiliser une note de frais.
- hr.expense.action_refuse : refuser une note de frais.
- hr.expense.action_reset : réinitialiser une note de frais.
- hr.expense.action_split_wizard : répartir une note de frais.
- hr.expense.action_submit : soumettre une note de frais.
- hr.expense.create_expense_from_attachments : créer une note de frais depuis des pièces.
- hr.leave.action_approve : approuver un congé.
- hr.leave.action_back_to_approval : renvoyer le congé en approbation.
- hr.leave.action_cancel : annuler un congé.
- hr.leave.action_refuse : refuser un congé.
- hr.leave.allocation.action_approve : approuver une allocation de congés.
- hr.leave.allocation.action_refuse : refuser une allocation de congés.

Disambiguation : relancer un candidat par email = hr (pas email_action). Consulter le solde de
congés / statut de note de frais = hr. Absence ajoutée à l'agenda = hr + calendaring.
« Embauche le candidat / crée l'employé depuis sa candidature » =
hr.applicant.create_employee_from_applicant. « Crée une fiche employé » (nouvel employé) =
hr.employee.create."""

_CONTACTS_CATALOG = """\

- res.company.action_all_company_branches : ouvrir toutes les sociétés/branches/entités.
- res.partner.address_get : récupérer l'adresse d'un partenaire.
- res.partner.copy : dupliquer une fiche contact.
- res.partner.create : créer une fiche contact/partenaire (personne ou société déjà existante).
- res.partner.create_company : créer une NOUVELLE société/entreprise.
- res.partner.find_or_create : chercher un contact et le créer si besoin.
- res.partner.open_commercial_entity : ouvrir l'entité commerciale.
- res.partner.read_group : lire des groupes de partenaires.
- res.partner.search_read : rechercher/lister des contacts.
- res.partner.unlink : supprimer une fiche contact.
- res.partner.write : modifier une fiche contact.

Disambiguation : fiche contact/société/partenaire = contacts. Lead/opportunité = crm.
Devis/commande = sales. Fusionner deux fiches = res.partner.find_or_create. Créer une SOCIÉTÉ
(nouvelle entreprise) = res.partner.create_company ; créer une FICHE contact = res.partner.create.
« Affiche l'adresse (facturation/livraison) » = res.partner.address_get (pas search_read)."""

_TASKING_CATALOG = """\

- tasking.assignee.add : assigner un·e membre à une tâche.
- tasking.assignee.bulk : assigner en masse.
- tasking.assignee.remove : retirer l'assignation.
- tasking.attachment.delete : supprimer une pièce jointe de tâche.
- tasking.attachment.download : télécharger une pièce jointe.
- tasking.attachment.upload : joindre un fichier à une tâche.
- tasking.bucket.create : créer une colonne/kanban.
- tasking.bucket.delete : supprimer une colonne.
- tasking.bucket.move_task : déplacer une tâche vers une autre colonne.
- tasking.bucket.update : modifier une colonne.
- tasking.comment.create : ajouter un commentaire à une tâche.
- tasking.comment.delete : supprimer un commentaire.
- tasking.comment.update : modifier un commentaire.
- tasking.label.add : ajouter un label à une tâche.
- tasking.label.bulk : appliquer des labels en masse.
- tasking.label.remove : retirer un label.
- tasking.notification.list : lister les notifications de tâches/projets.
- tasking.notification.mark_read : marquer les notifications comme lues.
- tasking.project.create : créer un projet.
- tasking.project.delete : supprimer un projet.
- tasking.project.duplicate : dupliquer un projet.
- tasking.project.read : consulter un projet.
- tasking.project.update : modifier un projet.
- tasking.relation.create : créer une relation entre tâches.
- tasking.relation.delete : supprimer une relation.
- tasking.share.link : partager un lien de tâche.
- tasking.share.project : partager un projet / donner accès.
- tasking.task.create : créer une tâche.
- tasking.task.delete : supprimer/effacer une tâche.
- tasking.task.duplicate : dupliquer une tâche.
- tasking.task.partial_update : mettre à jour partiellement une tâche.
- tasking.task.update : mettre à jour une tâche.
- tasking.view.create : créer une vue.
- tasking.view.delete : supprimer une vue.
- tasking.view.update : modifier une vue.

Disambiguation : TOUTE tâche = tasking, même datée (« échéance » ≠ événement). Partage d'un
PROJET = tasking.share.project. Pièce jointe de tâche = tasking.attachment.* (jamais
file_management)."""

_OFFICE_CATALOG = """\
- office.callback.error : gérer une erreur de callback.
- office.callback.force_save : confirmer la sauvegarde forcée (« confirme la sauvegarde forcee »,
  « valide le force-save »).
- office.callback.save_ready : vérifier que le document a bien été sauvegardé (« vérifie
  l'enregistrement du document X », « contrôle que X a bien ete sauvegarde »).
- office.command.drop : déconnecter un utilisateur de l'édition / libérer le document (« deconnecte
  X de l'edition de ... »).
- office.command.forcesave : forcer l'enregistrement/sauvegarde du document.
- office.command.info : informations de commande renvoyées par l'éditeur (ex. qui édite le
  document, statut d'édition en cours).
- office.command.meta : mettre à jour les métadonnées du document (titre, propriétés).
- office.command.version : version du serveur de commande.
- office.converter.convert : convertir le document (ex. vers PDF).
- office.docbuilder.generate : générer un document par script.
- office.editor.deny_editing : interdire l'édition.
- office.editor.download_as : télécharger sous un autre format.
- office.editor.insert_image : insérer une image dans le document.
- office.editor.insert_table : insérer/ajouter un tableau dans le document (« ajoute un tableau de 3x2 »,
  « insère un tableau »).
- office.editor.remove_image : supprimer/retirer une ou plusieurs image(s)/photo(s) du document
  (« supprime la photo du document », « retire les images »).
 - office.editor.create : CREER un NOUVEAU document/fichier (ex. word/docx, cell/xlsx, slide/pptx) avec ou sans contenu initial, eventuellement dans un dossier (« crée un fichier test.docx dans le dossier test contenant "..." »). Choisir CELLE-CI quand la demande cree un fichier qui n'existe pas encore, jamais add_content.
 - office.editor.add_content : ajouter du contenu text/paragraph/mot dans un document EXISTANT
 - office.editor.edit_content : modifier le contenu du document (modifier du texte), modifie le style/format/taille.
-office.editor.remove_table : supprimer un tableau du document
- office.editor.modify_table : modifier un tableau du document (nombre des colonnes/lignes, contenue)
- office.editor.add_title : ajouter un titre au document.
- office.editor.mail_merge : publipostage/fusion d'emails.
- office.editor.refresh_file : actualiser le fichier dans l'éditeur.
- office.editor.refresh_history : actualiser l'historique.
- office.editor.start_filling : démarrer le remplissage du formulaire.
- office.wopi.check_file_info : vérifier l'état/informations du fichier via WOPI (métadonnées,
  droits, statut).
- office.wopi.enumerate_children : lister les enfants (WOPI).
- office.wopi.get_file : ouvrir/récupérer le fichier dans l'éditeur.
- office.wopi.lock : verrouiller le fichier.
- office.wopi.put_file : enregistrer le fichier (WOPI).
- office.wopi.put_relative : créer/enregistrer un fichier relatif.
- office.wopi.refresh_lock : renouveler le verrou.
- office.wopi.rename : renommer le fichier.
- office.wopi.unlock : déverrouiller le fichier.

Disambiguation : ouvrir/éditer/sauvegarder le CONTENU d'un document nommé (.docx/.xlsx/.pptx/.pdf)
= office. Partager un document/dossier, droits, liens, versions, corbeille = file_management.
Création from scratch = office ; génération depuis sources internes = knowledge_kb.
« Ouvre ... dans l'éditeur de documents » = office.wopi.get_file. Les office.command.* sont des
commandes envoyées à l'éditeur (forcesave, info, meta, version) ; les office.wopi.* sont des
opérations du serveur sur le FICHIER (check_file_info, get_file, put_file, put_relative, rename).
« Qui édite le document / statut d'édition » = office.command.info. « Vérifier les métadonnées ou
les droits du fichier » = office.wopi.check_file_info. « Version du serveur OnlyOffice » =
office.command.version. « Créer un fichier depuis un existant / enregistrer sous un autre nom » =
office.wopi.put_relative. « Déconnecte X de l'édition » = office.command.drop ; « Déverrouille le
fichier » = office.wopi.unlock ; « Force l'enregistrement » = office.command.forcesave ; « Confirme/
valide la sauvegarde forcée » = office.callback.force_save ; « Vérifie l'enregistrement / que le doc
a bien été sauvegardé » = office.callback.save_ready ; « Met à jour les métadonnées / le titre du
document » = office.command.meta (pas office.wopi.rename)."""

_MESSAGING_CATALOG = """\
- messaging.automation_rule.clone : dupliquer une règle d'automatisation.
- messaging.automation_rule.create : créer une règle d'automatisation.
- messaging.automation_rule.delete : supprimer une règle.
- messaging.automation_rule.update : modifier une règle.
- messaging.campaign.create : créer une campagne.
- messaging.campaign.delete : supprimer une campagne.
- messaging.campaign.update : modifier une campagne.
- messaging.canned_response.create : créer une réponse type.
- messaging.canned_response.delete : supprimer une réponse type.
- messaging.canned_response.update : modifier une réponse type.
- messaging.channel.create : créer un canal Telegram et y publier un premier message.
- messaging.conversation.assign : assigner une conversation.
- messaging.conversation.create : créer une conversation/ticket.
- messaging.conversation.custom_attributes : attributs personnalisés.
- messaging.conversation.delete : supprimer une conversation.
- messaging.conversation.labels : gérer les labels de conversation.
- messaging.conversation.mute : couper les notifications d'une conversation.
- messaging.conversation.participants : gérer les participants.
- messaging.conversation.read : lire une conversation.
- messaging.conversation.toggle_priority : priorité de la conversation.
- messaging.conversation.toggle_status : changer le statut (ouvert/résolu).
- messaging.conversation.toggle_typing : indicateur de frappe.
- messaging.conversation.transcript : transcription de la conversation.
- messaging.conversation.unmute : réactiver les notifications.
- messaging.conversation.update : modifier une conversation.
- messaging.group.create : créer un groupe Telegram avec les membres indiqués.
- messaging.inbox.create : créer une boîte de réception.
- messaging.inbox.delete : supprimer une boîte.
- messaging.inbox.register_webhook : enregistrer un webhook.
- messaging.inbox.update : modifier une boîte.
- messaging.macro.create : créer un macro.
- messaging.macro.delete : supprimer un macro.
- messaging.macro.execute : exécuter un macro.
- messaging.macro.update : modifier/corriger un raccourci ou macro.
- messaging.message.delete : supprimer un message.
- messaging.message.retry : réessayer un message.
- messaging.message.send : envoyer un message dans une conversation.
- messaging.message.update : modifier un message.
- messaging.notification.list : lister les notifications de messagerie.
- messaging.notification.mark_read : marquer les notifications comme lues.
- messaging.notification.snooze : reporter une notification.

Disambiguation : conversation client/support (Chatwoot) = messaging, même sur un thème métier.
Couper les notifications d'une conversation = mute. Email professionnel = email_action. Envoyer un
CONTENU PAR EMAIL (adresse email) = email_action (email.email_submission.set), pas
messaging.message.send, qui envoie un message DANS une conversation Chatwoot (« envoie la
transcription de la conversation a X@... » = messaging.conversation.transcript, l'envoi par email
étant géré par email_action).

Une action email n'est/liste JAMAIS un effet de bord : un envoi d'email
(« envoie ce fichier a X », « envoie le rapport ») = UNIQUEMENT
email.email_submission.set (+ email.blob.upload si une pièce jointe est
précisée). N'ajoute AUCUNE action de gestion de boîte (email.mailbox.*,
email.sieve_script.*, email.vacation_response.set, email.identity.*) sauf si
l'utilisateur demande explicitement de gérer ses dossiers, filtres,
réponses automatiques ou adresses d'expédition."""

_EMAIL_CATALOG = """\
- email.blob.copy : copier une pièce jointe.
- email.blob.get : récupérer/télécharger une pièce jointe précise.
- email.blob.lookup : chercher/localiser une pièce jointe (recherche SEULE, JAMAIS pour
  attacher/joindre/supprimer).
- email.blob.upload : attacher/joindre une pièce jointe à un email à envoyer.
- email.email.copy : copier un email.
- email.email.get : obtenir le CONTENU d'un email PRÉCIS et identifié (« récupère l'email X »,
  « lis l'email de X »).
- email.email.import : importer un email.
- email.email.parse : analyser le contenu / extraire les données d'un email (« analyse le contenu
  de l'email X », « extrait les données de l'email Y »).
- email.email.query : rechercher/lister des emails SANS obtenir leur contenu (« liste les mails »,
  « cherche un email de X »).
- email.email.query_changes : détecter les changements.
- email.email.search_snippet : extraits de recherche.
- email.email.set : définir/modifier un email.
- email.email_submission.query : interroger/lister les ENVOIS (emails déjà envoyés : « liste les
  emails envoyés », « recherche les mails envoyés a X »).
- email.email_submission.set : envoyer un email/invitation/rapport/document par email.
- email.identity.get : obtenir l'identité.
- email.identity.set : définir une identité/adresse d'expédition (« ajoute une nouvelle identité
  d'envoi X », « modifie l'adresse d'expédition X »).
- email.mailbox.create : créer un dossier de mail.
- email.mailbox.delete : supprimer un dossier.
- email.mailbox.get : obtenir un dossier PRÉCIS (son contenu : « affiche le contenu de la boite de
  reception », « liste mes dossiers email »).
- email.mailbox.query : lister/rechercher les dossiers/boites mail.
- email.mailbox.rename : renommer un dossier.
- email.mailbox.set : définir un dossier.
- email.push_subscription.get : obtenir les abonnements push.
- email.push_subscription.set : définir les abonnements push.
- email.quota.get : obtenir l'espace restant/occupé de la boîte mail (« indique l'espace restant »).
- email.sieve_script.query : lister les filtres/règles.
- email.sieve_script.set : définir un filtre/règle.
- email.sieve_script.validate : valider un script de filtre.
- email.smtp.data : données SMTP.
- email.smtp.mail_from : envoyer DEPUIS une adresse expéditeur précise (« envoie l'email depuis
  compta@groupe.fr », « prepare l'expédition depuis X »).
- email.smtp.rcpt_to : destinataires SMTP.
- email.thread.get : obtenir un fil de discussion.
- email.thread.get_changes : changements du fil.
- email.vacation_response.set : réponse automatique/absence.

Disambiguation : envoyer/répondre/transférer/rechercher un EMAIL professionnel = email_action.
Pièces jointes d'EMAIL = email.blob.* (jamais file_management). Répondre pour confirmer une
réunion = email_action + calendaring. Conversation client (Chatwoot) = messaging. Ne choisis pas
email.email.query/get comme étape préalable : « récupère l'email X » = email.email.get, « liste
les mails / cherche un email » = email.email.query, « copie l'email » = email.email.copy.
« Analyse le contenu de l'email X » = email.email.parse (pas query/get). « Liste les emails
envoyés / mails envoyés a X » = email.email_submission.query (les envois), pas email.email.query.
« Envoie l'email depuis l'adresse X » = email.smtp.mail_from ; « ajoute/modifie une identité
d'expédition » = email.identity.set. Attacher/joindre une pièce jointe à un envoi =
email.blob.upload (jamais email.blob.lookup, qui n'est qu'une recherche)."""

_FILE_CATALOG = """\
- file.copy : copier un fichier/dossier.
- file.delete : supprimer un fichier/dossier (PAS la corbeille).
- file.direct_editing : édition directe du fichier.
- file.get : télécharger/récupérer un fichier.
- file.mkcol : créer un dossier/collection.
- file.move : déplacer/renommer un fichier ou dossier.
- file.propfind : lire les propriétés/métadonnées.
- file.proppatch : modifier les propriétés.
- file.put : téléverser/déposer un fichier.
- file.share.create : créer un partage / lien de partage.
- file.share.delete : supprimer un partage.
- file.share.transfer : transférer un partage.
- file.share.update : modifier les droits d'un partage.
- file.trashbin.list : lister la corbeille.
- file.trashbin.purge : vider/supprimer DÉFINITIVEMENT le contenu de la corbeille (« vide la
  corbeille »).
- file.trashbin.restore : restaurer depuis la corbeille.
- file.version.list : lister les versions du fichier.
- file.version.restore : restaurer une version.

Disambiguation : partager un document/dossier, créer un lien, droits d'accès, versions, corbeille,
déplacer/supprimer/téléverser = file_management. Modifier le CONTENU d'un document bureautique =
office. Partager un ESPACE de connaissance = knowledge_kb."""

_CALENDAR_CATALOG = """\
- calendar.create : créer un calendrier/agenda.
- calendar.list : lister les calendriers.
- event.create : créer un événement/réunion/rendez-vous.
- event.delete : supprimer un événement.
- event.free_busy : afficher les disponibilités.
- event.modify : modifier/reporter/décaler la date, l'heure ou le lieu d'un événement/réunion
  EXISTANT (jamais event.create pour « modifie la réunion »).
- event.update : synonyme de event.modify.
- event.multiget : obtenir plusieurs événements précis.
- event.query : lister/requêter les événements (uniquement si la demande liste ou affiche des
  événements).
- event.recurrence : gérer la récurrence.
- event.reminder : gérer les rappels.
- event.sync : synchroniser l'agenda.

Disambiguation : réunion/rendez-vous/événement/créneau = calendaring. Une TÂCHE datée reste
tasking. Une réunion planifiée dans un contexte métier (lead, candidat) = calendaring + domaine
métier. Confirmer une réunion par email = email_action + calendaring. Ne choisis pas event.query
comme étape préalable : « supprime l'événement » = event.delete seul, « crée l'événement » =
event.create seul, « lève mes disponibilités » = event.free_busy seul."""

_KB_CATALOG = """\
- kb.answer.grounded : répondre à une question à partir des sources, en citant les sources.
- kb.export.pdf : exporter un rapport en PDF.
- kb.generate.image : générer une infographie/image.
- kb.generate.podcast : générer un podcast.
- kb.generate.report : générer un rapport/synthèse.
- kb.generate.video : générer une vidéo.
- kb.ingest.document : importer/ingérer et classer automatiquement des documents dans la base
  (« classe automatiquement les documents », « indexe/ajoute ces documents »).
- kb.ingest.url : importer une URL source.
- kb.ingest.youtube : importer une vidéo YouTube.
- kb.note.create : créer une note.
- kb.search.fulltext : recherche par MOTS-CLÉS / terme EXACT (« documents contenant 'X' »,
  « cherche le mot exact 'X' », « contenant le terme 'Y' et 'Z' »).
- kb.search.vector : recherche SÉMANTIQUE par THÉMATIQUE (« documents qui abordent la
  thématique... », « sur le thème de... »).
- kb.space.create : créer un espace de connaissance ou un sous-dossier dans un espace.
- kb.space.delete : supprimer un espace.
- kb.space.read : consulter/lire un espace (son contenu, ses éléments).
- kb.space.update : modifier un espace : renommer, droits/accès, membres, supprimer des doublons,
  annuler/revenir en arrière sur un classement automatique.
- kb.status.batch : statut de traitement en lot.
- kb.status.processing : statut de traitement.

Disambiguation : espace/base de connaissances/« nos sources »/SurfSense → knowledge_kb. Toute
génération de contenu depuis les sources internes (rapport, synthèse, infographie, vidéo, podcast)
= knowledge_kb. Partager/donner des droits sur un ESPACE = knowledge_kb (pas file_management).
Répondre à une question à partir des sources = kb.answer.grounded. Chercher/lister des résultats
de recherche = kb.search.vector/fulltext. Consulter le contenu d'un espace (documents modifiés,
membres) = kb.space.read. Importer une source (document/URL/YouTube) dans un espace =
kb.ingest.* (pas kb.space.update)."""

_GENERAL_CATALOG = """\
- conseil personnel : conseil de vie privée.
- conseil professionnel - rh : conseil RH (salariés, embauche, licenciement...).
- conseil professionnel - accounting : conseil comptable.
- conseil professionnel - administration : conseil administratif.
- conseil professionnel - artisanat : conseil métiers de l'artisanat.
- conseil professionnel - automobile : conseil secteur automobile.
- conseil professionnel - finance : conseil finance.
- conseil professionnel - fiscalité : conseil fiscal.
- conseil professionnel - juridique : conseil juridique.
- conseil professionnel - management : conseil management/organisation.
- conseil professionnel - marketing : conseil marketing.
- conseil professionnel - édition : conseil secteur de l'édition.
- general : salutation, question générale, conversation sans action.
- redaction : rédaction libre d'un texte.
- resume : résumé/synthèse d'un contenu.
- traduction : traduction d'un texte.

Disambiguation : tout conseil/explication sans action système = general. La rédaction libre, le
résumé et la traduction sont des actions general (pas office). Choisir le bon sous-type
professionnel selon la spécialité demandée. Une question PERSONNELLE ou privée (logement,
rénovation de sa maison, voyage, loisirs, santé, photographie, musique, retraite personnelle...) =
conseil personnel, MÊME si le thème touche un métier (artisanat, finance, édition...). Une
question liée au MÉTIER ou à l'activité professionnelle de l'utilisateur (gérer son entreprise,
ses salariés, facturer, gérer son commerce...) = conseil professionnel - <secteur> selon le
domaine concerné. Une salutation ou question générale sans conseil = general."""

_ACTION_CATALOGS = {
    "accounting": _ACCOUNTING_CATALOG,
    "calendaring": _CALENDAR_CATALOG,
    "contacts": _CONTACTS_CATALOG,
    "crm": _CRM_CATALOG,
    "email_action": _EMAIL_CATALOG,
    "file_management": _FILE_CATALOG,
    "general": _GENERAL_CATALOG,
    "hr": _HR_CATALOG,
    "knowledge_kb": _KB_CATALOG,
    "messaging": _MESSAGING_CATALOG,
    "office": _OFFICE_CATALOG,
    "sales": _SALES_CATALOG,
    "tasking": _TASKING_CATALOG,
}

_ACTION_EXAMPLES: dict[str, str] = {
    "accounting": """Demande : « Crée le paiement client de Artemis Design. »
Réponse : {"actions": ["account.journal.create_customer_payment", "res.partner.search_read"]}

Demande : « Crée la facture fournisseur de TechNova. »
Réponse : {"actions": ["account.journal.action_create_vendor_bill"]}

Demande : « Saisis le règlement a Cora Group. »
Réponse : {"actions": ["account.journal.create_supplier_payment"]}

Demande : « Crée le paiement fournisseur de Atelier Martin. »
Réponse : {"actions": ["res.partner.search_read", "account.journal.create_supplier_payment"]}

Demande : « Saisis l'encaissement de LogiTrans. »
Réponse : {"actions": ["res.partner.search_read", "account.journal.create_customer_payment"]}

Demande : « Crée une facture pour BureauPlus. »
Réponse : {"actions": ["res.partner.search_read", "account.move.create"]}

Demande : « Télécharge la facture TechNova en PDF. »
Réponse : {"actions": ["account.move.action_invoice_download_pdf"]}

Demande : « Valide la facture Solenco. »
Réponse : {"actions": ["account.move.action_post"]}

Demande : « Duplique la facture BureauPlus. »
Réponse : {"actions": ["account.move.action_duplicate"]}

Demande : « Valide la facture Medisys et envoie-la au client. »
Réponse : {"actions": ["account.move.action_post"]}

Demande : « Envoie la facture LogiTrans au client et transmets-la en copie. »
Réponse : {"actions": ["account.move.action_get"]}

Demande : « Ouvre le rapprochement pour la facture Verdeco. »
Réponse : {"actions": ["account.move.open_reconcile_view", "account.move.search_read"]}

Demande : « Clôture le règlement 12 800 EUR. »
Réponse : {"actions": ["account.payment.action_cancel"]}

Demande : « Enregistre le paiement de TechNova et mets a jour la fiche client. »
Réponse : {"actions": ["res.partner.search_read", "account.move.action_register_payment", "res.partner.write"]}

Demande : « Enregistre le remboursement de la note de frais de Chloé dans la comptabilité d'Odoo. »
Réponse : {"actions": ["account.payment.register.create_payments"]}

Demande : « Montre les paiements de la facture Medisys. »
Réponse : {"actions": ["account.move.search_read", "account.move.open_payments"]}

Demande : « Paye la note de frais de Jean Dupont et comptabilise-la dans la comptabilité d'Odoo. »
Réponse : {"actions": ["account.move.action_post"]}""",
    "calendaring": """Demande : « Supprime l'événement de ce soir. »
Réponse : {"actions": ["event.delete"]}

Demande : « Crée un événement avant le 30 a 10h. »
Réponse : {"actions": ["event.create"]}

Demande : « Affiche mes disponibilités le mois dernier. »
Réponse : {"actions": ["event.free_busy"]}

Demande : « Modifie la date de la réunion TechNova à lundi 14h. »
Réponse : {"actions": ["event.modify"]}

Demande : « Déplace la réunion equipe dev au 21 septembre à 9h. »
Réponse : {"actions": ["event.modify"]}

Demande : « Récupère les details de plusieurs événements s'il te plaît. »
Réponse : {"actions": ["event.multiget"]}

Demande : « Repousse la tache a ce soir et met a jour le calendrier du projet. »
Réponse : {"actions": ["event.create"]}""",
    "contacts": """Demande : « Supprime le contact Pierre Moreau. »
Réponse : {"actions": ["res.partner.search_read", "res.partner.unlink"]}

Demande : « Donne l'adresse de livraison de Dupont & Fils. »
Réponse : {"actions": ["res.partner.search_read", "res.partner.address_get"]}

Demande : « Groupe les contacts par societe s'il te plaît. »
Réponse : {"actions": ["res.partner.read_group"]}

Demande : « Mets a jour la fiche de Lucas Girard. »
Réponse : {"actions": ["res.partner.search_read", "res.partner.write"]}

Demande : « Ajoute le tag 'Priorité haute' au contact 'SudCo Distribution'. »
Réponse : {"actions": ["res.partner.search_read", "res.partner.write"]}

Demande : « Crée la societe Solenco et son contact principal. »
Réponse : {"actions": ["res.partner.create_company", "res.partner.create"]}

Demande : « Crée le contact professionnel de Ines Bouzid. »
Réponse : {"actions": ["res.partner.create"]}

Demande : « Fusionne les deux fiches prospects en doublon pour Verdeco, mets à jour ses coordonnées sur la fiche contact, et crée une tâche. »
Réponse : {"actions": ["res.partner.find_or_create", "res.partner.write"]}""",
    "crm": """Demande : « Convertit le lead BatiNord en opportunité. »
Réponse : {"actions": ["crm.lead.convert_opportunity"]}

Demande : « Applique la conversion du lead Dupont & Fils en opportunité. »
Réponse : {"actions": ["crm.lead2opportunity.partner.action_apply"]}

Demande : « Fusionne les deux opportunités BioMarche en une seule. »
Réponse : {"actions": ["crm.lead.merge_opportunity"]}

Demande : « Fusionne les opportunités selectionnees de Solenco. »
Réponse : {"actions": ["crm.merge.opportunity.action_merge"]}

Demande : « Marque le lead BureauPlus comme perdu. »
Réponse : {"actions": ["crm.lead.action_set_lost"]}

Demande : « J'ai eu Vincent Roche au téléphone, il est partant pour la mise aux normes du local. Valide son opportunité dans Odoo et planifie un rendez-vous avec lui. »
Réponse : {"actions": ["crm.lead.write", "crm.lead.action_schedule_meeting"]}""",
    "email_action": """Demande : « Réponds a l'email de Hugo Lefebvre pour confirmer la réunion la semaine prochaine. »
Réponse : {"actions": ["email.email.query", "email.email_submission.set"]}

Demande : « Affiche l'email de Lucas Girard. »
Réponse : {"actions": ["email.email.get"]}

Demande : « Recherche les emails de Medisys. »
Réponse : {"actions": ["email.email.query"]}

Demande : « Analyse le contenu de l'email Verdeco. »
Réponse : {"actions": ["email.email.parse"]}

Demande : « Liste les emails envoyés ce mois-ci s'il te plaît. »
Réponse : {"actions": ["email.email_submission.query"]}

Demande : « Envoie l'email depuis l'adresse compta@groupe.fr. »
Réponse : {"actions": ["email.smtp.mail_from"]}

Demande : « Ajoute une nouvelle identité d'envoi direction@entreprise.fr. »
Réponse : {"actions": ["email.identity.set"]}

Demande : « Copie la pièce jointe note_interne.docx vers un autre email. »
Réponse : {"actions": ["email.blob.copy", "email.blob.lookup"]}

Demande : « Récupère la pièce jointe de l'email BioMarche. »
Réponse : {"actions": ["email.blob.get"]}

Demande : « Joint le fichier facture_2026-045.pdf au mail. »
Réponse : {"actions": ["email.blob.upload"]}

Demande : « Envoie l'email a Atelier Martin avec la pièce jointe planning_chantier.pdf. »
Réponse : {"actions": ["email.email_submission.set", "email.blob.upload"]}

Demande : « Télécharge l'attachement devis_2026-118.pdf. »
Réponse : {"actions": ["email.blob.get"]}

Demande : « Réutilise l'attachement devis_2026-118.pdf. »
Réponse : {"actions": ["email.blob.copy", "email.blob.lookup"]}

Demande : « Crée la réunion ce soir et envoie l'invitation aux participants. »
Réponse : {"actions": ["email.email_submission.set"]}

Demande : « Génère le rapport via docbuilder et envoie-le a clients@boutique. »
Réponse : {"actions": ["email.email_submission.set"]}

Demande : « Téléverse note_interne.docx dans Nextcloud, ajoute-le a la base de connaissances et envoie un email de confirmation. »
Réponse : {"actions": ["email.email_submission.set"]}

Demande : « Copie l'email TechNova vers le dossier archive. »
Réponse : {"actions": ["email.email.copy", "email.mailbox.set"]}

Demande : « Crée une règle qui classe les emails de BioMarche dans le dossier 'Prospects'. »
Réponse : {"actions": ["email.mailbox.create", "email.sieve_script.set"]}

Demande : « Télécharge le document suivi_clients.xlsx au format PDF et envoie-le par email a Bruno Chevalier. »
Réponse : {"actions": ["email.email_submission.set"]}""",
    "file_management": """Demande : « Copie devis_2026-118.pdf vers le dossier audit securite. »
Réponse : {"actions": ["file.copy"]}

Demande : « Vide la corbeille definitivement s'il te plaît. »
Réponse : {"actions": ["file.trashbin.purge"]}

Demande : « Restaure le fichier cahier_des_charges.docx depuis la corbeille. »
Réponse : {"actions": ["file.trashbin.restore"]}

Demande : « Supprime definitivement devis_2026-118.pdf. »
Réponse : {"actions": ["file.trashbin.purge"]}

Demande : « Transfère la propriété du dossier reorganisation a Pierre Moreau. »
Réponse : {"actions": ["file.share.transfer"]}

Demande : « Liste les fichiers du dossier lancement produit. »
Réponse : {"actions": ["file.propfind"]}

Demande : « Téléverse devis_2026-118.pdf sur le cloud. »
Réponse : {"actions": ["file.put"]}

Demande : « Convertit note_interne.docx en PDF puis partage-le avec Emma Rousseau. »
Réponse : {"actions": ["file.share.create"]}""",
    "general": """Demande : « Quel budget prévoir pour rénover une salle de bain de 6m² ? »
Réponse : {"actions": ["conseil personnel"]}

Demande : « Rédige une réponse polie à la réclamation envoyée par Bois & Sens. »
Réponse : {"actions": ["redaction"]}

Demande : « Condense cette proposition de 10 pages en un résumé d'une demi-page. »
Réponse : {"actions": ["resume"]}

Demande : « Bonjour s'il te plaît. »
Réponse : {"actions": ["general"]}

Demande : « En tant que responsable RH, comment rédiger une offre d'emploi attractive ? »
Réponse : {"actions": ["conseil professionnel - rh"]}

Demande : « Comment transmettre un patrimoine à mes enfants en limitant les droits de succession ? »
Réponse : {"actions": ["conseil personnel"]}

Demande : « Comment calculer la marge bénéficiaire idéale sur des travaux de rénovation ? »
Réponse : {"actions": ["conseil professionnel - finance"]}

Demande : « Quelles démarches administratives pour créer une association loi 1901 ? »
Réponse : {"actions": ["conseil professionnel - juridique"]}

Demande : « Quels sont les droits d'un salarié en cas d'arrêt maladie prolongé ? »
Réponse : {"actions": ["conseil professionnel - juridique"]}

Demande : « Quelles sont les charges déductibles pour un électricien auto-entrepreneur ? »
Réponse : {"actions": ["conseil professionnel - accounting"]}

Demande : « Quelle régularité de publication adopter pour mon blog personnel ? »
Réponse : {"actions": ["conseil professionnel - marketing"]}

Demande : « Quel matériel choisir pour commencer la sculpture sur bois ? »
Réponse : {"actions": ["conseil personnel"]}

Demande : « Quelle est la différence entre un PER individuel et un PER collectif ? »
Réponse : {"actions": ["conseil personnel"]}""",
    "hr": """Demande : « Transforme le candidat Karim Benali en employe. »
Réponse : {"actions": ["hr.applicant.create_employee_from_applicant"]}

Demande : « Crée une fiche employe pour Hugo Lefebvre. »
Réponse : {"actions": ["hr.employee.create"]}

Demande : « Clôture le dossier de Jean Dupont. »
Réponse : {"actions": ["hr.applicant.archive_applicant"]}

Demande : « Approuve le congé de Ines Bouzid. »
Réponse : {"actions": ["hr.leave.action_approve"]}

Demande : « Passe en priorité urgente la conversation de LogiTrans sur Chatwoot, crée une tâche et vérifie qui de l'équipe support est disponible. »
Réponse : {"actions": ["hr.employee.search_read"]}

Demande : « Crée le dossier employe de Ines Bouzid et crée aussi son contact. »
Réponse : {"actions": ["hr.employee.create", "res.partner.create"]}

Demande : « Transforme la candidature de Camille en fiche employé, crée son contact professionnel dans Odoo, et planifie son entretien d'embauche. »
Réponse : {"actions": ["hr.applicant.create_employee_from_applicant", "res.partner.create", "hr.applicant.action_create_meeting"]}

Demande : « Paye la note de frais de Jean Dupont et comptabilise-la. »
Réponse : {"actions": ["hr.expense.action_pay"]}

Demande : « Approuve la note de frais de Emma, enregistre le remboursement dans la comptabilité d'Odoo, et bloque un créneau pour la réunion. »
Réponse : {"actions": ["hr.expense.action_approve"]}

Demande : « Approuve le congé de Sandra pour dans 48h, préviens-le par email de la validation, et ajoute son absence à l'agenda. »
Réponse : {"actions": ["hr.leave.action_approve"]}""",
    "knowledge_kb": """Demande : « Quel est le barème interne des frais kilométriques pour les commerciaux ? »
Réponse : {"actions": ["kb.answer.grounded"]}

Demande : « D'après nos sources documentaires, trouve tous les documents contenant 'RGE 2026'. »
Réponse : {"actions": ["kb.search.fulltext"]}

Demande : « Dans notre base de connaissances, cherche les documents qui abordent la thématique de la garantie décennale. »
Réponse : {"actions": ["kb.search.vector"]}

Demande : « Dans notre base interne, classe automatiquement les 20 documents en attente dans les catégories existantes. »
Réponse : {"actions": ["kb.ingest.document"]}

Demande : « Dans surfsence, crée un sous-dossier 'Contrats 2026' dans mon espace 'Documentation juridique'. »
Réponse : {"actions": ["kb.space.create"]}

Demande : « Supprime l'accès de l'ancien stagiaire à tous les espaces partagés. »
Réponse : {"actions": ["kb.space.update"]}

Demande : « Télécharge rapport_trimestriel.docx en PDF et ajoute-le a l'espace de connaissances. »
Réponse : {"actions": ["kb.export.pdf"]}

Demande : « Ajoute une note a l'espace de connaissances sur le suivi des clients. »
Réponse : {"actions": ["kb.note.create"]}

Demande : « Cherche la procédure interne concernant la mise aux normes du local dans notre base SurfSense. »
Réponse : {"actions": ["kb.search.vector"]}""",
    "messaging": """Demande : « Mets le ticket de Artemis Design en priorité haute. »
Réponse : {"actions": ["messaging.conversation.toggle_priority"]}

Demande : « Corrige le raccourci d'actions s'il te plaît. »
Réponse : {"actions": ["messaging.macro.update"]}

Demande : « Crée une conversation avec Emma Rousseau. »
Réponse : {"actions": ["messaging.conversation.create"]}

Demande : « Affecte les leads a Marie Lambert et previens-le par message. »
Réponse : {"actions": ["messaging.message.send"]}

Demande : « Passe la tache dans la colonne 'Terminé' et notifie l'equipe. »
Réponse : {"actions": ["messaging.notification.list"]}

Demande : « Crée un filtre Chatwoot pour surveiller la conversation des contacts tagués 'Prospect chaud'. »
Réponse : {"actions": ["messaging.automation_rule.create"]}

Demande : « Crée un groupe Telegram 'Projet Alpha' avec @tayssir et @sami. »
Réponse : {"actions": ["messaging.group.create"]}

Demande : « Crée un canal Telegram 'Actu' et publie le message Bonjour. »
Réponse : {"actions": ["messaging.channel.create"]}""",
    "office": """Demande : « Mets le texte en gras. »
Réponse : {"actions": ["office.editor.edit_content"]}

Demande : « Change la couleur du texte en bleu. »
Réponse : {"actions": ["office.editor.edit_content"]}

Demande : « Mets tout le texte en italique. »
Réponse : {"actions": ["office.editor.edit_content"]}

Demande : « Ajoute un titre 'Introduction' au debut du document. »
Réponse : {"actions": ["office.editor.add_title"]}

Demande : « Insere une image logo.png au debut du document. »
Réponse : {"actions": ["office.editor.insert_image"]}

Demande : « Ajoute un tableau de 3 colonnes et 2 lignes. »
Réponse : {"actions": ["office.editor.insert_table"]}

Demande : « Supprime la photo du document. »
Réponse : {"actions": ["office.editor.remove_image"]}

Demande : « Crée un fichier test.docx dans le dossier test contenant bonjour. »
Réponse : {"actions": ["office.editor.create"]}

Demande : « Crée une présentation formation.pptx avec le titre Les bases de Excel. »
Réponse : {"actions": ["office.editor.create"]}

Demande : « Ajoute un paragraphe avec le texte 'Bonjour le monde' a la fin du fichier. »
Réponse : {"actions": ["office.editor.add_content"]}

Demande: « Supprime le paragraphe 3 du document. »
Réponse : {"actions": ["office.editor.edit_content"]}

Demande : « Ajoute le tableau de taille 3*2 »
Réponse :{"action":["office.editor.insert_table"]}

Demande : « Supprime le tableau »
Réponse :{"action":["office.editor.remove_table"]}

Demande : « ajouter une nouvelle colonne »
Réponse :{"action":["office.editor.modify_table"]}

Demande : « Ouvre cahier_des_charges.docx dans l'editeur de documents. »
Réponse : {"actions": ["office.wopi.get_file"]}

Demande : « Affiche qui edite actuellement planning_chantier.pdf. »
Réponse : {"actions": ["office.command.info"]}

Demande : « Convertit rapport_trimestriel.docx en PDF. »
Réponse : {"actions": ["office.converter.convert"]}

Demande : « Deconnecte Ines Bouzid de l'edition de proposition_commerciale.pdf. »
Réponse : {"actions": ["office.command.drop"]}

Demande : « Déverrouille le fichier proposition_commerciale.pdf. »
Réponse : {"actions": ["office.wopi.unlock"]}

Demande : « Force l'enregistrement du document note_interne.docx. »
Réponse : {"actions": ["office.command.forcesave"]}

Demande : « Vérifie l'enregistrement du document rapport_trimestriel.docx. »
Réponse : {"actions": ["office.callback.save_ready"]}

Demande : « Confirme la sauvegarde forcee de facture_2026-045.pdf. »
Réponse : {"actions": ["office.callback.force_save"]}

Demande : « Met a jour les métadonnées du document contrat_annexe.pdf. »
Réponse : {"actions": ["office.command.meta"]}

Demande : « Télécharge le document rapport_trimestriel.docx en PDF. »
Réponse : {"actions": ["office.editor.download_as"]}""",
    "sales": """Demande : « Confirme la commande de Artemis Design et crée la facture d'acompte. »
Réponse : {"actions": ["sale.order.action_confirm", "sale.advance.payment.inv.create_invoices"]}

Demande : « Envoie le devis Atelier Martin au client. »
Réponse : {"actions": ["sale.order.action_quotation_send"]}

Demande : « Annule la commande de BioMarche. »
Réponse : {"actions": ["sale.order.action_cancel"]}

Demande : « Verrouille la commande BatiNord. »
Réponse : {"actions": ["sale.order.action_lock"]}

Demande : « Convertit le lead BatiNord en opportunité puis crée le devis. »
Réponse : {"actions": ["sale.order.create"]}

Demande : « Convertis le lead de Ahmed Trabelsi en opportunité, crée le devis correspondant pour 15 000 €, et envoie-le directement par email. »
Réponse : {"actions": ["sale.order.create", "sale.order.action_quotation_send"]}""",
    "tasking": """Demande : « Crée la tache dans migration ERP et assigne-la a Thomas Petit. »
Réponse : {"actions": ["tasking.task.create", "tasking.assignee.add"]}

Demande : « Efface la tache du projet s'il te plaît. »
Réponse : {"actions": ["tasking.task.delete"]}

Demande : « Déplace la tache vers la colonne 'Terminé' s'il te plaît. »
Réponse : {"actions": ["tasking.bucket.move_task"]}

Demande : « Repousse la tache a ce soir et met a jour le calendrier du projet. »
Réponse : {"actions": ["tasking.task.update"]}

Demande : « On vient de décrocher l'appel d'offres pour Green Solutions, c'est 2,3 millions d'euros. Passe l'opportunité en gagnée dans le CRM avec ce montant, crée le projet 'Contrat maintenance 2027' et assigne-le à Emma. »
Réponse : {"actions": ["tasking.project.create", "tasking.assignee.add"]}""",
}

_ACTION_PROMPT_TEMPLATE = """# Rôle

Tu es le module de sélection d'actions de Viito pour le domaine {domain} ({tool}).
Tu reçois la demande de l'utilisateur et le catalogue des actions de CE domaine. Ta seule tâche :
choisir, DANS CE CATALOGUE, les actions à exécuter, dans l'ordre d'exécution. Tu ne réponds JAMAIS
à la demande : tu ne fais que sélectionner des actions.

# Format de sortie (obligatoire)

Réponds UNIQUEMENT avec un objet JSON valide, sans texte avant ni après, sans balise markdown,
sans explication :

{{"actions": ["<action_id>", "<action_id>", "..."]}}

Règles de sortie :
- Choisis UNIQUEMENT parmi les identifiants du catalogue ci-dessous. Jamais ailleurs.
- Recopie chaque identifiant TEL QUEL (minuscules, points, tirets). Ne le réécris pas, ne le
  traduis pas, ne l'abrège pas.
- Une seule action → tableau à 1 élément. Plusieurs actions → tableau ordonné par exécution.
- Sans doublon.
- Les actions de lecture/recherche (*.search_read, *.read_group, *.query, *.get, *.lookup,
  *.read, event.query, event.multiget, event.free_busy, kb.search.*) sont utilisées quand la
  demande consiste EXPLICITEMENT à lire, lister, chercher, afficher, consulter, grouper,
  compter, vérifier, indiquer, récupérer ou obtenir une information (« liste les leads »,
  « affiche l'adresse », « recherche le contact », « groupe les contacts par société », « quel
  est le statut... », « vérifie qui est disponible... », « lis l'email de X », « récupère le
  mail de X »).
- Une LECTURE PRÉALABLE (trouver l'enregistrement AVANT l'action demandée) n'est AUTORISÉE que
  dans les cas listés ci-dessous (contact nommé ; paiement ou facture pour un partenaire nommé ;
  paiements ou rapprochement d'une facture nommée). Dans TOUS les autres cas, le simple fait que
  la phrase cite un NOM (société, contact, facture, client, employé, lead, commande) ne
  déclenche AUCUNE lecture : « crée », « valide », « convertit », « clôture », « approuve »,
  « envoie », « confirme », « imprime », « télécharge », « annule », « supprime » (hors contact)
  n'impliquent PAS de recherche. Exemples : « Crée la facture fournisseur de TechNova » →
  account.journal.action_create_vendor_bill seul ; « Saisis le règlement à Cora Group » →
  account.journal.create_supplier_payment seul ; « Valide la facture Solenco » →
  account.move.action_post seul ; « Duplique la facture BureauPlus » → account.move.action_duplicate
  seul ; « Télécharge la facture TechNova en PDF » → account.move.action_invoice_download_pdf seul ;
  « Liste les entités de X » → res.company.action_all_company_branches seul ; « crée l'événement »
  → event.create seul ; « confirme la commande » → sale.order.action_confirm seul ; « archive le
  lead X » → crm.lead.action_archive seul ; « crée une fiche employe pour X » → hr.employee.create
  seul.
- LECTURES PRÉALABLES AUTORISÉES (uniquement ces cas) :
  * CONTACT nommé : supprimer, mettre à jour, dupliquer, afficher l'adresse ou ouvrir l'entité
    commerciale d'un contact/société nommé → res.partner.search_read + l'action (« Supprime le
    contact Pierre Moreau » = res.partner.search_read + res.partner.unlink ; « Mets a jour la
    fiche de X » = res.partner.search_read + res.partner.write ; « Ajoute le tag Y au contact Z »
    = res.partner.search_read + res.partner.write ; « Duplique la fiche de X » =
    res.partner.search_read + res.partner.copy ; « Donne l'adresse de X » = res.partner.search_read
    + res.partner.address_get).
  * Créer un PAIEMENT pour un partenaire nommé (« Crée le paiement client de X », « Saisis
    l'encaissement de X », « Crée le paiement fournisseur de X ») → res.partner.search_read +
    account.journal.create_customer_payment / account.journal.create_supplier_payment. « Saisis le
    règlement a X » (saisie directe) = account.journal.create_supplier_payment seul.
  * Créer une FACTURE pour un partenaire nommé (« Crée une facture pour X ») =
    res.partner.search_read + account.move.create.
  * Ouvrir les PAIEMENTS ou le RAPPROCHEMENT d'une facture nommée = account.move.search_read +
    account.move.open_payments / account.move.open_reconcile_view.
  Pour TOUT le reste (valider/post/dupliquer/imprimer/hacher/télécharger une facture nommée,
  facture fournisseur, écriture, taux de change, devis, commande, lead, employé, événement,
  ticket, tâche), AUCUNE lecture préalable. En cas de doute, n'ajoute PAS la lecture.
- Quand le domaine email_action est fourni, tout envoi par EMAIL (envoyer une invitation, un
  rapport, un document, un devis, un lien, notifier par email) = email.email_submission.set,
  même si l'action principale appartient à un autre domaine (« crée la réunion et envoie
  l'invitation » = event.create + email.email_submission.set ; « génère le rapport et envoie-le
  à X » = office.docbuilder.generate + email.email_submission.set). Ne remplace jamais
  email.email_submission.set par l'action d'envoi d'un autre domaine.
- Sélectionne TOUTES les actions demandées par la phrase, dans l'ordre d'exécution, sans en
  sauter une seule et sans en inventer. Chaque action du catalogue a un sens précis : ne choisis
  pas un identifiant « proche » du bon quand un identifiant exact existe.
- Tu ne sélectionnes QUE des actions de TON domaine {domain}. Une demande peut mélanger des
  actions de plusieurs domaines (comptabiliser une facture = accounting, envoyer un email ou une
  invitation = email_action, créer un événement/réunion = calendaring, modifier une fiche contact
  = contacts, créer une tâche = tasking...) : d'autres modules s'occupent de ces domaines. N'émet
  ici QUE la partie de la demande qui relève de {domain}. Exemples : « paye la note de frais et
  comptabilise-la » (hr) → hr.expense.action_pay seul ; « envoie la transcription de la
  conversation par email » (messaging) → messaging.conversation.transcript seul ; « enregistre le
  paiement et mets a jour la fiche client » (accounting) → account.move.action_register_payment
  seul.
- Si aucune action du catalogue ne correspond à la demande → {{"actions": []}}.
- Ne déduis jamais d'identifiant : si un choix semble absent du catalogue, retourne {{"actions": []}}.

# Exemples ({domain})

{examples}

# Catalogue ({domain})

{catalog}

# Rappel

Sortie = UNIQUEMENT le JSON {{"actions": [...]}}. Rien d'autre.
"""

ACTION_SYSTEM_PROMPTS_BY_DOMAIN: dict[str, str] = {
    domain: _ACTION_PROMPT_TEMPLATE.format(
        domain=domain, tool=_DOMAIN_TOOLS[domain], catalog=catalog, examples=_ACTION_EXAMPLES.get(domain, "")
    )
    for domain, catalog in _ACTION_CATALOGS.items()
}


class ActionClassifierClient(DomainClassifierClient):
    def __init__(self, config: ActionClassifierConfig, system_prompt: str | None = None) -> None:
        super().__init__(config, system_prompt)


def build_action_classifier_client(
    *,
    model: str,
    api_key: str | None = None,
    temperature: float = 0.0,
    top_p: float = 1.0,
    max_tokens: int = 256,
    frequency_penalty: float = 0.0,
    presence_penalty: float = 0.0,
    seed: int | None = 42,
    base_url: str = "https://openrouter.ai/api/v1",
    site_url: str | None = None,
    app_name: str = "activity-ai-engine",
    json_mode: bool = True,
) -> ActionClassifierClient:
    resolved_api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not resolved_api_key:
        raise ValueError("OPENROUTER_API_KEY is required to use an OpenRouter client")

    return ActionClassifierClient(
        ActionClassifierConfig(
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


_ACTION_ID_TOKENS = re.compile(r"[a-z0-9]+")


def _keyword_tokens(value: str) -> set[str]:
    return set(_ACTION_ID_TOKENS.findall(value.lower()))


def _fold_accents(value: str) -> str:
    import unicodedata
    normalized = unicodedata.normalize("NFD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


class RuleBasedActionClassifierClient:
    STOP_TOKENS = frozenset({
        "account", "crm", "hr", "sales", "office", "tasking", "messaging", "email",
        "file", "event", "kb", "res", "sale", "calendar", "calendaring", "contacts",
        "contact", "action", "create", "write", "set", "get", "update", "delete",
        "read", "search", "view", "list", "open", "send", "add", "remove", "put",
        "move", "copy", "de", "des", "du", "dans", "pour", "avec", "une", "un",
    })

    TRIGGER_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
        (("conseil personnel", "conseille-moi", "conseil de vie", "conseil perso"), "conseil personnel"),
        (("conseil professionnel", "conseil pro", "conseille-moi sur le plan"), "conseil professionnel - management"),
        (("résumé", "resume", "synthetise", "résume"), "resume"),
        (("rédige", "redige", "rédiger", "rediger", "paragraphe", "texte à écrire",
          "ecris", "écris", "génère un texte", "genere un texte"), "redaction"),
        (("tradu", "traduction", "translate"), "traduction"),
        (("bonjour", "salut", "bonsoir", "que peux-tu faire", "qu'est-ce que tu sais faire", "qui es-tu", "merci", "au revoir"), "general"),
        (("valide la facture", "validez la facture", "comptabilise la facture", "poste la facture", "poste l'ecriture", "valide l'ecriture"), "account.move.action_post"),
        (("envoie la facture", "envoyer la facture", "envoie la commande", "transmets la facture"), "account.move.action_get"),
        (("enregistre le paiement", "saisis le reglement", "reglement de la facture", "enregistrer le paiement"), "account.move.action_register_payment"),
        (("cree un avoir", "revers la facture", "note de credit", "transforme la facture en avoir"), "account.move.action_reverse"),
        (("duplique la facture", "copie l'ecriture"), "account.move.action_duplicate"),
        (("imprime la facture", "imprime le pdf"), "account.move.action_print_pdf"),
        (("telecharge la facture", "recupere le pdf de la facture"), "account.move.action_invoice_download_pdf"),
        (("annule le paiement", "annuler le reglement"), "account.payment.action_cancel"),
        (("valide le paiement", "poste le reglement", "approuve le reglement"), "account.payment.action_post"),
        (("cree le paiement", "saisis l'encaissement", "cree l'encaissement", "saisis le reglement"), "account.journal.create_customer_payment"),
        (("releve bancaire", "rapprochement bancaire", "ouvre le rapprochement"), "account.move.open_reconcile_view"),
        (("cree une facture fournisseur", "facture d'achat"), "account.journal.action_create_vendor_bill"),
        (("e-facture", "facture electronique", "factures electroniques"), "account.journal.button_fetch_in_einvoices"),
        (("calcule les taxes", "calcule la tva", "tva sur"), "account.tax.compute_all"),
        (("marque l'opportunite", "comme gagnee", "cloture en succes", "passe la piste en gagne", "action_set_won"), "crm.lead.action_set_won"),
        (("comme perdu", "en echec", "cloture la piste", "action_set_lost"), "crm.lead.action_set_lost"),
        (("convertit le lead", "transforme le prospect", "en opportunite", "convert_opportunity"), "crm.lead.convert_opportunity"),
        
        (("cree un lead", "nouveau prospect", "nouvelle piste", "enregistre une nouvelle piste", "crm.lead.create"), "crm.lead.create"),
        (("planifie une reunion pour le lead", "cale un rendez-vous avec", "organise un appel", "action_schedule_meeting"), "crm.lead.action_schedule_meeting"),
        (("affiche les doublons", "montre les leads en double", "action_show_potential_duplicates"), "crm.lead.action_show_potential_duplicates"),
        (("archive le lead", "met de cote la piste"), "crm.lead.action_archive"),
        (("restaure le lead", "reactive la piste"), "crm.lead.action_restore"),
        (("confirme la commande", "valide le devis et cree les livraisons", "action_confirm"), "sale.order.action_confirm"),
        (("cree un devis", "prepare une commande", "cree une quotation", "sale.order.create"), "sale.order.create"),
        (("envoie le devis", "envoie la quotation", "action_quotation_send"), "sale.order.action_quotation_send"),
        (("annule la commande", "cloture le devis en annule"), "sale.order.action_cancel"),
        (("verrouille la commande", "bloque la modification du devis"), "sale.order.action_lock"),
        (("repasse la commande en brouillon", "remet le devis en draft"), "sale.order.action_draft"),
        (("remise sur le devis", "assistant de remise"), "sale.order.action_open_discount_wizard"),
        (("facture d'acompte", "facture l'avance"), "sale.advance.payment.inv.create_invoices"),
        (("cree une tache", "ajoute une tache", "tache '", "tasking.task.create"), "tasking.task.create"),
        (("assigne la tache", "affecte cette tache", "assigne-la", "assignee.add"), "tasking.assignee.add"),
        (("deplace la tache", "passe la tache au bucket", "bucket.move_task"), "tasking.bucket.move_task"),
        (("cree le projet", "cree un nouveau projet", "project.create"), "tasking.project.create"),
        (("cree la colonne", "cree le bucket", "bucket.create"), "tasking.bucket.create"),
        (("attache le fichier", "joint le fichier", "attachment.upload"), "tasking.attachment.upload"),
        (("ajoute un commentaire", "commentaire sur la tache"), "tasking.comment.create"),
        (("partage le projet", "donne acces au projet"), "tasking.share.project"),
        (("cree une conversation", "nouveau ticket", "conversation.create"), "messaging.conversation.create"),
        (("envoie un message a", "reponds au client", "message.send"), "messaging.message.send"),
        (("resolu le ticket", "passe le ticket en attente", "toggle_status"), "messaging.conversation.toggle_status"),
        (("priorite haute", "baisse la priorite", "toggle_priority"), "messaging.conversation.toggle_priority"),
        (("assigne la conversation", "attribue le ticket", "conversation.assign"), "messaging.conversation.assign"),
        (("reponse type", "modele de reponse", "canned_response.create"), "messaging.canned_response.create"),
        (("cree une regle", "automatisation", "automation_rule.create"), "messaging.automation_rule.create"),
        (("cree une campagne", "campagne de relance", "campaign.create"), "messaging.campaign.create"),
        (("cree un macro", "raccourci d'actions", "macro.create"), "messaging.macro.create"),
        (("envoie un email", "reponds a l'email", "transfere l'email", "envoie un mail", "email.email_submission.set"), "email.email_submission.set"),
        (("piece jointe", "l'attachement", "email.blob.upload"), "email.blob.upload"),
        (("recupere la piece jointe", "telecharge l'attachement", "email.blob.get"), "email.blob.get"),
        # "creer un dossier" is a FILE folder far more often than a mailbox:
        # "cree un nouveau dossier dispo3 et deplacer le fichier dedans" used to
        # force email.mailbox.create into the send, which Gmail cannot run, and
        # the resulting deferred action failed the whole turn.
        (("dossier mail", "dossier dans ma boite", "dossier dans gmail", "dossier dans la boite",
          "nouveau dossier mail", "mailbox.create"), "email.mailbox.create"),
        (("reponse automatique", "message d'absence", "vacation_response.set"), "email.vacation_response.set"),
        (("cree une regle pour classer", "filtre pour les mails", "sieve_script.set"), "email.sieve_script.set"),
        (("recherche les emails", "liste les mails", "email.email.query"), "email.email.query"),
        (("cree un contact", "cree un nouveau contact", "creer un contact", "creer un nouveau contact",
          "cree une fiche contact", "creer une fiche contact", "cree la fiche contact", "creer la fiche",
          "nouveau contact", "nouvelle fiche", "res.partner.create"), "res.partner.create"),
        (("mets a jour la fiche", "modifie l'adresse du contact", "res.partner.write"), "res.partner.write"),
        (("cree la societe", "res.partner.create_company"), "res.partner.create_company"),
        (("fusionne les deux fiches", "doublon"), "res.partner.find_or_create"),
        (("recherche le contact", "affiche la liste des contacts", "res.partner.search_read"), "res.partner.search_read"),
        (("cree une fiche employe", "enregistre le nouvel employe", "hr.employee.create"), "hr.employee.create"),
        (("cree un compte utilisateur", "cree les comptes utilisateurs", "hr.employee.action_create_user"), "hr.employee.action_create_user"),
        (("approuve la note de frais", "valide les frais", "expense.action_approve"), "hr.expense.action_approve"),
        (("cree une note de frais", "expense.create_expense_from_attachments"), "hr.expense.create_expense_from_attachments"),
        (("approuve le conge", "valide la demande de conge", "leave.action_approve"), "hr.leave.action_approve"),
        (("refuse le conge", "leave.action_refuse"), "hr.leave.action_refuse"),
        (("candidat", "transforme le candidat", "create_employee_from_applicant"), "hr.applicant.create_employee_from_applicant"),
        (("cree un evenement", "ajoute une reunion", "event.create"), "event.create"),
        (("modifie la reunion", "modifier la reunion", "modifie la date", "modifier la date", "modifie l'heure", "modifie le lieu", "change la date de", "change la date", "reporter l'evenement", "reporter la reunion", "repousse la reunion", "repousse l'evenement", "decale la reunion", "deplace la reunion", "event.modify"), "event.modify"),
        (("modifie l'evenement", "mets a jour l'evenement", "update the event", "event.update"), "event.update"),
        (("affiche mes disponibilites", "event.free_busy"), "event.free_busy"),
        (("supprime l'evenement", "annule la reunion", "supprime tous les reunions", "supprime toutes les reunions", "supprime tous les evenements", "supprime toutes les evenements", "supprime tout le calendrier", "event.delete"), "event.delete"),
        (("affiche les evenements", "liste mes rendez-vous", "event.query"), "event.query"),
        (("rappels", "alerte 15 minutes", "event.reminder"), "event.reminder"),
        (("rend la reunion", "repete l'evenement", "event.recurrence"), "event.recurrence"),
        (("synchronise mon agenda", "event.sync"), "event.sync"),
        (("cree un calendrier", "cree un agenda", "calendar.create"), "calendar.create"),
        (("affiche mes calendriers", "liste les agendas", "calendar.list"), "calendar.list"),
        (("telecharge le fichier", "recupere le fichier", "depuis le cloud", "file.get"), "file.get"),
        (("upload le fichier", "depose ", "file.put"), "file.put"),
        (("partage ", "cree un lien de partage", "donne acces au dossier", "file.share.create"), "file.share.create"),
        (("deplace ", "renomme ", "file.move"), "file.move"),
        (("copie ", "duplique ", "file.copy"), "file.copy"),
        (("supprime le fichier", "efface ", "file.delete"), "file.delete"),
        (("corbeille", "trashbin"), "file.trashbin.restore"),
        (("vide la corbeille", "trashbin.purge"), "file.trashbin.purge"),
        (("versions du fichier", "historique de ", "file.version.list"), "file.version.list"),
        (("cree le dossier", "cree un nouveau dossier", "file.mkcol"), "file.mkcol"),
        (("convertit ", "en pdf", "office.converter.convert"), "office.converter.convert"),
        (("recupere le contenu de", "via wopi", "ouvre le fichier", "office.wopi.get_file"), "office.wopi.get_file"),
        (("dans l'editeur de documents", "modification directe", "file.direct_editing"), "file.direct_editing"),
        (("insere une image", "ajoute le logo", "insere le logo", "ajoute une image",
          "mets une image", "met une image", "insere la photo", "ajoute la photo",
          "office.editor.insert_image"), "office.editor.insert_image"),
        (("ajoute un tableau", "insere un tableau", "ajoute une grille", "insert un tableau",
          "office.editor.insert_table"), "office.editor.insert_table"),
        (("supprime la photo", "retire la photo", "supprime l'image", "retire l'image",
          "supprime les images", "office.editor.remove_image"), "office.editor.remove_image"),
        (("modifie le contenu", "edite le texte", "change le texte", "modifie le texte",
          "edite le document", "modifie le document", "change le document",
          "office.editor.edit_content"), "office.editor.edit_content"),
        (("met en gras", "mets en gras", "met le texte en gras", "mets le texte en gras",
          "met tous le text en gras", "mets tous le text en gras",
          "met tout le texte en gras", "mets tout le texte en gras",
          "met en italique", "mets en italique",
          "change la couleur", "change la couleur du texte",
          "modifie la couleur", "modifie la couleur du texte",
          "met en bleu", "mets en bleu", "met en rouge", "mets en rouge",
          "formate le texte", "formate le document",
          "met en gras le texte", "mets en gras le texte",
          "office.editor.edit_content"), "office.editor.edit_content"),
        (("ajoute un titre", "ajoute le titre", "ajouter un titre", "ajouter le titre",
          "insere un titre", "insere le titre",
          "titre en gras", "titre au debut", "titre au debut du document",
          "office.editor.add_title"), "office.editor.add_title"),
        (("cree un fichier", "creer un fichier", "cree un nouveau document", "cree un document",
          "creer un document", "cree une presentation", "creer une presentation",
          "cree une presentation", "cree un tableur", "creer un tableur",
          "cree un fichier word", "cree un fichier excel", "cree un fichier powerpoint",
          "cree un nouveau fichier", "creer un nouveau fichier", "cree le document",
          "cree un document docx", "tel qu'un fichier test.docx", "un fichier .docx",
          "office.editor.create"), "office.editor.create"),
        (("ajoute du contenu", "ajoute un paragraphe", "ajoute le texte", "ajouter un paragraphe",
          "ajoute du texte", "ajoute un texte", "ecris un paragraphe", "ecris dans le document",
          "ajoute un mot", "ecris le texte", "ajoute le contenu",
          "office.editor.add_content"), "office.editor.add_content"),
        (("supprime le tableau", "retire le tableau", "supprime la grille", "remove the table",
          "office.editor.remove_table"), "office.editor.remove_table"),
        (("modifie le tableau", "modifie la grille", "change le tableau", "modify the table",
          "modifie les colonnes du tableau", "modifie les lignes du tableau",
          "office.editor.modify_table"), "office.editor.modify_table"),
        (("remplissage du formulaire", "saisie du formulaire", "office.editor.start_filling"), "office.editor.start_filling"),
        (("verrouille ", "prend le verrou", "office.wopi.lock"), "office.wopi.lock"),
        (("enregistre les changements", "sauvegarde les changements", "office.wopi.put_file"), "office.wopi.put_file"),
        (("deverrouille ", "relache le verrou", "office.wopi.unlock"), "office.wopi.unlock"),
        (("erreur de sauvegarde", "office.callback.error"), "office.callback.error"),
        (("genere un document", "par script de generation", "office.docbuilder.generate"), "office.docbuilder.generate"),
        (("importe le document", "ajoute a l'espace", "kb.ingest.document"), "kb.ingest.document"),
        (("recherche les documents similaires", "recherche semantique", "kb.search.vector"), "kb.search.vector"),
        (("recherche le terme", "recherche textuelle", "kb.search.fulltext"), "kb.search.fulltext"),
        (("cree un espace", "cree un nouvel espace", "kb.space.create"), "kb.space.create"),
        (("genere un rapport", "genere une synthese", "kb.generate.report"), "kb.generate.report"),
        (("repends a", "en citant les sources", "reponse sourcee", "kb.answer.grounded"), "kb.answer.grounded"),
        (("exporte le rapport", "telecharge la synthese", "kb.export.pdf"), "kb.export.pdf"),
        (("cree une note", "note de brainstorming", "kb.note.create"), "kb.note.create"),
    )

    def complete(self, prompt: str) -> str:
        intent_match = re.search(r"Demande\s*:\s*(.+)", prompt, flags=re.IGNORECASE | re.DOTALL)
        intent = intent_match.group(1).strip() if intent_match else prompt
        lowered = _fold_accents(intent.lower())

        catalog: list[str] = []
        for line in prompt.splitlines():
            candidate = line.strip().lstrip("-").strip()
            if candidate and (candidate in ACTION_LABELS or _looks_like_action(candidate)):
                if normalize_action(candidate) and candidate not in catalog:
                    catalog.append(candidate)

        if not catalog:
            return json.dumps({"actions": []})

        matched: list[str] = []
        for triggers, action_id in self.TRIGGER_RULES:
            if action_id not in catalog:
                continue
            if any(_fold_accents(trigger) in lowered for trigger in triggers):
                matched.append(action_id)

        if matched:
            return json.dumps({"actions": matched[:2]})

        intent_tokens = _keyword_tokens(intent)
        scored: list[tuple[int, int, str]] = []
        for action_id in catalog:
            tokens = _keyword_tokens(action_id) - self.STOP_TOKENS
            if not tokens:
                tokens = _keyword_tokens(action_id)
            overlap = tokens & intent_tokens
            score = len(overlap)
            if score:
                scored.append((score, -len(action_id), action_id))

        scored.sort(key=lambda item: (-item[0], item[1]))
        best = [item[2] for item in scored[:2]]
        return json.dumps({"actions": best})


def _looks_like_action(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9_]+)+", value))


def coerce_action_completion_client(model_client: Any | None) -> Callable[[str], str]:
    if model_client is None:
        return RuleBasedActionClassifierClient().complete

    if callable(model_client):
        return model_client

    for method_name in ("complete", "generate", "predict", "invoke"):
        method = getattr(model_client, method_name, None)
        if callable(method):
            return method

    raise TypeError(
        "Unsupported model client. Pass a callable or an object exposing complete(), generate(), predict(), or invoke()."
    )


class ActionClassifierAgent:
    def __init__(
        self,
        model_client: Any | None = None,
        domain_client: Any | None = None,
        use_domain_pipeline: bool = True,
    ) -> None:
        self._completion = coerce_action_completion_client(model_client)
        self._domain_agent = DomainClassifierAgent(model_client=domain_client) if use_domain_pipeline else None

    def _build_prompt(self, intent: str, domains: list[str], catalog: list[str]) -> str:
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

    def _filter_to_catalog(self, predicted: list[str], catalog: list[str]) -> list[str]:
        catalog_set = {normalize_action(action_id) for action_id in catalog}
        result: list[str] = []
        for action_id in predicted:
            normalized = normalize_action(action_id)
            if normalized is not None and normalized in catalog_set and normalized not in result:
                result.append(normalized)
        return result

    def _call_completion(self, prompt: str, system_prompt: str | None = None) -> str:
        try:
            return str(self._completion(prompt, system_prompt=system_prompt)).strip()
        except TypeError:
            return str(self._completion(prompt)).strip()

    def _predict_for_domain(self, intent: str, domain: str) -> tuple[list[str], str]:
        catalog = catalog_for_domains([domain])
        if not catalog:
            return [], ""
        prompt = self._build_prompt(intent, [domain], catalog)
        system_prompt = ACTION_SYSTEM_PROMPTS_BY_DOMAIN.get(domain, ACTION_SYSTEM_PROMPTS_BY_DOMAIN["general"])
        raw_response = self._call_completion(prompt, system_prompt)
        predicted_actions = self._filter_to_catalog(extract_actions_from_response(raw_response), catalog)
        if not predicted_actions:
            print(f"  [predict] empty/invalid action list for {domain}, retrying in 2s", flush=True)
            time.sleep(2)
            raw_response = self._call_completion(prompt, system_prompt)
            predicted_actions = self._filter_to_catalog(extract_actions_from_response(raw_response), catalog)
        if not predicted_actions:
            # Deterministic fallback: a trigger rule (e.g. « créer un nouveau
            # contact … » -> res.partner.create) still wins when the LLM keeps
            # returning an empty/invalid action list.
            try:
                rules = RuleBasedActionClassifierClient()
                fallback_raw = rules.complete(prompt)
                fallback = self._filter_to_catalog(extract_actions_from_response(fallback_raw), catalog)
                if fallback:
                    predicted_actions = fallback
                    raw_response = fallback_raw
            except Exception:  # noqa: BLE001 - fallback is best-effort
                pass
        return predicted_actions, raw_response

    def predict(self, intent: str, domains: list[str] | None = None) -> ActionPrediction:
        if domains is None:
            if self._domain_agent is None:
                domains = ["general"]
            else:
                domains = self._domain_agent.predict(intent).predicted_domains

        if not domains:
            return ActionPrediction(
                intent=intent,
                predicted_actions=[],
                domains=domains,
                raw_response="",
            )

        merged: list[str] = []
        raw_parts: list[str] = []
        for domain in domains:
            predicted, raw = self._predict_for_domain(intent, domain)
            raw_parts.append(raw)
            for action_id in predicted:
                if action_id not in merged:
                    merged.append(action_id)

        return ActionPrediction(
            intent=intent,
            predicted_actions=merged,
            domains=domains,
            raw_response="\n".join(part for part in raw_parts if part),
        )


def classify_actions(
    intent: str,
    domains: list[str] | None = None,
    model_client: Any | None = None,
    domain_client: Any | None = None,
) -> ActionPrediction:
    return ActionClassifierAgent(
        model_client=model_client,
        domain_client=domain_client,
        use_domain_pipeline=domains is None,
    ).predict(intent, domains=domains)
