import { useEffect, useState, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import ListRow from '../components/ListRow';
import EmptyState from '../components/EmptyState';
import RecordDetailPanel from '../components/RecordDetailPanel';
import { OdooIcon } from '../components/icons';

const TABS = [
  { key: 'conges', label: 'Congés', modelKey: 'hr-leaves' },
  { key: 'employes', label: 'Employés', modelKey: 'hr-employees' },
];

export default function OdooRhPage() {
  const navigate = useNavigate();
  const { tab, id } = useParams();
  const activeTab = TABS.find((t) => t.key === tab)?.key || 'conges';
  const activeModel = TABS.find((t) => t.key === activeTab)?.modelKey || 'hr-leaves';
  const selectedId = id ? Number(id) : null;

  const [records, setRecords] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');

  const fetchData = useCallback(async (q = '') => {
    setLoading(true);
    setError('');
    try {
      const res = await api.odooList(activeModel, q);
      setRecords(res.records || []);
    } catch (e) {
      setError(e.message || 'Erreur inconnue');
      setRecords([]);
    } finally {
      setLoading(false);
    }
  }, [activeModel]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleSearch = (e) => {
    e.preventDefault();
    fetchData(search);
  };

  const discussItem = async (item) => {
    const context = {
      title: item.title,
      tool: 'Odoo',
      domain: activeModel,
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
    navigate(`/odoo/rh/${activeTab}/${recordId}`, { replace: false });
  };

  const clearSelection = () => navigate(`/odoo/rh/${activeTab}`);

  const emptyTitle = activeTab === 'conges' ? 'Aucun congé' : 'Aucun employé';
  const emptyText = activeTab === 'conges' ? 'Aucune demande de congé trouvée.' : 'Aucun employé trouvé dans Odoo.';

  return (
    <div className="gmail-split odoo-split odoo-split-rh">
      <div className="vk-list-pane">
        <div className="odoo-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`odoo-tab ${activeTab === t.key ? 'active' : ''}`}
              onClick={() => navigate(`/odoo/rh/${t.key}`)}
            >
              {t.label}
            </button>
          ))}
        </div>

        <form className="odoo-search" onSubmit={handleSearch}>
          <input
            type="text"
            className="odoo-search-input"
            placeholder={`Rechercher dans ${TABS.find((t) => t.key === activeTab)?.label.toLowerCase()}...`}
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
            title={emptyTitle}
            text={emptyText}
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

      <RecordDetailPanel modelKey={activeModel} recordId={selectedId} onClose={clearSelection} />
    </div>
  );
}