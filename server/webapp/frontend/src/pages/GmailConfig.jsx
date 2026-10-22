import { useEffect, useState } from 'react';
import { api } from '../api/client';

export default function GmailConfig() {
  const [status, setStatus] = useState({ connected: false, email: null, loaded: false, configured: false });
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api.gmailStatus()
      .then((s) => setStatus({ ...s, loaded: true }))
      .catch(() => setStatus({ connected: false, email: null, loaded: true, configured: false }));
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get('connected');
    const errorParam = params.get('error');
    if (connected === '1') {
      api.gmailStatus()
        .then((s) => setStatus({ ...s, loaded: true }))
        .catch(() => setStatus({ connected: false, email: null, loaded: true, configured: false }));
      window.history.replaceState({}, '', '/gmail/config');
    } else if (errorParam) {
      setError(decodeURIComponent(errorParam));
      window.history.replaceState({}, '', '/gmail/config');
    }
  }, []);

  const handleConnect = async () => {
    if (!clientId || !clientSecret) {
      setError('Veuillez remplir Client ID et Client Secret.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      await api.gmailSaveCredentials(clientId, clientSecret);
      const { url } = await api.gmailAuthUrl();
      window.location.href = url;
    } catch (e) {
      setError(e.message || 'Erreur lors de la connexion');
      setSaving(false);
    }
  };

  const handleDisconnect = async () => {
    setSaving(true);
    try {
      await api.gmailDisconnect();
      setStatus({ connected: false, email: null, loaded: true, configured: false });
      setClientId('');
      setClientSecret('');
    } catch (e) {
      setError(e.message || 'Erreur lors de la déconnexion');
    }
    setSaving(false);
  };

  const redirectUri = `${window.location.origin}/api/gmail/callback`;

  return (
    <div className="gmail-page">
      <div className="gmail-config">
        <h2>Configuration Gmail</h2>

        <div className={`status-card ${status.connected ? 'connected' : 'disconnected'}`}>
          <span className={`status-dot ${status.connected ? 'green' : ''}`}></span>
          <span className="status-label">
            {status.connected
              ? `Connecté — ${status.email}`
              : 'Non connecté'}
          </span>
        </div>

        {status.connected ? (
          <button
            className="btn btn-secondary"
            onClick={handleDisconnect}
            disabled={saving}
          >
            Déconnecter
          </button>
        ) : (
          <>
            <div className="form-group">
              <label>Client ID (Google Cloud OAuth)</label>
              <input
                type="text"
                className="form-input"
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                placeholder="xxxx.apps.googleusercontent.com"
              />
            </div>

            <div className="form-group">
              <label>Client Secret</label>
              <input
                type="password"
                className="form-input"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                placeholder="GOCSPX-..."
              />
            </div>

            <div className="form-group">
              <label>URI de redirection (à copier dans Google Cloud Console)</label>
              <input
                type="text"
                className="form-input readonly"
                value={redirectUri}
                readOnly
              />
            </div>

            <div className="form-group">
              <label>Scopes demandés (lecture seule)</label>
              <input
                type="text"
                className="form-input readonly"
                value="https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send"
                readOnly
              />
            </div>

            {error && <div className="form-error">{error}</div>}

            <button
              className="btn btn-primary"
              onClick={handleConnect}
              disabled={saving}
            >
              {saving ? 'Connexion...' : 'Activer l\'API Gmail'}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
