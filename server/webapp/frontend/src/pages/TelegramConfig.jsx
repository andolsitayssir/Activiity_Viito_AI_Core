import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { TelegramIcon } from '../components/icons';

const EMPTY = { connected: false, configured: false, awaiting_code: false, me: null, phone: '', loaded: false };

export default function TelegramConfig() {
  const [status, setStatus] = useState(EMPTY);
  const [apiId, setApiId] = useState('');
  const [apiHash, setApiHash] = useState('');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [passwordNeeded, setPasswordNeeded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');

  useEffect(() => {
    api.telegramStatus()
      .then((s) => setStatus({ ...s, loaded: true }))
      .catch(() => setStatus({ ...EMPTY, loaded: true }));
  }, []);

  const refresh = () =>
    api.telegramStatus()
      .then((s) => {
        setStatus({ ...s, loaded: true });
        return s;
      })
      .catch(() => ({ ...EMPTY, loaded: true }));

  const saveAndSendCode = async () => {
    if (!apiId.trim() || !apiHash.trim() || !phone.trim()) {
      setError('Veuillez remplir api_id, api_hash et le numéro de téléphone.');
      return;
    }
    setBusy(true);
    setError('');
    setInfo('');
    setPasswordNeeded(false);
    try {
      const s = await api.telegramSaveConfig(apiId.trim(), apiHash.trim(), phone.trim());
      setStatus({ ...s, loaded: true });
      if (s.connected) {
        setInfo('Déjà connecté.');
        return;
      }
      await api.telegramSendCode();
      setStatus({ ...(await api.telegramStatus()), loaded: true });
      setInfo('Code envoyé par Telegram (SMS ou notification de l\'app). Saisissez-le ci-dessous.');
    } catch (e) {
      setError(e.message || 'Erreur lors de la configuration');
    } finally {
      setBusy(false);
    }
  };

  const submitCode = async () => {
    if (!code.trim()) {
      setError('Saisissez le code reçu.');
      return;
    }
    setBusy(true);
    setError('');
    setInfo('');
    try {
      const s = await api.telegramLogin(code.trim(), password || null);
      setStatus({ ...s, loaded: true });
      setCode('');
      setPassword('');
      setPasswordNeeded(false);
      if (s.connected) setInfo('Connexion réussie.');
    } catch (e) {
      if (e.payload && e.payload.password_needed) {
        setPasswordNeeded(true);
        setError(e.payload.message || 'Ce compte a activé la vérification en deux étapes (2FA).');
      } else {
        setError((e.message && typeof e.message === 'string' && e.message) || 'Code invalide');
      }
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setError('');
    try {
      const s = await api.telegramLogout();
      setStatus({ ...s, loaded: true });
      setPasswordNeeded(false);
      setCode('');
      setPassword('');
      setInfo('Déconnecté.');
    } catch (e) {
      setError(e.message || 'Erreur à la déconnexion');
    } finally {
      setBusy(false);
    }
  };

  const me = status.me || {};

  return (
    <div className="gmail-page">
      <div className="gmail-config">
        <h2>Configuration Telegram</h2>

        <div className={`status-card ${status.connected ? 'connected' : 'disconnected'}`}>
          <span className={`status-dot ${status.connected ? 'green' : ''}`}></span>
          <span className="status-label">
            {status.connected
              ? `Connecté — ${[me.first_name, me.last_name].filter(Boolean).join(' ')} (${me.username ? `@${me.username}` : me.phone || ''})`
              : (status.awaiting_code ? 'Code envoyé — en attente de validation'
                 : (status.configured ? 'Configuration enregistrée, connexion requise' : 'Non connecté'))}
          </span>
        </div>

        {error && <div className="form-error">{error}</div>}
        {info && !error && <div className="form-error" style={{ color: '#16a34a' }}>{info}</div>}

        {status.connected ? (
          <div style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={disconnect} disabled={busy}>
              {busy ? 'Déconnexion...' : 'Se déconnecter de Telegram'}
            </button>
          </div>
        ) : (
          <>
            {!status.awaiting_code ? (
              <>
                <div className="form-group">
                  <label>api_id (my.telegram.org → API development tools)</label>
                  <input className="form-input" value={apiId} onChange={(e) => setApiId(e.target.value)} placeholder="ex. 28542137" />
                </div>
                <div className="form-group">
                  <label>api_hash</label>
                  <input className="form-input" type="password" value={apiHash} onChange={(e) => setApiHash(e.target.value)} placeholder="32 caractères hexadécimaux" />
                </div>
                <div className="form-group">
                  <label>Numéro de téléphone (format international)</label>
                  <input className="form-input" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+33612345678" />
                </div>
                <button className="btn btn-primary" onClick={saveAndSendCode} disabled={busy}>
                  {busy ? 'Envoi...' : 'Enregistrer et envoyer le code'}
                </button>
              </>
            ) : (
              <>
                <div className="form-group">
                  <label>Code de connexion reçu sur Telegram</label>
                  <input className="form-input" value={code} onChange={(e) => setCode(e.target.value)} placeholder="12345" autoComplete="one-time-code" />
                </div>
                {passwordNeeded && (
                  <div className="form-group">
                    <label>Mot de passe du compte Telegram (2FA)</label>
                    <input className="form-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Mot de passe à deux étapes" />
                  </div>
                )}
                <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                  <button className="btn btn-primary" onClick={submitCode} disabled={busy || !code.trim()}>
                    {busy ? 'Vérification...' : passwordNeeded ? 'Valider le mot de passe' : 'Valider le code'}
                  </button>
                  <button className="btn btn-secondary" onClick={() => setStatus({ ...status, awaiting_code: false })} disabled={busy}>
                    Retour
                  </button>
                </div>
              </>
            )}
          </>
        )}

        <div style={{ marginTop: 22, fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.5 }}>
         
          <p>1. Sur <code>my.telegram.org</code>, connectez-vous avec votre numéro → <strong>API development tools</strong> → créez une application pour récupérer <code>api_id</code> et <code>api_hash</code>.</p>
          </div>

      </div>
    </div>
  );
}