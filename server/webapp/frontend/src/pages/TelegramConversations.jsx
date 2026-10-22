import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { EmptyChatIcon, SendIcon, TelegramIcon } from '../components/icons';

function formatDate(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return '';
  return d.toLocaleString('fr-FR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export default function TelegramConversations() {
  const [conversations, setConversations] = useState([]);
  const [selected, setSelected] = useState(null);
  const [messages, setMessages] = useState([]);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const scrollRef = useRef(null);

  const loadList = useCallback(() => {
    api.telegramStatus()
      .then((s) => {
        setConnected(s.connected);
        if (!s.connected) {
          setLoading(false);
          return;
        }
        return api.telegramConversations().then((d) => setConversations(d.conversations || []));
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadList(); }, [loadList]);

  const openConversation = (chatId) => {
    setSelected(chatId);
    setMessages([]);
    api.telegramConversationMessages(chatId)
      .then((d) => setMessages(d.messages || []))
      .catch(() => setMessages([]));
  };

  const send = async () => {
    const value = text.trim();
    if (!value || !selected || sending) return;
    setSending(true);
    setText('');
    const optimistic = { direction: 'out', text: value, from_name: 'Moi' };
    setMessages((prev) => [...prev, optimistic]);
    try {
      await api.telegramSendToChat(selected, value);
      const d = await api.telegramConversationMessages(selected);
      setMessages(d.messages || []);
      loadList();
    } catch (e) {
      setMessages((prev) => [
        ...prev.slice(0, -1),
        { direction: 'out', text: value, from_name: 'Moi' },
        { direction: 'in', text: `Échec de l'envoi : ${e.message}`, from_name: 'Système' },
      ]);
    } finally {
      setSending(false);
    }
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, selected]);

  const activeConv = conversations.find((c) => c.chat_id === selected);

  if (!connected) {
    return (
      <div className="gmail-page">
        <div className="gmail-config">
          <h2>Conversations Telegram</h2>
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
    <div className="conv-layout">
      <div className="conv-list">
        <div className="conv-list-header">
          <h3>Discussions</h3>
          <span className="conv-count">{conversations.length}</span>
        </div>
        <button className="btn btn-secondary btn-sm" style={{ margin: '10px 16px' }} onClick={loadList}>
          Rafraîchir
        </button>
        {conversations.length === 0 && (
          <div style={{ padding: '14px 18px', fontSize: 12, color: 'var(--muted)' }}>
            Aucune conversation pour l'instant. Cliquez sur « Rafraîchir » pour charger vos discussions Telegram.
          </div>
        )}
        {conversations.map((c) => (
          <button
            key={c.chat_id}
            className={`conv-item${c.chat_id === selected ? ' active' : ''}`}
            onClick={() => openConversation(c.chat_id)}
          >
            <div className="conv-item-top">
              <span className="conv-item-name">{c.title}</span>
              <span className="conv-item-time">{formatDate(c.last_date)}</span>
            </div>
            {c.type === 'private' && c.phone && <div className="conv-item-phone">{c.phone}</div>}
            <div className="conv-item-snippet">{c.last_message || ''}</div>
            {c.messages > 0 && <span className="conv-count" style={{ float: 'right' }}>{c.messages}</span>}
          </button>
        ))}
      </div>

      <div className="conv-thread">
        {!selected ? (
          <div className="thread-empty">
            <TelegramIcon />
            <p><strong>Sélectionne une discussion à gauche</strong></p>
            <p>Vos conversations Telegram apparaissent ici après un rafraîchissement.</p>
          </div>
        ) : (
          <>
            <div className="thread-header">
              <span className="thread-header-name">{activeConv ? activeConv.title : 'Conversation'}</span>
              {activeConv && activeConv.type === 'private' && activeConv.phone && (
                <span className="thread-header-phone">{activeConv.phone}</span>
              )}
            </div>
            <div className="thread-messages" ref={scrollRef}>
              {messages.length === 0 ? (
                <div style={{ alignSelf: 'center' }}>
                  <EmptyChatIcon />
                </div>
              ) : (
                messages.map((m, i) => (
                  <div key={i} className={m.direction === 'out' ? 'msg-wrapper user' : 'msg-wrapper'}>
                    <div className={m.direction === 'out' ? 'msg user' : 'msg contact'}>
                      {m.text}
                      {m.date && <div className="tg-msg-date">{formatDate(m.date)}</div>}
                    </div>
                  </div>
                ))
              )}
            </div>
            <div className="composer">
              <textarea
                rows="1"
                placeholder="Répondre sur Telegram..."
                value={text}
                disabled={sending}
                onChange={(e) => { setText(e.target.value); }}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
                style={{ flex: 1, border: 'none', outline: 'none', resize: 'none', fontFamily: 'inherit', fontSize: 13.5, background: 'transparent', padding: '10px 4px' }}
              />
              <button className="icon-btn send" onClick={send} disabled={!text.trim() || sending} title="Envoyer">
                <SendIcon />
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}