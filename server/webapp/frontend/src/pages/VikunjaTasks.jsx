import { useEffect, useState, useCallback, useMemo } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import EmptyState from '../components/EmptyState';
import TaskDetailPanel from '../components/TaskDetailPanel';
import { VikunjaIcon, CheckIcon, CloseIcon } from '../components/icons';

function fmtShort(iso) {
  if (!iso || String(iso).startsWith('0001')) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
}

function ProjectSelect({ projects, value, onChange }) {
  return (
    <select className="vk-filter-select" value={value ?? ''} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">Tous les projets</option>
      {projects.filter((p) => !p.virtual).map((p) => (
        <option key={p.id} value={p.id}>{p.title}</option>
      ))}
    </select>
  );
}

function TaskRow({ task, selected, onSelect }) {
  const done = Boolean(task.done);
  const due = fmtShort(task.due_date);
  const busy = done;
  return (
    <div
      className={busy ? 'vk-task-row selected' : 'vk-task-row'}
      onClick={() => onSelect(task.id)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === 'Enter' && onSelect(task.id)}
    >
      <div className={`vk-row-check ${done ? 'done' : ''}`}>{done && <CheckIcon />}</div>
      <div className="vk-row-main">
        <div className="vk-row-title">{task.title || `Tâche #${task.id}`}</div>
        <div className="vk-row-sub">
          {task.project_id ? `Projet #${task.project_id}` : ''}
          {due ? ` · Échéance ${due}` : ''}
          {task.labels && task.labels.length > 0 ? ` · ${task.labels.length} étiquette(s)` : ''}
        </div>
      </div>
      <div className="vk-row-meta">
        {task.priority > 0 && <span className="vk-prio-dot" title={`Priorité ${task.priority}`} />}
        {task.title && task.id}
      </div>
    </div>
  );
}

export default function VikunjaTasks() {
  const navigate = useNavigate();
  const { id } = useParams();
  const selectedId = id ? Number(id) : null;
  const [tasks, setTasks] = useState([]);
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [projectFilter, setProjectFilter] = useState(null);
  const [doneFilter, setDoneFilter] = useState('');

  const loadProjects = useCallback(() => {
    api.vikunjaProjects()
      .then((res) => setProjects(res.projects || []))
      .catch(() => {});
  }, []);

  const loadTasks = useCallback((q = '', project = projectFilter, done = doneFilter) => {
    setLoading(true);
    setError('');
    api.vikunjaTasks(q, project, done)
      .then((res) => setTasks(res.tasks || []))
      .catch((e) => {
        setError(e.message || 'Erreur inconnue');
        setTasks([]);
      })
      .finally(() => setLoading(false));
  }, [projectFilter, doneFilter]);

  useEffect(() => { loadProjects(); }, [loadProjects]);
  useEffect(() => { loadTasks(); }, [loadTasks]);

  const handleSearch = (e) => {
    e.preventDefault();
    loadTasks(search);
  };

  const handleSelect = (taskId) => {
    navigate(`/vikunja/tasks/${taskId}`, { replace: false });
  };

  const handleClose = () => navigate('/vikunja/tasks');

  const filtered = useMemo(() => tasks, [tasks]);

  if (!error && projects.length === 0 && tasks.length === 0 && !loading) {
    return (
      <div className="gmail-page">
        <EmptyState
          icon={<VikunjaIcon />}
          title="Vikunja injoignable"
          text="Vérifiez la configuration Vikunja puis réessayez."
        />
      </div>
    );
  }

  return (
    <div className="gmail-split vk-split">
      <div className="vk-list-pane">
        <form className="odoo-search" onSubmit={handleSearch}>
          <input
            type="text"
            className="odoo-search-input"
            placeholder="Rechercher une tâche..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button type="submit" className="btn btn-primary btn-sm">Chercher</button>
        </form>
        <div className="vk-filter-bar">
          <ProjectSelect projects={projects} value={projectFilter} onChange={setProjectFilter} />
          <select className="vk-filter-select" value={doneFilter} onChange={(e) => setDoneFilter(e.target.value)}>
            <option value="">Toutes</option>
            <option value="false">En cours</option>
            <option value="true">Terminées</option>
          </select>
        </div>

        {loading ? (
          <div className="loading-text">Chargement...</div>
        ) : error ? (
          <EmptyState icon={<VikunjaIcon />} title="Vikunja injoignable" text={error} />
        ) : filtered.length === 0 ? (
          <EmptyState icon={<VikunjaIcon />} title="Aucune tâche" text="Aucune tâche ne correspond à votre recherche." />
        ) : (
          <div className="vk-list-scroll">
            {filtered.map((t) => (
              <TaskRow key={t.id} task={t} selected={t.id === selectedId} onSelect={handleSelect} />
            ))}
          </div>
        )}
      </div>

      <TaskDetailPanel taskId={selectedId} onClose={handleClose} />
    </div>
  );
}