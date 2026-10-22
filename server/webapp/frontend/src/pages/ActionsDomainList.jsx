import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { DOMAINS } from '../constants';
import ListRow from '../components/ListRow';
import EmptyState from '../components/EmptyState';
import { BackIcon, BoltIcon } from '../components/icons';

export default function ActionsDomainList() {
  const { domain } = useParams();
  const navigate = useNavigate();
  const [items, setItems] = useState(null);
  const [err, setErr] = useState(false);
  const meta = DOMAINS.find((d) => d.key === domain);

  useEffect(() => {
    setItems(null);
    setErr(false);
    api
      .actionsByDomain(domain)
      .then((rows) => setItems(rows || []))
      .catch(() => {
        setItems([]);
        setErr(true);
      });
  }, [domain]);

  const discussItem = async (item) => {
    const context = {
      title: item.title,
      tool: item.tool,
      domain,
      action_id: item.id,
      source_thread_id: item.source_thread_id,
    };
    try {
      const conv = await api.createConversation(context);
      navigate(`/conversations/${conv.id}`);
    } catch {
      navigate(`/conversations/local-${Date.now().toString(36)}`);
    }
  };

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div className="domain-header">
        <Link to="/actions" className="back-link">
          <BackIcon />
          Toutes les catégories
        </Link>
      </div>
      {items === null ? (
        <div className="loading">Chargement...</div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={<BoltIcon />}
          title={meta ? `Rien de créé dans ${meta.label}` : 'Rien de créé pour l\'instant'}
          text={
            err
              ? 'Backend hors ligne — impossible de charger ce domaine.'
              : 'Les éléments créés par Viito dans ce domaine apparaîtront ici.'
          }
        />
      ) : (
        <div className="list-scroll">
          {items.map((it) => (
            <ListRow key={it.id} item={it} onDiscuss={discussItem} />
          ))}
        </div>
      )}
    </div>
  );
}
