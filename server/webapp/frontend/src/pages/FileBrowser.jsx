import { useEffect, useState, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import EmptyState from '../components/EmptyState';

function formatSize(bytes) {
  if (!bytes || bytes === 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatDate(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function getFileIcon(type, name) {
  if (type === 'folder') {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" width="22" height="22" style={{ color: '#f59e0b' }}>
        <path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
      </svg>
    );
  }
  const ext = (name || '').split('.').pop()?.toLowerCase() || '';
  const colors = { pdf: '#ef4444', doc: '#3b82f6', docx: '#3b82f6', xls: '#22c55e', xlsx: '#22c55e', ppt: '#f97316', pptx: '#f97316', txt: '#6b7280', csv: '#22c55e', json: '#8b5cf6', zip: '#a855f7', gz: '#a855f7', png: '#ec4899', jpg: '#ec4899', jpeg: '#ec4899', gif: '#ec4899', svg: '#ec4899', mp3: '#06b6d4', mp4: '#06b6d4' };
  const color = colors[ext] || '#6b7280';
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" width="22" height="22" style={{ color }}>
      <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
      <path d="M14 2v6h6" />
    </svg>
  );
}

const OO_EXTS = {
  word: ['docx', 'doc', 'odt', 'rtf', 'txt'],
  cell: ['xlsx', 'xls', 'ods', 'csv'],
  slide: ['pptx', 'ppt', 'odp'],
  pdf: ['pdf'],
};

function getOnlyOfficeCategory(name) {
  const ext = (name || '').split('.').pop()?.toLowerCase() || '';
  for (const [cat, exts] of Object.entries(OO_EXTS)) {
    if (exts.includes(ext)) return cat;
  }
  return null;
}

export default function FileBrowser() {
  const navigate = useNavigate();
  const [items, setItems] = useState([]);
  const [currentPath, setCurrentPath] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [selected, setSelected] = useState(null);
  const [contextMenu, setContextMenu] = useState(null);
  const [moveTarget, setMoveTarget] = useState(null); // item to move
  const [moveDestPath, setMoveDestPath] = useState(''); // destination folder path
  const [moveFolders, setMoveFolders] = useState([]); // folders in current move destination
  const [moveLoading, setMoveLoading] = useState(false);
  const [shareTarget, setShareTarget] = useState(null); // item to share
  const [shareEmails, setShareEmails] = useState('');
  const [shareMessage, setShareMessage] = useState('');
  const [shareSending, setShareSending] = useState(false);
  const [shareError, setShareError] = useState('');
  const [renameTarget, setRenameTarget] = useState(null); // item to rename
  const [renameName, setRenameName] = useState('');
  const [renameBusy, setRenameBusy] = useState(false);
  const fileInputRef = useRef(null);

  const fetchFiles = (path = '') => {
    setLoading(true);
    setError('');
    api.listFiles(path)
      .then((d) => setItems(d.items || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchFiles(currentPath); }, [currentPath]);

  const handleUpload = async (e) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    try {
      await api.uploadFilesToPath(files, currentPath);
      fetchFiles(currentPath);
    } catch (err) {
      setError(err.message);
    }
    e.target.value = '';
  };

  const handleCreateFolder = () => {
    if (!newFolderName.trim()) return;
    api.createFolder(newFolderName.trim(), currentPath)
      .then(() => { setShowNewFolder(false); setNewFolderName(''); fetchFiles(currentPath); })
      .catch((e) => setError(e.message));
  };

  const handleDelete = (item) => {
    if (!confirm(`Supprimer "${item.name}" ?`)) return;
    api.deleteFile(item.path)
      .then(() => { setSelected(null); fetchFiles(currentPath); })
      .catch((e) => setError(e.message));
  };

  const handleDownload = (item) => {
    api.downloadFile(item.path, item.name);
  };

  const handleOpen = (item) => {
    const BASE = window.location.origin.replace(/\/$/, '');
    window.open(`${BASE}/api/files/download/${encodeURIComponent(item.path)}`, '_blank');
  };

  const handleRenameOpen = (item) => {
    setRenameTarget(item);
    setRenameName(item.name);
    setRenameBusy(false);
  };

  const handleRenameConfirm = () => {
    if (!renameTarget) return;
    const name = renameName.trim();
    if (!name || name === renameTarget.name) { setRenameTarget(null); return; }
    setRenameBusy(true);
    api.renameFile(renameTarget.path, name)
      .then(() => { setRenameTarget(null); fetchFiles(currentPath); })
      .catch((e) => { setRenameBusy(false); setError(e.message); });
  };

  const handleMoveOpen = (item) => {
    setMoveTarget(item);
    setMoveDestPath('');
    setMoveLoading(true);
    api.listFiles('')
      .then((d) => {
        const folders = (d.items || []).filter(i => i.type === 'folder');
        setMoveFolders(folders);
        setMoveLoading(false);
      })
      .catch(() => { setMoveFolders([]); setMoveLoading(false); });
  };

  const handleMoveNavigate = (folderPath) => {
    setMoveDestPath(folderPath);
    setMoveLoading(true);
    api.listFiles(folderPath)
      .then((d) => {
        const folders = (d.items || []).filter(i => i.type === 'folder');
        setMoveFolders(folders);
        setMoveLoading(false);
      })
      .catch(() => { setMoveFolders([]); setMoveLoading(false); });
  };

  const handleMoveConfirm = () => {
    if (!moveTarget) return;
    const dest = moveDestPath || '';
    setMoveLoading(true);
    api.moveFile(moveTarget.path, dest ? `${dest}/${moveTarget.name}` : moveTarget.name)
      .then(() => {
        setMoveTarget(null);
        setMoveDestPath('');
        fetchFiles(currentPath);
      })
      .catch((e) => { setError(e.message); setMoveLoading(false); });
  };

  const handleShareOpen = (item) => {
    setShareTarget(item);
    setShareEmails('');
    setShareMessage('');
    setShareError('');
  };

  const handleShareConfirm = () => {
    if (!shareTarget) return;
    const emails = shareEmails.split(/[,\s;]+/).map((e) => e.trim()).filter(Boolean);
    if (emails.length === 0) {
      setShareError('Veuillez saisir au moins une adresse email.');
      return;
    }
    setShareSending(true);
    setShareError('');
    api.shareFile(shareTarget.path, emails, shareMessage)
      .then(() => { setShareTarget(null); setShareSending(false); })
      .catch((e) => { setShareError(e.message); setShareSending(false); });
  };

  const handleClick = (item) => {
    if (item.type === 'folder') {
      setCurrentPath(item.path);
      setSelected(null);
    } else {
      setSelected(item);
    }
  };

  const handleBack = () => {
    const parts = currentPath.split('/');
    parts.pop();
    setCurrentPath(parts.join('/'));
    setSelected(null);
  };

  const pathParts = currentPath ? currentPath.split('/').filter(Boolean) : [];

  return (
    <div className="files-layout">
      <div className="files-toolbar">
        <div className="files-toolbar-left">
          {currentPath && (
            <button className="btn btn-ghost btn-sm" onClick={handleBack}>
              <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16">
                <path d="M12 4l-6 6 6 6" />
              </svg>
            </button>
          )}
          <div className="files-breadcrumb">
            <span className="files-breadcrumb-item" onClick={() => setCurrentPath('')}>Mes fichiers</span>
            {pathParts.map((part, i) => (
              <span key={i}>
                <span className="files-breadcrumb-sep">/</span>
                <span
                  className="files-breadcrumb-item"
                  onClick={() => setCurrentPath(pathParts.slice(0, i + 1).join('/'))}
                >
                  {part}
                </span>
              </span>
            ))}
          </div>
        </div>
        <div className="files-toolbar-right">
          <button className="btn btn-ghost btn-sm" onClick={() => setShowNewFolder(true)} title="Nouveau dossier">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="18" height="18">
              <path d="M3 7v10a2 2 0 002 2h10a2 2 0 002-2V9a2 2 0 00-2-2h-4l-2-2H5a2 2 0 00-2 2z" />
              <path d="M10 11v4m-2-2h4" />
            </svg>
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => fileInputRef.current?.click()}>
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="16" height="16" style={{ marginRight: 6 }}>
              <path d="M10 3v10m0 0l-3-3m3 3l3-3M4 14v2a1 1 0 001 1h10a1 1 0 001-1v-2" />
            </svg>
            Upload
          </button>
          <input ref={fileInputRef} type="file" multiple hidden onChange={handleUpload} />
        </div>
      </div>

      {showNewFolder && (
        <div className="files-new-folder">
          <input
            className="form-input"
            placeholder="Nom du dossier"
            value={newFolderName}
            onChange={(e) => setNewFolderName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleCreateFolder()}
            autoFocus
          />
          <button className="btn btn-primary btn-sm" onClick={handleCreateFolder}>Créer</button>
          <button className="btn btn-ghost btn-sm" onClick={() => { setShowNewFolder(false); setNewFolderName(''); }}>Annuler</button>
        </div>
      )}

      {moveTarget && (
        <div className="files-modal-overlay" onClick={() => setMoveTarget(null)}>
          <div className="files-modal" onClick={(e) => e.stopPropagation()}>
            <div className="files-modal-header">
              <h3>Déplacer "{moveTarget.name}"</h3>
              <button className="btn btn-ghost btn-sm" onClick={() => setMoveTarget(null)}>✕</button>
            </div>
            <div className="files-modal-body">
              <div className="files-modal-breadcrumb">
                <span className="files-breadcrumb-item" onClick={() => handleMoveNavigate('')}>Racine</span>
                {moveDestPath && moveDestPath.split('/').filter(Boolean).map((part, i, arr) => (
                  <span key={i}>
                    <span className="files-breadcrumb-sep">/</span>
                    <span
                      className="files-breadcrumb-item"
                      onClick={() => handleMoveNavigate(arr.slice(0, i + 1).join('/'))}
                    >
                      {part}
                    </span>
                  </span>
                ))}
              </div>
              {moveLoading ? (
                <div className="loading-text">Chargement...</div>
              ) : moveFolders.length === 0 ? (
                <div className="files-modal-empty">Aucun sous-dossier</div>
              ) : (
                <div className="files-modal-list">
                  {moveFolders.map((folder) => (
                    <div
                      key={folder.id}
                      className="files-modal-item"
                      onClick={() => handleMoveNavigate(folder.path)}
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" width="18" height="18" style={{ color: '#f59e0b', flexShrink: 0 }}>
                        <path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                      </svg>
                      <span>{folder.name}</span>
                      <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" width="14" height="14" style={{ marginLeft: 'auto', opacity: 0.4 }}>
                        <path d="M8 4l6 6-6 6" />
                      </svg>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="files-modal-footer">
              <span className="files-modal-dest">
                Déplacer dans : <strong>{moveDestPath || 'Racine'}</strong>
              </span>
              <div className="files-modal-actions">
                <button className="btn btn-ghost btn-sm" onClick={() => setMoveTarget(null)}>Annuler</button>
                <button className="btn btn-primary btn-sm" onClick={handleMoveConfirm} disabled={moveLoading}>
                  Déplacer ici
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {renameTarget && (
        <div className="files-modal-overlay" onClick={() => { if (!renameBusy) setRenameTarget(null); }}>
          <div className="files-modal" onClick={(e) => e.stopPropagation()}>
            <div className="files-modal-header">
              <h3>Renommer "{renameTarget.name}"</h3>
              <button className="btn btn-ghost btn-sm" onClick={() => setRenameTarget(null)}>✕</button>
            </div>
            <div className="files-modal-body" style={{ padding: 16 }}>
              <input
                className="form-input"
                placeholder="Nouveau nom"
                value={renameName}
                onChange={(e) => setRenameName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleRenameConfirm()}
                autoFocus
              />
            </div>
            <div className="files-modal-footer">
              <div className="files-modal-actions" style={{ marginLeft: 'auto' }}>
                <button className="btn btn-ghost btn-sm" onClick={() => setRenameTarget(null)} disabled={renameBusy}>Annuler</button>
                <button className="btn btn-primary btn-sm" onClick={handleRenameConfirm} disabled={renameBusy}>
                  {renameBusy ? 'Renommage...' : 'Renommer'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {shareTarget && (
        <div className="files-modal-overlay" onClick={() => { if (!shareSending) setShareTarget(null); }}>
          <div className="files-modal" onClick={(e) => e.stopPropagation()}>
            <div className="files-modal-header">
              <h3>Partager "{shareTarget.name}"</h3>
              <button className="btn btn-ghost btn-sm" onClick={() => setShareTarget(null)}>✕</button>
            </div>
            <div className="files-modal-body" style={{ padding: 16 }}>
              <div style={{ fontSize: 12.5, fontWeight: 600, color: '#6B7280', marginBottom: 6 }}>Emails des destinataires</div>
              <input
                className="form-input"
                placeholder="ex: jean@exemple.fr, marie@exemple.fr"
                value={shareEmails}
                onChange={(e) => setShareEmails(e.target.value)}
                autoFocus
              />
              <div style={{ fontSize: 12.5, fontWeight: 600, color: '#6B7280', margin: '12px 0 6px' }}>Message (optionnel)</div>
              <textarea
                className="form-input"
                placeholder="Bonjour, voici le fichier partagé."
                value={shareMessage}
                onChange={(e) => setShareMessage(e.target.value)}
                rows={3}
                style={{ resize: 'vertical' }}
              />
              {shareError && <div className="form-error" style={{ marginTop: 12 }}>{shareError}</div>}
            </div>
            <div className="files-modal-footer">
              <span className="files-modal-dest">
                Le fichier sera partagé et envoyé par email.
              </span>
              <div className="files-modal-actions">
                <button className="btn btn-ghost btn-sm" onClick={() => setShareTarget(null)} disabled={shareSending}>Annuler</button>
                <button className="btn btn-primary btn-sm" onClick={handleShareConfirm} disabled={shareSending}>
                  {shareSending ? 'Envoi en cours...' : 'Partager'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {error && <div className="form-error" style={{ margin: '12px 16px' }}>{error}</div>}

      {loading ? (
        <div className="loading-text">Chargement...</div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" /></svg>}
          title="Dossier vide"
          text="Créez un dossier ou téléchargez des fichiers."
        />
      ) : (
        <div className="files-grid">
          {items.map((item) => (
            <div
              key={item.id}
              className={`files-item ${selected?.path === item.path ? 'selected' : ''}`}
              onClick={() => handleClick(item)}
              onContextMenu={(e) => { e.preventDefault(); setContextMenu(contextMenu === item.path ? null : item.path); }}
            >
              <div className="files-item-icon">{getFileIcon(item.type, item.name)}</div>
              <div className="files-item-info">
                <div className="files-item-name">{item.name}</div>
                <div className="files-item-meta">
                  {item.type === 'folder' ? `${item.size || 0} éléments` : formatSize(item.size)}
                  {item.modified ? ` · ${formatDate(item.modified)}` : ''}
                </div>
              </div>
              <div className="files-item-actions">

                {item.type === 'file' && getOnlyOfficeCategory(item.name) && (
                  <button className="btn btn-ghost btn-sm file-act oo-open-btn" onClick={(e) => { e.stopPropagation(); navigate(`/onlyoffice/${getOnlyOfficeCategory(item.name)}?file=${encodeURIComponent(item.path)}`); }} title="Ouvrir avec OnlyOffice">
                    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="15" height="15">
                      <path d="M14 2H6a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V6l-4-4z" />
                      <path d="M14 2v4h4M10 10l-4 4m0 0l4-4m-4 4h8" />
                    </svg>
                    
                  </button>
                )}
                {item.type === 'file' && (
                  <button className="btn btn-ghost btn-sm file-act" onClick={(e) => { e.stopPropagation(); handleShareOpen(item); }} title="Partager / envoyer par email">
                    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="15" height="15">
                      <path d="M4 12l12-9-4 12-3-4-5-1z" />
                      <path d="M9 11l7-8" />
                    </svg>
                    
                  </button>
                )}
                {item.type === 'file' && (
                  <button className="btn btn-ghost btn-sm file-act" onClick={(e) => { e.stopPropagation(); handleDownload(item); }} title="Télécharger">
                    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="15" height="15">
                      <path d="M10 3v10m0 0l-3-3m3 3l3-3M4 14v2a1 1 0 001 1h10a1 1 0 001-1v-2" />
                    </svg>
                   
                  </button>
                )}
                <button className="btn btn-ghost btn-sm file-act" onClick={(e) => { e.stopPropagation(); handleRenameOpen(item); }} title="Renommer (modifier)">
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="15" height="15">
                    <path d="M4 16h5l9-9a1.4 1.4 0 00-2-2l-9 9v2h-3z" />
                    <path d="M12 5l3 3" />
                  </svg>
                  
                </button>
                <button className="btn btn-ghost btn-sm file-act" onClick={(e) => { e.stopPropagation(); handleMoveOpen(item); }} title="Déplacer">
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="15" height="15">
                    <path d="M3 7v10a2 2 0 002 2h10a2 2 0 002-2V9a2 2 0 00-2-2h-4l-2-2H5a2 2 0 00-2 2z" />
                    <path d="M10 11l3 3m0 0l-3 3m3-3H7" />
                  </svg>
                  
                </button>
                <button className="btn btn-ghost btn-sm file-act" onClick={(e) => { e.stopPropagation(); handleDelete(item); }} title="Supprimer">
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" width="15" height="15">
                    <path d="M6 6l8 8m0-8l-8 8" />
                  </svg>
                  
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
