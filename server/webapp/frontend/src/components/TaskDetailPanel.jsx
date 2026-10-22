import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { CloseIcon, CheckIcon, VikunjaIcon } from './icons';

function pickColor(title) {
  const colors = ['#D6007A', '#8B5CF6', '#3B82F6', '#0EA5E9', '#10B981', '#F59E0B', '#EF4444', '#EC4899'];
  let hash = 0;
  for (let i = 0; i < (title || '').length; i++) hash = title.charCodeAt(i) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

function fmtDate(iso) {
  if (!iso) return '';
  if (String(iso).startsWith('0001')) return '';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' }) +
    (d.getHours() || d.getMinutes() ? ` ${d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}` : '');
}

function cn(...parts) { return parts.filter(Boolean).join(' '); }

const PRIORITY_LABELS = { 0: 'Basse', 1: 'Moyenne', 2: 'Haute', 3: 'Urgente' };
const PRIORITY_COLORS = { 0: '#10B981', 1: '#F59E0B', 2: '#EF4444', 3: '#991B1B' };

function Field({ label, value }) {
  if (value === undefined || value === null || value === '') return null;
  return (
    <div className="vk-field">
      <div className="vk-field-label">{label}</div>
      <div className="vk-field-value">{value}</div>
    </div>
  );
}

export default function TaskDetailPanel({ taskId, onClose }) {
  const [task, setTask] = useState(null);
  const [comments, setComments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (taskId == null) return;
    setLoading(true);
    setError('');
    setTask(null);
    setComments([]);
    api.vikunjaTaskDetail(taskId)
      .then((res) => setTask(res.task || null))
      .catch((e) => setError(e.message || 'Erreur inconnue'))
      .finally(() => setLoading(false));
    api.vikunjaTaskComments(taskId)
      .then((res) => setComments(res.comments || []))
      .catch(() => {});
  }, [taskId]);

  if (taskId == null) {
    return (
      <div className="gmail-detail-empty">
        
        <p>Sélectionnez une tâche pour voir son détail</p>
      </div>
    );
  }

  if (loading) return <div className="gmail-detail-empty"><div className="loading-text">Chargement...</div></div>;

  if (error) {
    return (
      <div className="gmail-detail-empty">
       
        <p>{error}</p>
      </div>
    );
  }

  if (!task) {
    return <div className="gmail-detail-empty"><p>Tâche introuvable</p></div>;
  }

  const done = Boolean(task.done);
  const dueDate = fmtDate(task.due_date);
  const priorityColor = PRIORITY_COLORS[task.priority] || PRIORITY_COLORS[0];

  return (
    <div className="gmail-detail vk-detail">
      <div className="gmail-detail-topbar">
        <button className="btn btn-ghost btn-sm" onClick={onClose} title="Fermer le détail">
          <CloseIcon />
        </button>
        <div className="gmail-detail-actions">
          {done && <span className="vk-done-badge">Terminée</span>}
          <span className="tool-tag">Vikunja</span>
        </div>
      </div>

      <div className="gmail-detail-scroll vk-detail-scroll">
        <div className="vk-task-header">
          <div
            className="vk-task-check"
            style={{ background: done ? '#10B981' : '#F3F4F6', color: done ? '#fff' : '#D1D5DB', borderColor: done ? '#10B981' : '#E5E7EB' }}
          >
            {done && <CheckIcon />}
          </div>
          <div className="vk-task-titlewrap">
            <div className="vk-task-title">{task.title || `Tâche #${task.id}`}</div>
            <div className="vk-task-id">#{task.identifier || task.id} · projet {task.project_id}</div>
          </div>
        </div>

        <div className="vk-task-meta-row">
          <span className="vk-priority-chip" style={{ background: `${priorityColor}1A`, color: priorityColor, borderColor: `${priorityColor}40` }}>
            {PRIORITY_LABELS[task.priority] || 'Basse'}
          </span>
          {task.percent_done > 0 && (
            <span className="vk-percent-chip">{task.percent_done}% fait</span>
          )}
          {dueDate && <span className="vk-date-chip">Échéance : {dueDate}</span>}
        </div>

        {task.description ? (
          <div className="vk-section">
            <div className="vk-section-title">Description</div>
            <div className="vk-description">{task.description}</div>
          </div>
        ) : null}

        <div className="vk-fields-card">
          <Field label="Création" value={`${fmtDate(task.created)}${task.created_by ? ` par ${task.created_by}` : ''}`} />
          <Field label="Dernière mise à jour" value={fmtDate(task.updated)} />
          <Field label="Début" value={fmtDate(task.start_date)} />
          <Field label="Fin" value={fmtDate(task.end_date)} />
          <Field label="Position" value={task.position != null ? task.position : ''} />
          <Field label="Pièces jointes" value={task.attachments > 0 ? `${task.attachments} pièce(s)` : ''} />
          <Field label="Tâches liées" value={task.related_tasks > 0 ? `${task.related_tasks} relation(s)` : ''} />
        </div>

        {(task.labels && task.labels.length > 0) ? (
          <div className="vk-section">
            <div className="vk-section-title">Étiquettes</div>
            <div className="vk-label-row">
              {task.labels.map((l) => (
                <span key={l.id} className="vk-label-chip" style={{ borderColor: l.hex_color || '#E5E7EB' }}>
                  <span className="vk-label-dot" style={{ background: l.hex_color || pickColor(l.title) }} />
                  {l.title}
                </span>
              ))}
            </div>
          </div>
        ) : null}

        {(task.assignees && task.assignees.length > 0) ? (
          <div className="vk-section">
            <div className="vk-section-title">Assigné à</div>
            <div className="vk-label-row">
              {task.assignees.map((a) => (
                <span key={a.id} className="vk-assignee-chip">{a.name || `Utilisateur #${a.id}`}</span>
              ))}
            </div>
          </div>
        ) : null}

        <div className="vk-section">
          <div className="vk-section-title">Commentaires ({comments.length})</div>
          {comments.length === 0 ? (
            <div className="vk-comments-empty">Aucun commentaire.</div>
          ) : (
            <div className="vk-comments">
              {comments.map((c) => (
                <div key={c.id} className="vk-comment">
                  <div className="vk-comment-top">
                    <span className="vk-comment-author">{c.author || 'Inconnu'}</span>
                    <span className="vk-comment-date">{fmtDate(c.created)}</span>
                  </div>
                  <div className="vk-comment-body">{c.comment}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="vk-footer-actions">
          <a className="btn btn-ghost" href={`/vikunja/tasks/${task.id}`}>
            Ouvrir la vue tâches
          </a>
        </div>
      </div>
    </div>
  );
}