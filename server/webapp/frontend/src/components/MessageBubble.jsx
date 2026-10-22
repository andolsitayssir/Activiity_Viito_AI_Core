import { useState, useRef, useEffect, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import { InspectorIcon } from './icons';

function InspectorPopup({ meta, onClose, anchorRef }) {
  const popupRef = useRef(null);
  const [style, setStyle] = useState({ position: 'fixed', top: 0, left: 0, visibility: 'hidden', zIndex: 10000 });
  const actions = meta?.actions || [];

  useLayoutEffect(() => {
    // The popup lives inside an overflow-y:auto message list; a purely
    // absolute popup gets clipped / painted under the thread header bar.
    // Portal to <body> with fixed positioning + a high z-index keeps it
    // always on top. Positions are re-computed on resize/scroll.
    function place() {
      const anchor = anchorRef.current;
      const popup = popupRef.current;
      if (!anchor || !popup) return;
      const r = anchor.getBoundingClientRect();
      const width = popup.offsetWidth || 340;
      const height = popup.offsetHeight || 0;
      const spaceTop = r.top - 6;
      const spaceBottom = window.innerHeight - r.bottom - 6;
      const flip = height > spaceTop && spaceBottom > spaceTop;
      const left = Math.min(Math.max(r.left, 8), Math.max(window.innerWidth - width - 8, 8));
      setStyle(
        flip
          ? { position: 'fixed', top: r.bottom + 6, left, visibility: 'visible', zIndex: 10000 }
          : { position: 'fixed', bottom: window.innerHeight - r.top + 6, left, visibility: 'visible', zIndex: 10000 }
      );
    }
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [anchorRef]);

  useEffect(() => {
    function handleClick(e) {
      if (
        popupRef.current && !popupRef.current.contains(e.target) &&
        anchorRef.current && !anchorRef.current.contains(e.target)
      ) {
        onClose();
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [onClose, anchorRef]);
  const domains = meta?.domains || [];
  const outcome = meta?.outcome || 'finished';

  return createPortal(
    <div className="inspector-popup" ref={popupRef} style={style}>
      <div className="inspector-header">
        <span className="inspector-title">Détails du traitement</span>
        <span className={`inspector-badge ${outcome === 'failed' ? 'failed' : outcome === 'escalated' ? 'warn' : 'ok'}`}>
          {outcome === 'failed' ? 'Échoué' : outcome === 'escalated' ? 'Escaladé' : 'Terminé'}
        </span>
      </div>

      {domains.length > 0 && (
        <div className="inspector-section">
          <div className="inspector-label">Domaines</div>
          <div className="inspector-chips">
            {domains.map((d, i) => (
              <span key={i} className="inspector-chip">{d}</span>
            ))}
          </div>
        </div>
      )}

      {actions.length > 0 && (
        <div className="inspector-section">
          <div className="inspector-label">Actions exécutées ({actions.length})</div>
          <div className="inspector-actions">
            {actions.map((a, i) => (
              <div key={i} className="inspector-action">
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
        </div>
      )}

      {actions.length === 0 && domains.length === 0 && (
        <div className="inspector-empty">Aucune action détaillée disponible</div>
      )}
    </div>,
    document.body
  );
}

export default function MessageBubble({ msg }) {
  const role = msg.role || 'contact';
  const content = msg.content || msg.text || '';
  const meta = msg.meta;
  const isAssistant = role === 'assistant';
  const hasInspector = isAssistant && meta && (meta.actions?.length > 0 || meta.domains?.length > 0);
  const [showInspector, setShowInspector] = useState(false);
  const anchorRef = useRef(null);

  if (msg.pending) {
    return (
      <div className="msg-wrapper assistant">
        <div className="msg contact">
          <span className="oo-viito-thinking" aria-label="Viito réfléchit">
            <span></span><span></span><span></span>
          </span>
        </div>
      </div>
    );
  }

  const className =
    role === 'user' ? 'msg user' : isAssistant ? 'msg contact' : `msg ${role}`;

  const wrapperClass = isAssistant ? 'msg-wrapper assistant' : role === 'user' ? 'msg-wrapper user' : 'msg-wrapper';

  return (
    <div className={wrapperClass}>
      <div className={className}>{content}</div>
      {hasInspector && (
        <div className="msg-inspector-anchor" ref={anchorRef}>
          <button
            className="msg-inspector-btn"
            onClick={() => setShowInspector((v) => !v)}
            title="Inspecter le traitement"
          >
            <InspectorIcon />
          </button>
          {showInspector && (
            <InspectorPopup meta={meta} onClose={() => setShowInspector(false)} anchorRef={anchorRef} />
          )}
        </div>
      )}
    </div>
  );
}
