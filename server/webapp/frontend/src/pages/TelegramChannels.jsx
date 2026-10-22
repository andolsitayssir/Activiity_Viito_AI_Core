import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { SentIcon, TelegramIcon } from '../components/icons';

function formatDate(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return '';
  return d.toLocaleString('fr-FR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export default function TelegramChannels() {
  const [channels, setChannels] = useState([]);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(null);
  const [messages, setMessages] = useState({});
  const [text, setText] = useState({});
  const [sending, setSending] = useState(false);

  const loadList = () => {
    api.telegramStatus()
      .then((s) => {
        setConnected(s.connected);
        if (!s.connected) return '';
        return '';
      })
      .then(() => api.telegramChannels())
      .then((d) => setChannels(d.channels || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { loadList(); }, []);

  const toggleChannel = async (channelId) => {
    const next = expanded === channelId ? null : channelId;
    setExpanded(next);
    if (next) {
      const d = await api.telegramConversationMessages(channelId);
      setMessages((prev) => ({ ...prev, [channelId]: d.messages || [] }));
    }
  };

  const send = async (channelId) => {
    const value = (text[channelId] || '').trim();
    if (!value || sending) return;
    setSending(true);
    const optimistic = { direction: 'out', text: value, from_name: 'Moi' };
    setMessages((prev) => ({ ...prev, [channelId]: [...(prev[channelId] || []), optimistic] }));
    setText((prev) => ({ ...prev, [channelId]: '' }));
    try {
      await api.telegramSendToChannel(channelId, value);
      const d = await api.telegramConversationMessages(channelId);
      setMessages((prev) => ({ ...prev, [channelId]: d.messages || [] }));
    } catch (e) {
      setMessages((prev) => ({
        ...prev,
        [channelId]: [...(prev[channelId] || []).slice(0, -1), { direction: 'in', text: `Échec : ${e.message}`, from_name: 'Système' }],
      }));
    } finally {
      setSending(false);
    }
  };

  if (!connected) {
    return (
      <div className="gmail-page">
        <div className="gmail-config">
          <h2>Canaux Telegram</h2>
          <p style={{ color: 'var(--muted)', fontSize: 13 }}>
            Connectez d'abord votre compte Telegram dans la page <a href="/telegram/config">Configuration</a>.
          </p>
        </div>
      </div>
    );
  }

  if (loading) return <div className="loading">Chargement...</div>;
  if (error) return <div className="gmail-page"><div className="form-error">{error}</div></div>;

  return (
    <div className="gmail-page" style={{ padding: '18px 28px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>Canaux broadcast</h2>
        <button className="btn btn-secondary btn-sm" onClick={loadList}>Rafraîchir</button>
      </div>

      {channels.length === 0 && (
        <div className="thread-empty">
          <TelegramIcon />
          <p><strong>Aucun canal pour l'instant</strong></p>
          <p>Vos canaux Telegram apparaissent ici après un rafraîchissement.</p>
        </div>
      )}

      {channels.map((c) => (
        <div key={c.chat_id} className="gmail-list-item" onClick={() => toggleChannel(c.chat_id)} style={{ cursor: 'pointer' }}>
          <div className="gmail-list-avatar" style={{ background: '#229ED9' }}>
            <SentIcon />
          </div>
          <div className="gmail-list-content">
            <div className="gmail-list-header">
              <span className="gmail-list-sender">{c.title}</span>
              <span className="gmail-list-date">{formatDate(c.last_date)}</span>
            </div>
            <div className="gmail-list-subject">
              {c.username ? `@${c.username}` : `id ${c.chat_id}`}
              {c.messages > 0 && <span className="gmail-thread-count">{c.messages}</span>}
            </div>
            <div className="gmail-list-snippet">{c.last_message || ''}</div>
          </div>
        </div>
      ))}

      {expanded && (() => {
        const channel = channels.find((c) => c.chat_id === expanded);
        if (!channel) return null;
        return (
          <div style={{ marginTop: 16, border: '1px solid var(--border)', borderRadius: 10, padding: 14 }}>
            <div className="gmail-list-header" style={{ marginBottom: 10 }}>
              <span className="gmail-list-sender">Publication dans {channel.title}</span>
            </div>
            <div style={{ maxHeight: 260, overflowY: 'auto', marginBottom: 12 }}>
              {(messages[expanded] || []).map((m, i) => (
                <div key={i} style={{ marginBottom: 8, lineHeight: 1.4 }}>
                  <span style={{ fontWeight: 600, fontSize: 12, color: 'var(--muted-2)' }}>{m.from_name}</span>{' '}
                  <span style={{ fontSize: 13.5 }}>{m.text}</span>
                  {m.date && <div style={{ fontSize: 11, color: 'var(--muted-2)' }}>{formatDate(m.date)}</div>}
                </div>
              ))}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                className="form-input"
                style={{ flex: 1 }}
                placeholder={`Publier dans ${channel.title}...`}
                value={text[expanded] || ''}
                onChange={(e) => setText((prev) => ({ ...prev, [expanded]: e.target.value }))}
                onKeyDown={(e) => { if (e.key === 'Enter') send(expanded); }}
              />
              <button className="btn btn-primary" onClick={() => send(expanded)} disabled={sending || !(text[expanded] || '').trim()}>
                Publier
              </button>
            </div>
          </div>
        );
      })()}
    </div>
  );
}