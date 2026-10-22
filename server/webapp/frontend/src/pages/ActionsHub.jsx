import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { DOMAINS } from '../constants';

export default function ActionsHub() {
  const [counts, setCounts] = useState({});
  const [err, setErr] = useState(false);

  useEffect(() => {
    api
      .actionsSummary()
      .then((rows) => {
        const map = {};
        rows.forEach((r) => {
          map[r.domain] = r.count;
        });
        setCounts(map);
      })
      .catch(() => setErr(true));
  }, []);

  return (
    <div className="hub-scroll">
      <p className="hub-intro">
        Les 13 domaines officiels sur lesquels Viito route et exécute chaque demande. Cliquez
        sur un domaine pour voir ce qui a été créé.
      </p>
      {err && (
        <p style={{ fontSize: 12.5, color: 'var(--muted)' }}>
          Backend hors ligne — les compteurs ne peuvent pas être chargés.
        </p>
      )}
      <div className="hub-grid">
        {DOMAINS.map((d) => {
          const count = counts[d.key] || 0;
          return (
            <Link to={`/actions/${d.key}`} key={d.key} className="hub-card">
              <div className="hub-card-top">
                <span className={`hub-dot${count ? '' : ' off'}`}></span>
                <span className="hub-card-count">{count}</span>
              </div>
              <div className="hub-card-label">{d.label}</div>
              <div className="hub-card-key">{d.key}</div>
              <span className="hub-card-tool">{d.tool}</span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
