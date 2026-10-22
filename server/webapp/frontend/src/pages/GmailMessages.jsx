import { useEffect, useState, useMemo } from 'react';
import { api } from '../api/client';
import EmptyState from '../components/EmptyState';

const LABEL_CONFIG = {
  inbox: { label: 'INBOX', emptyTitle: 'Aucun message reçu', emptyText: 'Connectez votre compte Gmail pour voir vos messages reçus.' },
  sent: { label: 'SENT', emptyTitle: 'Aucun message envoyé', emptyText: 'Les messages envoyés apparaîtront ici.' },
  drafts: { label: 'DRAFTS', emptyTitle: 'Aucun brouillon', emptyText: 'Vos brouillons Gmail apparaîtront ici.' },
  trash: { label: 'TRASH', emptyTitle: 'Corbeille vide', emptyText: 'Aucun message supprimé pour l\'instant.' },
};

function getInitial(name) {
  if (!name) return '?';
  return name.charAt(0).toUpperCase();
}

function getAvatarColor(name) {
  const colors = ['#e91e63', '#9c27b0', '#673ab7', '#3f51b5', '#2196f3', '#009688', '#4caf50', '#ff9800', '#ff5722'];
  let hash = 0;
  for (let i = 0; i < (name || '').length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

function formatDate(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  const now = new Date();
  const diff = now - d;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'à l\'instant';
  if (mins < 60) return `${mins}min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}j`;
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
}

function formatSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function msgTimestamp(msg) {
  if (msg.timestamp) return Number(msg.timestamp) || 0;
  if (msg.date) return new Date(msg.date).getTime() || 0;
  return 0;
}

function groupByThread(messages) {
  const threads = {};
  for (const msg of messages) {
    const tid = msg.thread_id || msg.id;
    const ts = msgTimestamp(msg);
    if (!threads[tid]) {
      threads[tid] = { thread_id: tid, messages: [], subject: msg.subject, unread: false, latest: msg, latestTs: ts };
    }
    threads[tid].messages.push(msg);
    if (msg.unread) threads[tid].unread = true;
    if (ts >= threads[tid].latestTs) {
      threads[tid].latest = msg;
      threads[tid].latestTs = ts;
    }
  }
  return Object.values(threads).sort((a, b) => b.latestTs - a.latestTs);
}

function ThreadList({ threads, selectedId, onSelect }) {
  return (
    <div className="gmail-list">
      {threads.map((thread) => {
        const latest = thread.latest;
        const count = thread.messages.length;
        return (
          <div
            key={thread.thread_id}
            className={`gmail-list-item ${selectedId === thread.thread_id ? 'selected' : ''} ${thread.unread ? 'unread' : ''}`}
            onClick={() => onSelect(thread)}
          >
            <div className="gmail-list-avatar" style={{ background: getAvatarColor(latest.from_name) }}>
              {getInitial(latest.from_name)}
            </div>
            <div className="gmail-list-content">
              <div className="gmail-list-header">
                <span className="gmail-list-sender">{latest.from_name || 'Inconnu'}</span>
                <span className="gmail-list-date">{formatDate(latest.date)}</span>
              </div>
              <div className="gmail-list-subject">
                {latest.subject || '(sans objet)'}
                {count > 1 && <span className="gmail-thread-count">{count}</span>}
              </div>
              <div className="gmail-list-snippet">{latest.snippet || ''}</div>
            </div>
            {thread.unread && <div className="gmail-unread-dot"></div>}
          </div>
        );
      })}
    </div>
  );
}

function ThreadDetail({ thread, onClose, onDiscuss }) {
  const [expanded, setExpanded] = useState({});

  if (!thread) {
    return (
      <div className="gmail-detail-empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" width="64" height="64">
          <rect x="3" y="5" width="18" height="14" rx="2" />
          <path d="M3 7l9 6 9-6" />
        </svg>
        <p>Sélectionnez un message pour le lire</p>
      </div>
    );
  }

  const messages = thread.messages || [];
  const latest = messages.reduce((acc, m) => (msgTimestamp(m) >= msgTimestamp(acc) ? m : acc), messages[0]) || {};

  const toggleExpand = (idx) => {
    setExpanded((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  return (
    <div className="gmail-detail">
      <div className="gmail-detail-topbar">
        <button className="btn btn-ghost btn-sm" onClick={onClose} title="Retour à la liste">
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16">
            <path d="M12 4l-6 6 6 6" />
          </svg>
        </button>
        <div className="gmail-detail-actions">
    
        </div>
      </div>

      <div className="gmail-detail-scroll">
        <h2 className="gmail-detail-subject">{latest.subject || '(sans objet)'}</h2>

        {messages.map((msg, idx) => {
          const isLast = idx === messages.length - 1;
          const isExpanded = expanded[idx] || isLast || messages.length <= 2;

          return (
            <div key={msg.id} className={`gmail-thread-message ${isExpanded ? 'expanded' : 'collapsed'}`}>
              <div className="gmail-thread-header" onClick={() => toggleExpand(idx)}>
                <div className="gmail-thread-avatar" style={{ background: getAvatarColor(msg.from_name) }}>
                  {getInitial(msg.from_name)}
                </div>
                <div className="gmail-thread-sender-info">
                  <div className="gmail-thread-sender-name">{msg.from_name || 'Inconnu'}</div>
                  <div className="gmail-thread-sender-email">à {msg.to || 'moi'}</div>
                </div>
                <div className="gmail-thread-date">
                  {msg.date ? new Date(msg.date).toLocaleString('fr-FR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''}
                </div>
                {!isExpanded && <div className="gmail-thread-snippet-inline">{msg.snippet || ''}</div>}
                {msg.attachments && msg.attachments.length > 0 && !isExpanded && (
                  <svg className="gmail-thread-attach-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="14" height="14">
                    <path d="M14.5 5.5l-7 7a2.83 2.83 0 004 4l7-7a4.24 4.24 0 00-6-6l-7 7a5.66 5.66 0 008 8" />
                  </svg>
                )}
              </div>

              {isExpanded && (
                <div className="gmail-thread-body">
                  <div className="gmail-detail-to">
                    <strong>À :</strong> {msg.to || ''}
                    {msg.cc ? <> | <strong>Cc :</strong> {msg.cc}</> : null}
                  </div>

                  <div className="gmail-detail-body">
                    {msg.body_html ? (
                      <div dangerouslySetInnerHTML={{ __html: msg.body_html }} className="gmail-html-body" />
                    ) : (
                      <pre className="gmail-text-body">{msg.body || msg.snippet || 'Aucun contenu disponible.'}</pre>
                    )}
                  </div>

                  {msg.attachments && msg.attachments.length > 0 && (
                    <div className="gmail-attachments">
                      <div className="gmail-attachments-title">
                        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="14" height="14">
                          <path d="M14.5 5.5l-7 7a2.83 2.83 0 004 4l7-7a4.24 4.24 0 00-6-6l-7 7a5.66 5.66 0 008 8" />
                        </svg>
                        {msg.attachments.length} pièce(s) jointe(s)
                      </div>
                      {msg.attachments.map((att, i) => (
                        <div key={i} className="gmail-attachment-chip">
                          <div className="gmail-attachment-icon">
                            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="16" height="16">
                              <path d="M14 3l3 3-10 10H4v-3L14 3z" />
                            </svg>
                          </div>
                          <div className="gmail-attachment-info">
                            <span className="gmail-attachment-name">{att.filename}</span>
                            <span className="gmail-attachment-meta">{att.mimeType} {att.size && att.size !== '0' ? `· ${formatSize(Number(att.size))}` : ''}</span>
                          </div>
                          {att.attachmentId && (
                            <button
                              className="btn btn-ghost btn-sm gmail-attachment-download"
                              onClick={() => api.gmailDownloadAttachment(msg.id, att.attachmentId, att.filename)}
                              title={`Télécharger ${att.filename}`}
                            >
                              <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="16" height="16">
                                <path d="M10 3v10m0 0l-3-3m3 3l3-3M4 14v2a1 1 0 001 1h10a1 1 0 001-1v-2" />
                              </svg>
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function GmailMessages({ type = 'inbox' }) {
  const config = LABEL_CONFIG[type] || LABEL_CONFIG.inbox;
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedThread, setSelectedThread] = useState(null);
  const [connected, setConnected] = useState(false);

  const threads = useMemo(() => groupByThread(messages), [messages]);

  const fetchMessages = () => {
    setLoading(true);
    api.gmailStatus()
      .then((s) => {
        setConnected(s.connected);
        if (!s.connected) {
          setLoading(false);
          return;
        }
        if (type === 'drafts') {
          return api.gmailDrafts().then((d) => setMessages(d.drafts || []));
        }
        return api.gmailMessages(config.label).then((d) => setMessages(d.emails || []));
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchMessages(); }, [type]);

  const handleSelect = (thread) => {
    setSelectedThread(thread);
    if (thread.thread_id) {
      api.gmailThread(thread.thread_id)
        .then((detail) => setSelectedThread(detail))
        .catch(() => {});
    }
  };

  const handleDiscuss = (message) => {
    const context = `Email de ${message.from_name || message.from} — "${message.subject}"\n\n${message.snippet || ''}`;
    api.createConversation({ context_item: context })
      .then((conv) => { window.location.href = `/conversations/${conv.id}`; })
      .catch(() => {});
  };

  if (!connected) {
    return (
      <div className="gmail-page">
        <EmptyState
          icon={<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="3" y="5" width="14" height="10" rx="1.5" /><path d="M3 6.5l7 4 7-4" /></svg>}
          title="Compte non connecté"
          text="Connectez votre compte Gmail dans la page Configuration pour voir vos messages."
        />
      </div>
    );
  }

  if (loading) {
    return <div className="gmail-page"><div className="loading-text">Chargement...</div></div>;
  }

  if (error) {
    return <div className="gmail-page"><div className="form-error">{error}</div></div>;
  }

  return (
    <div className="gmail-split">
      <ThreadList
        threads={threads}
        selectedId={selectedThread?.thread_id || selectedThread?.id}
        onSelect={handleSelect}
      />
      <ThreadDetail
        thread={selectedThread}
        onClose={() => setSelectedThread(null)}
        onDiscuss={handleDiscuss}
      />
    </div>
  );
}
