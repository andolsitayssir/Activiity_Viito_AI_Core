import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import Composer from '../components/Composer';
import ToolTag from '../components/ToolTag';
import MessageBubble from '../components/MessageBubble';
import { PlusIcon, EmptyChatIcon, TrashIcon } from '../components/icons';

function formatListTime(iso) {
  if (!iso) return '';
  const t = new Date(iso);
  if (isNaN(t)) return '';
  const now = Date.now();
  const diff = now - t.getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return "à l'instant";
  if (min < 60) return `${min} min`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} h`;
  const day = Math.floor(hr / 24);
  if (day === 1) return 'Hier';
  return `${day} j`;
}

export default function ConversationsPage() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const [conversations, setConversations] = useState([]);
  const [loadErr, setLoadErr] = useState(false);
  const [selected, setSelected] = useState(null);
  const [messages, setMessages] = useState([]);
  const [sending, setSending] = useState(false);
  const scrollRef = useRef(null);

  const loadList = useCallback(() => {
    api
      .listConversations()
      .then((list) => {
        setConversations(list);
        setLoadErr(false);
      })
      .catch(() => setLoadErr(true));
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  const openConversation = useCallback(
    (id) => {
      setSelected(id);
      setMessages([]);
      navigate(`/conversations/${id}`, { replace: true });
      api
        .getConversation(id)
        .then((data) => setMessages(data.messages || []))
        .catch(() => setMessages([]));
    },
    [navigate]
  );

  // Auto-open from URL when navigating directly
  useEffect(() => {
    if (conversationId && (selected === null || selected !== conversationId)) {
      openConversation(conversationId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  const newConversation = async () => {
    try {
      const conv = await api.createConversation(null);
      loadList();
      openConversation(conv.id);
    } catch {
      // offline: still open a local blank thread so UI is usable
      const id = 'local-' + Date.now().toString(36);
      navigate(`/conversations/${id}`, { replace: true });
      setSelected(id);
      setMessages([]);
    }
  };

  const startFromContext = async (context) => {
    try {
      const conv = await api.createConversation(context);
      loadList();
      openConversation(conv.id);
    } catch {
      const id = 'local-' + Date.now().toString(36);
      navigate(`/conversations/${id}`, { replace: true });
      setSelected(id);
      setMessages([{ role: 'context', content: `À propos de : ${context?.title || ''}` }]);
    }
  };

  const deleteConversation = async (e, id) => {
    e.stopPropagation();
    if (!window.confirm('Supprimer cette conversation ?')) return;
    try {
      await api.deleteConversation(id);
      if (selected === id) {
        setSelected(null);
        setMessages([]);
        navigate('/conversations', { replace: true });
      }
      loadList();
    } catch {}
  };

  const sendMessage = async (text, attachments = null) => {
    if (!selected || sending) return;
    setSending(true);
    const fileNote =
      attachments && attachments.length > 0
        ? ` [${attachments.map((a) => a.filename).join(', ')}]`
        : '';
    const optimisticUser = { role: 'user', content: text + fileNote };
    const pendingIdx = messages.length;
    const nextMessages = [...messages, optimisticUser, { role: 'assistant', content: 'Viito réfléchit...', pending: true }];
    setMessages(nextMessages);

    try {
      const reply = await api.sendMessage(selected, text, attachments);
      setMessages((prev) =>
        prev.map((m, i) => (i === pendingIdx + 1 ? reply : m))
      );
    } catch (err) {
      const fallback = {
        role: 'assistant',
        content:
          err instanceof ApiError && err.status === 0
            ? 'Backend injoignable — la demande n\'a pas pu être envoyée.'
            : (err.message || 'Erreur lors du traitement.'),
        meta: { outcome: 'failed' },
      };
      setMessages((prev) => prev.map((m, i) => (i === pendingIdx + 1 ? fallback : m)));
    } finally {
      setSending(false);
      loadList();
    }
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, selected]);

  const activeConv = conversations.find((c) => c.id === selected);

  return (
    <div className="conv-layout">
      <div className="conv-list">
        {loadErr && (
          <div style={{ padding: '14px 18px', fontSize: 12, color: 'var(--muted)' }}>
            Backend hors ligne — les discussions ne peuvent pas être chargées.
          </div>
        )}
        <button className="new-conv-fab" onClick={newConversation}>
          <PlusIcon />
          Nouvelle conversation
        </button>
        <div className="conv-list-header">
          <h3>Discussions</h3>
          <span className="conv-count">{conversations.length}</span>
        </div>
        {conversations.map((c) => (
          <button
            key={c.id}
            className={`conv-item${c.id === selected ? ' active' : ''}`}
            onClick={() => openConversation(c.id)}
          >
            <div className="conv-item-top">
              <span className="conv-item-name">{c.title || 'Conversation'}</span>
              <span className="conv-item-time">{formatListTime(c.time)}</span>
            </div>
            <div className="conv-item-tags">
              <ToolTag tool={c.tool} />
            </div>
            <div className="conv-item-snippet">{c.last_message || ''}</div>
            <button
              className="conv-item-delete"
              onClick={(e) => deleteConversation(e, c.id)}
              title="Supprimer"
            >
              <TrashIcon />
            </button>
          </button>
        ))}
      </div>

      <div className="conv-thread">
        {!selected ? (
          <div className="thread-empty">
            <EmptyChatIcon />
            <p><strong>Sélectionne une discussion à gauche</strong></p>
            <p>ou démarre une nouvelle conversation avec Viito</p>
          </div>
        ) : (
          <>
            <div className="thread-header">
              <span className="thread-header-name">{activeConv ? activeConv.title : 'Conversation'}</span>
              <ToolTag tool={activeConv ? activeConv.tool : 'Viito'} />
            </div>
            <div className="thread-messages" ref={scrollRef}>
              {messages.length === 0 ? (
                <div style={{ alignSelf: 'center' }}>
                  <EmptyChatIcon />
                </div>
              ) : (
                messages.map((m, i) => <MessageBubble key={i} msg={m} />)
              )}
            </div>
            <Composer onSend={sendMessage} disabled={sending} />
          </>
        )}
      </div>
    </div>
  );
}
