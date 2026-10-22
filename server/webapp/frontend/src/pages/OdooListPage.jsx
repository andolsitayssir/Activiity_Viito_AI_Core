import { useEffect, useState, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import ListRow from '../components/ListRow';
import EmptyState from '../components/EmptyState';
import RecordDetailPanel from '../components/RecordDetailPanel';
import { OdooIcon } from '../components/icons';

const MODEL_KEYS = {
  contacts: 'contacts',
  crm: 'crm',
  accounting: 'accounting',
  sales: 'sales',
  meetings: 'meetings',
};

const LABELS = {
  contacts: { title: 'Contacts', emptyTitle: 'Aucun contact', emptyText: 'Aucun contact trouvé dans Odoo.' },
  crm: { title: 'CRM — Leads & Opportunités', emptyTitle: 'Aucune opportunité', emptyText: 'Aucune opportunité CRM trouvée.' },
  accounting: { title: 'Factures', emptyTitle: 'Aucune facture', emptyText: 'Aucune facture trouvée dans Odoo.' },
  sales: { title: 'Ventes / Devis', emptyTitle: 'Aucune vente', emptyText: 'Aucun devis ou vente trouvé dans Odoo.' },
  meetings: { title: 'Réunions', emptyTitle: 'Aucune réunion', emptyText: 'Aucune réunion trouvée dans Odoo.' },
};

export default function OdooListPage({ modelKey }) {
  const navigate = useNavigate();
  const { id } = useParams();
  const selectedId = id ? Number(id) : null;
  const [records, setRecords] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');

  const config = LABELS[modelKey] || { title: modelKey, emptyTitle: 'Aucun résultat', emptyText: 'Aucun enregistrement.' };

  const fetchData = useCallback(async (q = '') => {
    setLoading(true);
    setError('');
    try {
      const res = await api.odooList(MODEL_KEYS[modelKey] || modelKey, q);
      setRecords(res.records || []);
    } catch (e) {
      setError(e.message || 'Erreur inconnue');
      setRecords([]);
    } finally {
      setLoading(false);
    }
  }, [modelKey]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleSearch = (e) => {
    e.preventDefault();
    fetchData(search);
  };

  const discussItem = async (item) => {
    const context = {
      title: item.title,
      tool: 'Odoo',
      domain: modelKey,
      record_id: item.id,
    };
    try {
      const conv = await api.createConversation(context);
      navigate(`/conversations/${conv.id}`);
    } catch {
      navigate(`/conversations/local-${Date.now().toString(36)}`);
    }
  };

  const selectRecord = (recordId) => {
    navigate(`/odoo/${modelKey}/${recordId}`, { replace: false });
  };

  const clearSelection = () => navigate(`/odoo/${modelKey}`);

  return (
    <div className="gmail-split odoo-split">
      <div className="vk-list-pane">
        <form className="odoo-search" onSubmit={handleSearch}>
          <input
            type="text"
            className="odoo-search-input"
            placeholder={`Rechercher dans ${config.title.toLowerCase()}...`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button type="submit" className="btn btn-primary btn-sm">Rechercher</button>
        </form>

        {loading ? (
          <div className="loading-text">Chargement...</div>
        ) : error ? (
          <EmptyState
            icon={<OdooIcon />}
            title="Odoo injoignable"
            text={error}
          />
        ) : records.length === 0 ? (
          <EmptyState
            icon={<OdooIcon />}
            title={config.emptyTitle}
            text={config.emptyText}
          />
        ) : (
          <div className="list-scroll vk-list-scroll">
            {records.map((rec) => (
              <div
                key={rec.id}
                className={`odoo-row ${rec.id === selectedId ? 'selected' : ''}`}
                onClick={() => selectRecord(rec.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === 'Enter' && selectRecord(rec.id)}
              >
                <ListRow item={rec} onDiscuss={discussItem} />
              </div>
            ))}
          </div>
        )}
      </div>

      <RecordDetailPanel modelKey={modelKey} recordId={selectedId} onClose={clearSelection} />
    </div>
  );
}