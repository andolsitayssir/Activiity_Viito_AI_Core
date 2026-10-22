const BASE_URL = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/$/, '');

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
  } catch {
    throw new ApiError('Backend injoignable', 0);
  }
  if (!res.ok) {
    let detail;
    try {
      detail = await res.json();
    } catch {
      detail = null;
    }
    const msg =
      (detail && (detail.detail?.message || detail.detail?.error || detail.message)) ||
      `Erreur ${res.status}`;
    const err = new ApiError(msg, res.status);
    if (detail && typeof detail.detail === 'object' && detail.detail !== null) {
      err.payload = detail.detail;
    }
    throw err;
  }
  return res.json();
}

export const api = {
  listConversations: () => request('/api/conversations'),

  getConversation: (id) => request(`/api/conversations/${encodeURIComponent(id)}`),

  createConversation: (context_item) =>
    request('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({ context_item: context_item || null }),
    }),

  deleteConversation: (id) =>
    request(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  sendMessage: (id, text, attachments = null) => {
    const body = { text };
    if (attachments && attachments.length > 0) {
      body.attachments = attachments;
    }
    return request(`/api/conversations/${encodeURIComponent(id)}/messages`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  uploadFiles: async (files) => {
    const form = new FormData();
    for (const f of files) {
      form.append('files', f);
    }
    const BASE = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/$/, '');
    let res;
    try {
      res = await fetch(`${BASE}/api/upload`, { method: 'POST', body: form });
    } catch {
      throw new ApiError('Backend injoignable', 0);
    }
    if (!res.ok) {
      let detail;
      try { detail = await res.json(); } catch { detail = null; }
      throw new ApiError(detail?.detail || `Erreur ${res.status}`, res.status);
    }
    return res.json();
  },

  actionsSummary: () => request('/api/actions/summary'),

  actionsByDomain: (domain) => request(`/api/actions/${encodeURIComponent(domain)}`),

  transcribeAudio: async (blob) => {
    const form = new FormData();
    form.append('audio', blob, 'recording.webm');
    const BASE = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/$/, '');
    let res;
    try {
      res = await fetch(`${BASE}/api/transcribe`, { method: 'POST', body: form });
    } catch {
      throw new ApiError('Backend injoignable', 0);
    }
    if (!res.ok) {
      let detail;
      try { detail = await res.json(); } catch { detail = null; }
      throw new ApiError(detail?.detail || `Erreur ${res.status}`, res.status);
    }
    return res.json();
  },

  gmailStatus: () => request('/api/gmail/status'),
  gmailSaveCredentials: (client_id, client_secret) =>
    request('/api/gmail/credentials', {
      method: 'POST',
      body: JSON.stringify({ client_id, client_secret }),
    }),
  gmailAuthUrl: () => request('/api/gmail/auth-url'),
  gmailCallback: (code) =>
    request('/api/gmail/callback', { method: 'POST', body: JSON.stringify({ code }) }),
  gmailDisconnect: () =>
    request('/api/gmail/disconnect', { method: 'POST' }),
  gmailMessages: (label = 'inbox', query = '', maxResults = 20) =>
    request('/api/gmail/messages', {
      method: 'POST',
      body: JSON.stringify({ label, query, max_results: maxResults }),
    }),
  gmailMessageDetail: (id) => request(`/api/gmail/messages/${encodeURIComponent(id)}`),
  gmailThread: (threadId) => request(`/api/gmail/threads/${encodeURIComponent(threadId)}`),
  gmailDrafts: () => request('/api/gmail/drafts'),
  gmailSend: (to, subject, body, cc = '') =>
    request('/api/gmail/send', {
      method: 'POST',
      body: JSON.stringify({ to, subject, body, cc }),
    }),
  gmailTrash: (id) => request(`/api/gmail/trash/${encodeURIComponent(id)}`, { method: 'POST' }),
  gmailUntrash: (id) => request(`/api/gmail/untrash/${encodeURIComponent(id)}`, { method: 'POST' }),
  gmailDeleteDraft: (id) => request(`/api/gmail/drafts/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  gmailDownloadAttachment: async (msgId, attId, filename) => {
    const BASE = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/$/, '');
    let res;
    try {
      res = await fetch(`${BASE}/api/gmail/messages/${encodeURIComponent(msgId)}/attachments/${encodeURIComponent(attId)}`);
    } catch {
      throw new ApiError('Backend injoignable', 0);
    }
    if (!res.ok) throw new ApiError(`Erreur ${res.status}`, res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },

  // ── Telegram ─────────────────────────────────────────────────────
  telegramStatus: () => request('/api/telegram/status'),
  telegramSaveConfig: (api_id, api_hash, phone) =>
    request('/api/telegram/config', {
      method: 'POST',
      body: JSON.stringify({ api_id, api_hash, phone }),
    }),
  telegramSendCode: () => request('/api/telegram/send-code', { method: 'POST' }),
  telegramLogin: (code, password) =>
    request('/api/telegram/login', {
      method: 'POST',
      body: JSON.stringify({ code, password: password || null }),
    }),
  telegramLogout: () => request('/api/telegram/logout', { method: 'POST' }),
  telegramRefresh: () => request('/api/telegram/refresh', { method: 'POST' }),
  telegramConversations: () => request('/api/telegram/conversations'),
  telegramConversationMessages: (chatId) =>
    request(`/api/telegram/conversations/${encodeURIComponent(chatId)}`),
  telegramSendToChat: (chatId, text) =>
    request(`/api/telegram/conversations/${encodeURIComponent(chatId)}`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  telegramChannels: () => request('/api/telegram/channels'),
  telegramSendToChannel: (channelId, text) =>
    request(`/api/telegram/channels/${encodeURIComponent(channelId)}`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),

  // ── File management ─────────────────────────────────────────────
  listFiles: (path = '') => request(`/api/files?path=${encodeURIComponent(path)}`),
  getFileInfo: (path) => request(`/api/files/info?path=${encodeURIComponent(path)}`),
  uploadFilesToPath: async (files, path = '') => {
    const form = new FormData();
    for (const f of files) {
      form.append('files', f);
    }
    const BASE = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/$/, '');
    let res;
    try {
      res = await fetch(`${BASE}/api/files/upload?path=${encodeURIComponent(path)}`, { method: 'POST', body: form });
    } catch {
      throw new ApiError('Backend injoignable', 0);
    }
    if (!res.ok) {
      let detail;
      try { detail = await res.json(); } catch { detail = null; }
      throw new ApiError(detail?.detail || `Erreur ${res.status}`, res.status);
    }
    return res.json();
  },
  downloadFile: async (path, filename) => {
    const BASE = (import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/$/, '');
    let res;
    try {
      res = await fetch(`${BASE}/api/files/download/${encodeURIComponent(path)}`);
    } catch {
      throw new ApiError('Backend injoignable', 0);
    }
    if (!res.ok) throw new ApiError(`Erreur ${res.status}`, res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || path.split('/').pop();
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },
  deleteFile: (path) => request(`/api/files/${encodeURIComponent(path)}`, { method: 'DELETE' }),
  createFolder: (folder, parentPath = '') => {
    const full = parentPath ? `${parentPath}/${folder}` : folder;
    return request('/api/files/mkdir', {
      method: 'POST',
      body: JSON.stringify({ folder: full }),
    });
  },
  moveFile: (source, destination) =>
    request('/api/files/move', {
      method: 'POST',
      body: JSON.stringify({ source, destination }),
    }),
  copyFile: (source, destination) =>
    request('/api/files/copy', {
      method: 'POST',
      body: JSON.stringify({ source, destination }),
    }),
  renameFile: (path, newName) =>
    request('/api/files/rename', {
      method: 'POST',
      body: JSON.stringify({ path, new_name: newName }),
    }),
  listTrash: () => request('/api/files/trash'),
  restoreFromTrash: (name) =>
    request('/api/files/trash/restore', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  purgeTrash: () => request('/api/files/trash/purge', { method: 'DELETE' }),
  listShares: () => request('/api/files/shares'),
  shareFile: (path, recipients, message = '') =>
    request('/api/files/shares/email', {
      method: 'POST',
      body: JSON.stringify({ path, recipients, message }),
    }),

  // ── ONLYOFFICE ────────────────────────────────────────────────────
  onlyofficeStatus: () => request('/api/onlyoffice/docserver'),
  onlyofficeConfig: (path, type = null) =>
    request('/api/onlyoffice/config', {
      method: 'POST',
      body: JSON.stringify(type ? { path, type } : { path }),
    }),
  onlyofficeGetContent: (path) =>
    request(`/api/onlyoffice/content?path=${encodeURIComponent(path)}`),
  onlyofficeSetContent: (path, content, encoded = false) =>
    request('/api/onlyoffice/content', {
      method: 'POST',
      body: JSON.stringify({ path, content, encoded }),
    }),
  onlyofficeViito: (path, requestText) =>
    request('/api/onlyoffice/viito', {
      method: 'POST',
      body: JSON.stringify({ path, request: requestText }),
    }),

  // ── ODOO ──────────────────────────────────────────────────────────
  odooStatus: () => request('/api/odoo/status'),
  odooList: (modelKey, search = '') =>
    request(`/api/odoo/${encodeURIComponent(modelKey)}/list?search=${encodeURIComponent(search)}`),
  odooDetail: (modelKey, recordId) =>
    request(`/api/odoo/${encodeURIComponent(modelKey)}/${recordId}`),
  odooMeetingInvite: (recordId) =>
    request(`/api/odoo/meetings/${encodeURIComponent(recordId)}/invite`, {
      method: 'POST',
    }),
  odooMeetingEdit: (recordId, payload) =>
    request(`/api/odoo/meetings/${encodeURIComponent(recordId)}/edit`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ── VIKUNJA ────────────────────────────────────────────────────────
  vikunjaStatus: () => request('/api/vikunja/status'),
  vikunjaSaveConfig: (url, username, password) =>
    request('/api/vikunja/config', {
      method: 'POST',
      body: JSON.stringify({ url, username, password }),
    }),
  vikunjaProjects: () => request('/api/vikunja/projects'),
  vikunjaProjectDetail: (projectId) =>
    request(`/api/vikunja/projects/${encodeURIComponent(projectId)}`),
  vikunjaProjectTasks: (projectId, search = '') =>
    request(`/api/vikunja/projects/${encodeURIComponent(projectId)}/tasks?${search ? `s=${encodeURIComponent(search)}` : ''}`),
  vikunjaTasks: (search = '', project = null, done = '') => {
    const params = new URLSearchParams();
    if (search) params.set('s', search);
    if (project != null) params.set('project', project);
    if (done) params.set('done', done);
    const qs = params.toString();
    return request(`/api/vikunja/tasks${qs ? `?${qs}` : ''}`);
  },
  vikunjaTaskDetail: (taskId) =>
    request(`/api/vikunja/tasks/${encodeURIComponent(taskId)}`),
  vikunjaTaskComments: (taskId) =>
    request(`/api/vikunja/tasks/${encodeURIComponent(taskId)}/comments`),
  vikunjaLabels: () => request('/api/vikunja/labels'),
  vikunjaNotifications: () => request('/api/vikunja/notifications'),

  // ── Viito multi-turn sessions ─────────────────────────────────────
  onlyofficeListViitoSessions: () => request('/api/onlyoffice/viito/sessions'),
  onlyofficeCreateViitoSession: (filePath) =>
    request('/api/onlyoffice/viito/session', {
      method: 'POST',
      body: JSON.stringify({ file_path: filePath }),
    }),
  onlyofficeGetViitoSession: (sessionId) =>
    request(`/api/onlyoffice/viito/session/${encodeURIComponent(sessionId)}`),
  onlyofficeDeleteViitoSession: (sessionId) =>
    request(`/api/onlyoffice/viito/session/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    }),
  onlyofficeSendViitoMessage: (sessionId, content, attachments = null) =>
    request(`/api/onlyoffice/viito/session/${encodeURIComponent(sessionId)}/message`, {
      method: 'POST',
      body: JSON.stringify({ content, attachments }),
    }),
  onlyofficeAttachViitoSession: (sessionId, filename, contentType, contentB64) =>
    request(`/api/onlyoffice/viito/session/${encodeURIComponent(sessionId)}/attach`, {
      method: 'POST',
      body: JSON.stringify({ filename, content_type: contentType, content_b64: contentB64 }),
    }),
};
