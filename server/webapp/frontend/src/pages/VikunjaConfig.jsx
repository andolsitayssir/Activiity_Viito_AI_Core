import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { BackIcon, VikunjaIcon } from '../components/icons';

export default function VikunjaConfig() {
  const [status, setStatus] = useState({ loaded: false, configured: false, connected: false, url: '', username: '', user: null, error: '' });
  const [url, setUrl] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState('');
  const [msg, setMsg] = useState('');

  const loadStatus = (silent = false) => {
    if (!silent) setTesting(true);
    api.vikunjaStatus()
      .then((s) => {
        setStatus({ ...s, loaded: true });
        setUrl(s.url || '');
        setUsername(s.username || '');
        if (!s.username) setUsername(s.username || '');
      })
      .catch((e) => setStatus({ loaded: true, configured: false, connected: false, url, username, user: null, error: e.message || 'Erreur inconnue' }))
      .finally(() => setTesting(false));
  };

  useEffect(() => { loadStatus(true); }, []);

  const handleSave = async () => {
    if (!username.trim() || !password) {
      setError("Nom d'utilisateur et mot de passe sont requis.");
      return;
    }
    setSaving(true);
    setError('');
    setMsg('');
    try {
      const res = await api.vikunjaSaveConfig(url.trim() || 'http://localhost:3456', username.trim(), password);
      setStatus({ ...res, loaded: true });
      setMsg(res.connected ? 'Configuration enregistrée et connexion réussie.' : 'Configuration enregistrée mais connexion impossible.');
      setPassword('');
    } catch (e) {
      const detail = e.payload || {};
      const message = (typeof detail === 'object' && detail ? detail.message || detail.error : '') || e.message || 'Erreur lors de l\'enregistrement';
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="gmail-page vikunja-config">
      <div className="domain-header" style={{ paddingLeft: 0, paddingTop: 0, marginBottom: 18 }}>
        <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>Configuration Vikunja</h2>
      </div>

      <div className="gmail-config">
        <div className={`status-card ${status.connected ? 'connected' : 'disconnected'}`}>
          <span className={`status-dot ${status.connected ? 'green' : ''}`}></span>
          <span className="status-label">
            {status.connected
              ? `Connecté — ${status.user?.name || status.username}${status.url ? ` (${status.url})` : ''}`
              : status.configured ? 'Connecté impossible' : 'Non configuré'}
          </span>
        </div>

        {status.connected && status.user && (
          <div className="vikunja-status-row">
            <span className="vikunja-status-label">Utilisateur connecté</span>
            <span className="vikunja-status-value">{status.user.name || status.user.username} (id {status.user.id})</span>
          </div>
        )}

        {(error || status.error) && (status.error !== status.username || true) && (
          <div className="form-error">{(error || status.error || '')}</div>
        )}

        <div className="form-group">
          <label>URL du serveur Vikunja</label>
          <input
            type="text"
            className="form-input"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="http://localhost:3456"
          />
        </div>

        <div className="form-group">
          <label>Nom d'utilisateur</label>
          <input
            type="text"
            className="form-input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="viito-agent"
          />
        </div>

        <div className="form-group">
          <label>Mot de passe</label>
          <input
            type="password"
            className="form-input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
          />
        </div>

        <div className="vikunja-config-actions">
          <button className="btn btn-primary" onClick={handleSave} disabled={saving || testing}>
            {saving ? 'Enregistrement...' : 'Enregistrer et tester la connexion'}
          </button>
          <button className="btn btn-secondary" onClick={() => loadStatus()} disabled={testing || saving}>
            {testing ? 'Test en cours...' : 'Tester la connexion'}
          </button>
        </div>

        {msg && <div className="vikunja-success-msg">{msg}</div>}

        <div className="vikunja-hint">
          <div className="vikunja-hint-icon"><VikunjaIcon /></div>
          <p>
            Ces identifiants sont utilisés par le moteur IA pour créer, modifier et lire vos
            tâches Vikunja. Ils sont enregistrés localement (vikunja_config.json) et écrasent
            les variables d'environnement VIKUNJA_USERNAME / VIKUNJA_PASSWORD.
          </p>
        </div>
      </div>
    </div>
  );
}