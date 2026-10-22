import { useEffect, useRef, useState, useCallback } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import EmptyState from '../components/EmptyState';
import { WordIcon, ExcelIcon, PdfIcon, SlidesIcon, PlusIcon, InspectorIcon } from '../components/icons';

const CATEGORY_META = {
  word: { label: 'Word', icon: <WordIcon />, exts: ['.docx', '.doc', '.odt', '.rtf', '.txt'] },
  cell: { label: 'Excel', icon: <ExcelIcon />, exts: ['.xlsx', '.xls', '.ods', '.csv'] },
  slide: { label: 'Présentation', icon: <SlidesIcon />, exts: ['.pptx', '.ppt', '.odp'] },
  pdf: { label: 'PDF', icon: <PdfIcon />, exts: ['.pdf'] },
};

let docsApiPromise = null;
function loadDocsApi(dsUrl) {
  if (docsApiPromise) return docsApiPromise;
  const src = `${(dsUrl || 'http://127.0.0.1:8088').replace(/\/$/, '')}/web-apps/apps/api/documents/api.js`;
  docsApiPromise = new Promise((resolve, reject) => {
    if (window.DocsAPI) { resolve(window.DocsAPI); return; }
    const script = document.createElement('script');
    script.src = src;
    script.onload = () => resolve(window.DocsAPI);
    script.onerror = () => { docsApiPromise = null; reject(new Error('Impossible de charger OnlyOffice')); };
    document.body.appendChild(script);
  });
  return docsApiPromise;
}

async function collectAllFiles() {
  const queue = [''];
  const files = [];
  while (queue.length) {
    const path = queue.shift();
    let data;
    try { data = await api.listFiles(path); } catch { continue; }
    const items = data.items || [];
    for (const it of items) {
      if (it.type === 'folder') queue.push(it.path);
      else files.push(it);
    }
  }
  return files;
}

function matchCategory(path, exts) {
  const lower = path.toLowerCase();
  return exts.some((e) => lower.endsWith(e));
}

const OO_ID = 'viito-oo-editor-box';

export default function OnlyOfficePage() {
  const { category = 'word' } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const meta = CATEGORY_META[category] || CATEGORY_META.word;

  const [spaceFiles, setSpaceFiles] = useState([]);
  const [loadingFiles, setLoadingFiles] = useState(true);
  const [dsAlive, setDsAlive] = useState(true);
  const [dsUrl, setDsUrl] = useState('http://127.0.0.1:8088');
  const fileInputRef = useRef(null);

  const [activePath, setActivePath] = useState(null);
  const ooRef = useRef(null);
  const initFileRef = useRef(searchParams.get('file'));

  // Viito multi-turn session state
  const [viitoOpen, setViitoOpen] = useState(searchParams.get('viito') === '1' || !!searchParams.get('viito_session'));
  const [viitoSessionId, setViitoSessionId] = useState(searchParams.get('viito_session'));
  const [viitoMessages, setViitoMessages] = useState([]);
  const [viitoInput, setViitoInput] = useState('');
  const [viitoBusy, setViitoBusy] = useState(false);
  const [viitoAction, setViitoAction] = useState(null);
  const [viitoActionMsgIdx, setViitoActionMsgIdx] = useState(-1);
  const [showViitoInspector, setShowViitoInspector] = useState(false);
  const [viitoPopPos, setViitoPopPos] = useState(null);
  const viitoInspectorRef = useRef(null);
  const viitoMessagesEndRef = useRef(null);
  const viitoFileInputRef = useRef(null);
  const [pendingAttachments, setPendingAttachments] = useState([]);

  // The inspector popup is rendered in a fixed overlay (the message list scrolls
  // and would clip it), so its position is computed from the trigger button and
  // kept inside the viewport, below the topbar or above the button as needed.
  const toggleViitoInspector = (event, index, msg) => {
    const wasOpen = showViitoInspector && viitoActionMsgIdx === index;
    setViitoAction(msg.meta || { outcome: 'finished', domains: [], actions: [] });
    setViitoActionMsgIdx(index);
    setShowViitoInspector(!wasOpen);
    if (wasOpen) {
      setViitoPopPos(null);
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const width = 330;
    const estHeight = 300;
    const margin = 12;
    const left = Math.min(Math.max(margin, rect.left - 12), window.innerWidth - width - margin);
    let top = rect.bottom + 8;
    if (top + estHeight > window.innerHeight - margin) {
      top = Math.max(72, rect.top - estHeight - 8);
    }
    setViitoPopPos({ top, left, width });
  };

  const closeViitoInspector = useCallback(() => {
    setShowViitoInspector(false);
    setViitoPopPos(null);
  }, []);

  useEffect(() => {
    if (!showViitoInspector) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') closeViitoInspector(); };
    window.addEventListener('keydown', onKey);
    window.addEventListener('resize', closeViitoInspector);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', closeViitoInspector);
    };
  }, [showViitoInspector, closeViitoInspector]);

  const matchedFiles = spaceFiles.filter((f) => matchCategory(f.path, meta.exts));

  const destroyOo = useCallback(() => {
    if (ooRef.current) {
      try { ooRef.current.destroy(); } catch (_) {}
      ooRef.current = null;
    }
    const el = document.getElementById(OO_ID);
    if (el) el.innerHTML = '';
  }, []);

  const createOo = useCallback(async (path, url) => {
    destroyOo();
    const el = document.getElementById(OO_ID);
    if (el) el.innerHTML = '';
    try {
      const ds = await loadDocsApi(url);
      const { config, token } = await api.onlyofficeConfig(path, category);
      ooRef.current = new ds.DocEditor(OO_ID, { ...config, token });
    } catch (e) {
      alert('Erreur OnlyOffice: ' + (e.message || e));
    }
  }, [category, destroyOo]);

  useEffect(() => () => destroyOo(), [destroyOo]);

  // Inspector click-outside handler
  useEffect(() => {
    if (!showViitoInspector) return;
    function handleClick(e) {
      if (!e.target.closest('.oo-viito-inspector-anchor')) {
        setShowViitoInspector(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showViitoInspector]);

  // Auto-scroll chat to bottom
  useEffect(() => {
    viitoMessagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [viitoMessages]);

  // Load DocServer status and files
  useEffect(() => {
    api.onlyofficeStatus()
      .then((s) => { setDsAlive(!!s.alive); if (s.url) setDsUrl(s.url); })
      .catch(() => setDsAlive(false));
    collectAllFiles().then((f) => { setSpaceFiles(f); setLoadingFiles(false); });
  }, []);

  // Load session from URL params on mount
  useEffect(() => {
    const fileParam = initFileRef.current;
    const sessionParam = searchParams.get('viito_session');
    initFileRef.current = null;
    if (fileParam) {
      setActivePath(fileParam);
      setSearchParams({ file: fileParam, ...(sessionParam ? { viito_session: sessionParam } : {}) }, { replace: true });
    }
  }, [dsUrl]);

  // Load existing session if viito_session in URL
  useEffect(() => {
    if (viitoSessionId) {
      api.onlyofficeGetViitoSession(viitoSessionId)
        .then((s) => {
          const msgs = (s.messages || []).map((m) => ({
            ...m,
            content: m.role === 'assistant' ? (m.content || '').replace(/^\[MODIFY\]\s*/i, '') : m.content,
          }));
          setViitoMessages(msgs);
        })
        .catch(() => setViitoSessionId(null));
    }
  }, [viitoSessionId]);

  // Create editor when path changes
  useEffect(() => {
    if (activePath && dsUrl) {
      createOo(activePath, dsUrl);
    }
  }, [activePath, dsUrl, createOo]);

  // Create or load Viito session when opening file
  useEffect(() => {
    if (!activePath) return;
    const existingSession = searchParams.get('viito_session');
    if (existingSession) {
      setViitoSessionId(existingSession);
      return;
    }
    // Create new session
    api.onlyofficeCreateViitoSession(activePath)
      .then((s) => {
        setViitoSessionId(s.id);
        setViitoMessages([]);
        const next = new URLSearchParams(searchParams);
        next.set('file', activePath);
        next.set('viito_session', s.id);
        setSearchParams(next, { replace: true });
      })
      .catch(() => {});
  }, [activePath]);

  const toggleViitoPanel = (open) => {
    setViitoOpen(open);
    const next = new URLSearchParams(searchParams);
    if (open) next.set('viito', '1');
    else if (next.get('viito')) next.delete('viito');
    setSearchParams(next, { replace: true });
  };

  const openEditor = async (path) => {
    destroyOo();
    setActivePath(path);
    setViitoMessages([]);
    setViitoSessionId(null);
    setViitoAction(null);
    setViitoOpen(true);
    setViitoInput('');
  };

  const goBack = () => {
    destroyOo();
    setActivePath(null);
    setSearchParams({}, { replace: true });
    setViitoOpen(false);
    setViitoSessionId(null);
    setViitoMessages([]);
  };

  const handleUpload = async (e) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    try {
      await api.uploadFilesToPath(files, '');
      e.target.value = '';
      collectAllFiles().then((fs) => setSpaceFiles(fs));
    } catch (err) { alert(err.message); }
  };

  const sendViitoMessage = async () => {
    if (!viitoInput.trim() || !viitoSessionId || viitoBusy) return;
    const userMsg = viitoInput.trim();
    setViitoInput('');
    setViitoBusy(true);
    setViitoAction(null);
    const attachmentsToSend = [...pendingAttachments];
    setPendingAttachments([]);

    // Add user message to UI immediately
    const userMessage = { role: 'user', content: userMsg, timestamp: new Date().toISOString() };
    setViitoMessages((prev) => [...prev, userMessage]);

    try {
      const res = await api.onlyofficeSendViitoMessage(viitoSessionId, userMsg, attachmentsToSend.length > 0 ? attachmentsToSend : null);
      const rawContent = res.content || '';
      const displayContent = rawContent.replace(/^\[MODIFY\]\s*/i, '');
      const assistantMessage = {
        role: 'assistant',
        content: displayContent,
        timestamp: new Date().toISOString(),
        modified: res.modified,
        meta: res.meta || null,
      };
      setViitoMessages((prev) => [...prev, assistantMessage]);

      if (res.modified) {
        setViitoAction(res.meta || {
          domains: ['general'],
          actions: [{ action: 'file_edit', status: 'success', params: { file: activePath } }],
          outcome: 'finished',
        });
        await createOo(activePath, dsUrl);
      }
    } catch (err) {
      setViitoMessages((prev) => [...prev, {
        role: 'assistant',
        content: 'Erreur: ' + err.message,
        timestamp: new Date().toISOString(),
        isError: true,
      }]);
    } finally {
      setViitoBusy(false);
    }
  };

  const handleFileAttach = async (e) => {
    const files = e.target.files;
    if (!files || !viitoSessionId) return;
    for (const file of files) {
      try {
        const b64 = await new Promise((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result.split(',')[1]);
          reader.readAsDataURL(file);
        });
        setPendingAttachments((prev) => [...prev, { filename: file.name, content_type: file.type, content_b64: b64 }]);
        setViitoMessages((prev) => [...prev, {
          role: 'user',
          content: `[Pièce jointe: ${file.name}]`,
          timestamp: new Date().toISOString(),
          isAttachment: true,
        }]);
      } catch (err) {
        console.error('Attach error:', err);
      }
    }
    e.target.value = '';
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendViitoMessage();
    }
  };

  return (
    <div className="onlyoffice-layout">
      {!dsAlive && (
        <div className="oo-alert">Le serveur OnlyOffice n'est pas joignable. Vérifiez le conteneur Docker (port 8088).</div>
      )}

      <div className="oo-pane" style={{ display: activePath ? 'none' : undefined }}>
        <div className="oo-toolbar">
          <div className="oo-toolbar-left">
            <span className="oo-title">{meta.icon} Fichiers {meta.label}</span>
          </div>
          <div className="oo-toolbar-right">
            <button className="btn btn-primary btn-sm" onClick={() => fileInputRef.current?.click()}>
              <PlusIcon />
              <span style={{ marginLeft: 6 }}>Importer un fichier</span>
            </button>
            <input ref={fileInputRef} type="file" multiple hidden onChange={handleUpload} />
          </div>
        </div>
        <div className="oo-section-title">Vos fichiers dans l'espace</div>
        {loadingFiles ? (
          <div className="loading-text">Chargement des fichiers...</div>
        ) : matchedFiles.length === 0 ? (
          <EmptyState icon={meta.icon} title={`Aucun fichier ${meta.label}`} text="Importez un fichier ou choisissez-en un dans l'espace pour l'ouvrir avec OnlyOffice." />
        ) : (
          <div className="files-grid">
            {matchedFiles.map((item) => (
              <div key={item.id + (item.folder || '')} className="files-item" onClick={() => openEditor(item.path)}>
                <div className="files-item-icon">{meta.icon}</div>
                <div className="files-item-info">
                  <div className="files-item-name">{item.name}</div>
                  <div className="files-item-meta">
                    {item.folder ? `${item.folder} · ` : ''}{(item.size || 0) < 1024 ? `${item.size || 0} B` : `${((item.size || 0) / 1024).toFixed(1)} KB`}
                  </div>
                </div>
                <div className="oo-open-badge">Ouvrir</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="oo-pane" style={{ display: activePath ? undefined : 'none' }}>
        <div className="onlyoffice-editor-wrap">
          <div className="oo-editor-toolbar">
            <button className="btn btn-ghost btn-sm" onClick={goBack}>
              <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16" style={{ marginRight: 4 }}>
                <path d="M12 4l-6 6 6 6" />
              </svg>
              Retour
            </button>
            <span className="oo-editor-filename">{activePath}</span>
            <div className="oo-editor-actions">
              <button
                className={'btn btn-sm ' + (viitoOpen ? 'btn-primary' : 'btn-ghost')}
                onClick={() => toggleViitoPanel(!viitoOpen)}
                title="Demander à Viito de modifier ce fichier"
              >
                Demander Viito
              </button>
            </div>
          </div>
          <div className={'oo-editor-body' + (viitoOpen ? ' with-panel' : '')}>
            <div className="oo-editor-container">
              <div id={OO_ID} style={{ width: '100%', height: '100%', minHeight: 500 }}></div>
            </div>
            {viitoOpen && (
              <div className="oo-viito-panel">
                <div className="oo-viito-header">
                  <strong>Viito</strong>
                  <button className="btn btn-ghost btn-sm" onClick={() => toggleViitoPanel(false)}>&#x2715;</button>
                </div>
                <div className="oo-viito-messages">
                  {viitoMessages.length === 0 && (
                    <div className="oo-viito-empty">
                      Décrivez la modification à apporter au fichier <em>{activePath ? activePath.split('/').pop() : ''}</em>.
                    </div>
                  )}
                  {viitoMessages.map((msg, i) => (
                    <div key={i} className={`oo-viito-msg ${msg.role} ${msg.isError ? 'error' : ''} ${msg.isAttachment ? 'attachment' : ''}`}>
                      {msg.role === 'user' && !msg.isAttachment && (
                        <div className="oo-viito-msg-label">Vous</div>
                      )}
                      {msg.role === 'assistant' && (
                        <div className="oo-viito-msg-label">
                          Viito
                          {msg.modified && (
                            <span className="oo-viito-modified-badge">Modifié</span>
                          )}
                          <span className="oo-viito-inspector-anchor">
                            <button
                              className="btn btn-ghost btn-xs oo-viito-inspector-inline"
                              onClick={(e) => toggleViitoInspector(e, i, msg)}
                              title="Voir les détails"
                            >
                              <InspectorIcon />
                            </button>
                          </span>
                        </div>
                      )}
                      <div className="oo-viito-msg-content">{msg.content}</div>
                    </div>
                  ))}
                  {viitoBusy && (
                    <div className="oo-viito-msg assistant thinking">
                      <div className="oo-viito-msg-label">Viito</div>
                      <div className="oo-viito-msg-content oo-viito-thinking">
                        <span></span><span></span><span></span>
                      </div>
                    </div>
                  )}
                  <div ref={viitoMessagesEndRef} />
                </div>
                <div className="oo-viito-input-area">
                  <input
                    ref={viitoFileInputRef}
                    type="file"
                    multiple
                    hidden
                    onChange={handleFileAttach}
                  />
                  <button
                    className="btn btn-ghost btn-sm oo-viito-attach-btn"
                    onClick={() => viitoFileInputRef.current?.click()}
                    title="Joindre un fichier"
                  >
                    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="16" height="16">
                      <path d="M14.5 10l-5.5 5.5a3.5 3.5 0 01-5-5l5.5-5.5a2 2 0 013 2.5L7.5 13a.5.5 0 01-1-.5l4-4" />
                    </svg>
                  </button>
                  <textarea
                    className="form-input oo-viito-textarea"
                    placeholder="Décrivez la modification..."
                    value={viitoInput}
                    onChange={(e) => setViitoInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    rows={2}
                    disabled={viitoBusy}
                  />
                  <button
                    className="btn btn-primary btn-sm oo-viito-send-btn"
                    onClick={sendViitoMessage}
                    disabled={viitoBusy || !viitoInput.trim()}
                  >
                    {viitoBusy ? '...' : 'Envoyer'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {showViitoInspector && viitoPopPos && viitoAction && (
        <>
          <div className="oo-viito-inspector-backdrop" onClick={closeViitoInspector} />
          <div
            className="oo-viito-inspector-popup"
            style={{ top: viitoPopPos.top, left: viitoPopPos.left, width: viitoPopPos.width }}
          >
            <div className="inspector-header">
              <span className="inspector-title">Détails du traitement</span>
              <span className={`inspector-badge ${viitoAction.outcome === 'failed' ? 'failed' : viitoAction.outcome === 'escalated' ? 'warn' : 'ok'}`}>
                {viitoAction.outcome === 'failed' ? 'Échoué' : viitoAction.outcome === 'escalated' ? 'Escaladé' : 'Terminé'}
              </span>
            </div>
            {viitoAction.domains?.length > 0 && (
              <div className="inspector-section">
                <div className="inspector-label">Domaines</div>
                <div className="inspector-chips">
                  {viitoAction.domains.map((d, j) => (
                    <span key={j} className="inspector-chip">{d}</span>
                  ))}
                </div>
              </div>
            )}
            {viitoAction.actions?.length > 0 && (
              <div className="inspector-section">
                <div className="inspector-label">Actions exécutées</div>
                {viitoAction.actions.map((a, j) => (
                  <div key={j} className="inspector-action">
                    <div className="inspector-action-head">
                      <span className="inspector-action-name">{a.action}</span>
                      <span className={`inspector-action-status ${a.status}`}>{a.status}</span>
                    </div>
                    {a.params && Object.keys(a.params).length > 0 && (
                      <div className="inspector-params">
                        {Object.entries(a.params).map(([k, v]) => (
                          <div key={k} className="inspector-param">
                            <span className="param-key">{k}</span>
                            <span className="param-val">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
