import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { BackIcon, OdooIcon, CloseIcon } from './icons';

const MODEL_LABELS = {
  contacts: 'Contact',
  crm: 'Opportunité',
  accounting: 'Facture',
  sales: 'Vente / Devis',
  meetings: 'Réunion',
  'hr-employees': 'Employé',
  'hr-leaves': 'Demande de congé',
};

const FIELD_LABELS = {
  name: 'Nom',
  email: 'Email',
  email_from: 'Email',
  work_email: 'Email professionnel',
  phone: 'Téléphone',
  work_phone: 'Téléphone professionnel',
  mobile_phone: 'Téléphone mobile',
  company_type: 'Type',
  is_company: 'Est une société',
  street: 'Rue',
  street2: 'Rue (2)',
  city: 'Ville',
  zip: 'Code postal',
  state_id: 'État / Province',
  country_id: 'Pays',
  vat: 'N° TVA',
  website: 'Site web',
  partner_name: 'Société',
  contact_name: 'Contact',
  expected_revenue: 'Revenu attendu',
  probability: 'Probabilité',
  priority: 'Priorité',
  stage_id: 'Étape',
  source_id: 'Source',
  team_id: 'Équipe',
  date_deadline: 'Date limite',
  date_open: 'Date d\'ouverture',
  tag_ids: 'Tags',
  description: 'Description',
  employee_id: 'Employé',
  holiday_status_id: 'Type de congé',
  date_from: 'Date de début',
  date_to: 'Date de fin',
  request_date_from: 'Date de début (demande)',
  request_date_to: 'Date de fin (demande)',
  number_of_days: 'Nombre de jours',
  number_of_hours: 'Nombre d\'heures',
  state: 'Statut',
  private_name: 'Nom interne',
  notes: 'Notes',
  department_id: 'Département',
  parent_id: 'Manager',
  job_title: 'Poste',
  work_location_id: 'Lieu de travail',
  birthday: 'Date de naissance',
  wage: 'Salaire',
  marital: 'Situation familiale',
  work_permit_expiration_date: 'Expiration permis de travail',
  move_type: 'Type de facture',
  invoice_date: 'Date de facture',
  invoice_date_due: 'Date d\'échéance',
  amount_untaxed: 'Montant HT',
  amount_tax: 'TVA',
  amount_total: 'Montant total',
  amount_residual: 'Solde dû',
  currency_id: 'Devise',
  narration: 'Notes internes',
  payment_state: 'État du paiement',
  date_order: 'Date de commande',
  invoice_status: 'Statut facturation',
  validity_date: 'Date de validité',
  origin: 'Origine',
  note: 'Notes',
  payment_term_id: 'Conditions de paiement',
  client_order_ref: 'Réf. client',
  user_id: 'Responsable',
  start: 'Début',
  stop: 'Fin',
  location: 'Lieu',
  partner_ids: 'Participants',
  allday: 'Journée entière',
  duration: 'Durée (h)',
  active: 'Actif',
  privacy: 'Confidentialité',
  recurrence_id: 'Récurrence',
  recurrency: 'Récurrent',
  videocall_source: 'Source visio',
  videocall_location: 'Lien visio',
};

function formatValue(key, value) {
  if (value === null || value === undefined || value === '') return '—';
  if (key === 'is_company' || key === 'allday' || key === 'active' || key === 'recurrency') return value ? 'Oui' : 'Non';
  if (key === 'start' || key === 'stop') return String(value);
  if (key === 'amount_total' || key === 'amount_untaxed' || key === 'amount_tax' || key === 'amount_residual' || key === 'expected_revenue' || key === 'wage') {
    const num = Number(value);
    if (!isNaN(num)) return num.toLocaleString('fr-FR', { style: 'currency', currency: 'EUR' });
    return String(value);
  }
  if (key === 'probability') {
    const num = Number(value);
    if (!isNaN(num)) return `${num}%`;
    return String(value);
  }
  if (key === 'videocall_source') return { discuss: 'Visioconférence', custom: 'Personnalisé' }[value] || String(value);
  if (key === 'videocall_location') {
    return (
      <a href={value} target="_blank" rel="noopener noreferrer" className="odoo-meeting-link">
        {value}
      </a>
    );
  }
  return String(value);
}

function toDatetimeLocal(value) {
  if (!value) return '';
  const m = String(value).match(/^(\d{2})\/(\d{2})\/(\d{4})(?: (\d{2}):(\d{2}))?/);
  if (m) {
    const [, d, mo, y, hh, mm] = m;
    return `${y}-${mo}-${d}T${hh || '00'}:${mm || '00'}`;
  }
  const iso = String(value).match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (iso) {
    const [, y, mo, d, hh, mm] = iso;
    return `${y}-${mo}-${d}T${hh || '00'}:${mm || '00'}`;
  }
  return '';
}

function DetailField({ label, value }) {
  return (
    <div className="odoo-detail-field">
      <div className="odoo-detail-label">{label}</div>
      <div className="odoo-detail-value">{value}</div>
    </div>
  );
}

export default function RecordDetailPanel({ modelKey, recordId, onClose }) {
  const navigate = useNavigate();
  const [record, setRecord] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [inviting, setInviting] = useState(false);
  const [inviteMsg, setInviteMsg] = useState('');
  const [copied, setCopied] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editMsg, setEditMsg] = useState('');
  const [editForm, setEditForm] = useState({ name: '', location: '', description: '', start: '', stop: '' });

  useEffect(() => {
    if (recordId == null) return;
    setLoading(true);
    setError('');
    setRecord(null);
    api.odooDetail(modelKey, Number(recordId))
      .then((res) => setRecord(res.record))
      .catch((e) => setError(e.message || 'Erreur inconnue'))
      .finally(() => setLoading(false));
  }, [modelKey, recordId]);

  if (recordId == null) {
    return (
      <div className="gmail-detail-empty">
      
        <p>Sélectionnez un enregistrement pour voir son détail</p>
      </div>
    );
  }

  if (loading) return <div className="gmail-detail-empty"><div className="loading-text">Chargement...</div></div>;

  if (error) {
    return (
      <div className="gmail-detail-empty">
        <OdooIcon />
        <p>{error}</p>
      </div>
    );
  }

  if (!record) {
    return <div className="gmail-detail-empty"><p>Enregistrement introuvable</p></div>;
  }

  const isMeeting = modelKey === 'meetings';
  const hiddenForMeetings = isMeeting ? ['partner_ids'] : [];
  const fieldsToShow = Object.keys(FIELD_LABELS).filter(
    (k) => record[k] !== undefined && record[k] !== null && record[k] !== '' && !hiddenForMeetings.includes(k)
  );

  const copyLink = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  const sendInvite = async () => {
    setInviting(true);
    setInviteMsg('');
    try {
      const res = await api.odooMeetingInvite(Number(recordId));
      setInviteMsg(`Invitation envoyée à : ${res.to.join(', ')}`);
    } catch (e) {
      setInviteMsg(e.message || "Échec de l'envoi");
    } finally {
      setInviting(false);
    }
  };

  const openEdit = () => {
    setEditForm({
      name: record.title || '',
      location: record.location || '',
      description: record.description || '',
      start: toDatetimeLocal(record.start),
      stop: toDatetimeLocal(record.stop),
    });
    setEditMsg('');
    setEditOpen(true);
  };

  const saveEdit = async () => {
    if (!editForm.name.trim()) {
      setEditMsg("Le nom de la réunion est requis.");
      return;
    }
    setSavingEdit(true);
    setEditMsg('');
    const payload = {
      name: editForm.name.trim(),
      location: editForm.location.trim(),
      description: editForm.description,
    };
    if (editForm.start) payload.start = editForm.start;
    if (editForm.stop) payload.stop = editForm.stop;
    try {
      await api.odooMeetingEdit(Number(recordId), payload);
      setEditMsg('Réunion modifiée.');
      setEditOpen(false);
      setLoading(true);
      setError('');
      api.odooDetail(modelKey, Number(recordId))
        .then((res) => setRecord(res.record))
        .catch((e) => setError(e.message || 'Erreur inconnue'))
        .finally(() => setLoading(false));
    } catch (e) {
      setEditMsg(e.message || "Échec de la modification");
    } finally {
      setSavingEdit(false);
    }
  };

  const label = MODEL_LABELS[modelKey] || modelKey;

  return (
    <div className="gmail-detail odoo-detail-panel">
      <div className="gmail-detail-topbar">
        {onClose && (
          <button className="btn btn-ghost btn-sm" onClick={onClose} title="Fermer le détail">
            <CloseIcon />
          </button>
        )}
        <div className="gmail-detail-actions">
          <span className="odoo-detail-tag tool-tag">Odoo</span>
        </div>
      </div>

      <div className="odoo-detail-scroll">
        <div className="odoo-detail-top-v2">
          <div className="odoo-detail-avatar">{label.charAt(0)}</div>
          <div>
            <h2 className="odoo-detail-title">{record.title || `#${recordId}`}</h2>
            <div className="odoo-detail-subtitle">{label}</div>
          </div>
        </div>

        <div className="odoo-detail-fields">
          {fieldsToShow.map((k) => (
            <DetailField key={k} label={FIELD_LABELS[k]} value={formatValue(k, record[k])} />
          ))}
        </div>

        {isMeeting && editOpen && (
          <div className="odoo-meeting-block">
            <div className="odoo-meeting-title">Modifier la réunion</div>
            <label className="odoo-meeting-field">
              <span>Nom</span>
              <input type="text" value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} />
            </label>
            <label className="odoo-meeting-field">
              <span>Lieu</span>
              <input type="text" value={editForm.location} onChange={(e) => setEditForm({ ...editForm, location: e.target.value })} />
            </label>
            <label className="odoo-meeting-field">
              <span>Description</span>
              <textarea rows={3} value={editForm.description} onChange={(e) => setEditForm({ ...editForm, description: e.target.value })} />
            </label>
            <div className="odoo-meeting-dates">
              <label className="odoo-meeting-field">
                <span>Début</span>
                <input type="datetime-local" value={editForm.start} onChange={(e) => setEditForm({ ...editForm, start: e.target.value })} />
              </label>
              <label className="odoo-meeting-field">
                <span>Fin</span>
                <input type="datetime-local" value={editForm.stop} onChange={(e) => setEditForm({ ...editForm, stop: e.target.value })} />
              </label>
            </div>
            <div className="odoo-meeting-actions">
              <button className="btn btn-primary btn-sm" onClick={saveEdit} disabled={savingEdit}>
                {savingEdit ? 'Enregistrement...' : 'Enregistrer'}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => setEditOpen(false)}>Annuler</button>
            </div>
            {editMsg && <div className="odoo-meeting-invite-msg">{editMsg}</div>}
          </div>
        )}

        {isMeeting && record.attendees?.length > 0 && (
          <div className="odoo-meeting-block">
            <div className="odoo-meeting-title">Participants</div>
            <div className="odoo-meeting-attendees">
              {record.attendees.map((a) => (
                <div key={a.id || a.email || a.name} className="odoo-meeting-attendee">
                  <span className="odoo-meeting-attendee-name">{a.name || a.email || '—'}</span>
                  {a.email && <span className="odoo-meeting-attendee-email">{a.email}</span>}
                </div>
              ))}
            </div>
          </div>
        )}

        {isMeeting && record.meeting_link && (
          <div className="odoo-meeting-block">
            <div className="odoo-meeting-title">Lien de la réunion</div>
            <div className="odoo-meeting-link-row">
              <a className="odoo-meeting-link" href={record.meeting_link} target="_blank" rel="noopener noreferrer">
                {record.meeting_link}
              </a>
              <button className="btn btn-ghost btn-sm" onClick={() => copyLink(record.meeting_link)} title="Copier le lien">
                {copied ? 'Copié' : 'Copier'}
              </button>
            </div>
            <button className="btn btn-primary btn-sm" onClick={sendInvite} disabled={inviting} style={{ alignSelf: 'flex-start' }}>
              {inviting ? 'Envoi...' : 'Envoyer le lien aux participants'}
            </button>
            {inviteMsg && <div className="odoo-meeting-invite-msg">{inviteMsg}</div>}
          </div>
        )}

        <div className="odoo-detail-actions">
          {isMeeting && (
            <button className="btn btn-ghost" onClick={openEdit}>Modifier la réunion</button>
          )}
          {record.open_url && (
            <a className="btn btn-ghost" href={record.open_url} target="_blank" rel="noopener noreferrer">
              Ouvrir dans Odoo
            </a>
          )}
        </div>
      </div>
    </div>
  );
}