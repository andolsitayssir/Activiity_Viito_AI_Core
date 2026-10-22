import { useEffect, useState, useCallback } from 'react';
import { useNavigate, useParams, Link } from 'react-router-dom';
import { api } from '../api/client';
import EmptyState from '../components/EmptyState';
import { VikunjaIcon, CheckIcon, BackIcon, FolderIcon } from '../components/icons';

function ProjectCard({ project, selected, onSelect }) {
  const virtual = Boolean(project.virtual);
  return (
    <div
      className={selected ? 'vk-project-card selected' : 'vk-project-card'}
      onClick={() => onSelect(project.id)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === 'Enter' && onSelect(project.id)}
    >
      <div className="vk-project-avatar" style={virtual ? { background: '#F3F4F6', color: '#9CA3AF' } : undefined}>
        {virtual ? '★' : (project.title || '#').charAt(0).toUpperCase()}
      </div>
      <div className="vk-project-main">
        <div className="vk-project-title">{project.title || `Projet #${project.id}`}</div>
        <div className="vk-project-sub">
          {(project.views || []).join(' · ') || 'Aucune vue'}
          {project.is_archived ? ' · Archivé' : ''}
          {project.is_favorite ? ' · Favori' : ''}
        </div>
      </div>
    </div>
  );
}

function ProjectTask({ task, onOpen }) {
  const done = Boolean(task.done);
  return (
    <div className={`vk-project-task ${done ? 'done' : ''}`} onClick={() => onOpen(task.id)} role="button" tabIndex={0}
      onKeyDown={(e) => e.key === 'Enter' && onOpen(task.id)}>
      <div className={`vk-row-check ${done ? 'done' : ''}`}>{done && <CheckIcon />}</div>
      <div className="vk-row-main">
        <div className="vk-row-title">{task.title || `Tâche #${task.id}`}</div>
        {task.description && <div className="vk-row-sub vk-project-task-desc">{task.description.slice(0, 90)}{task.description.length > 90 ? '…' : ''}</div>}
      </div>
      <div className="vk-row-meta">
        {task.labels && task.labels.length > 0 && (
          <span className="vk-label-count">{task.labels.length}</span>
        )}
        <span className="vk-task-id-meta">#{task.id}</span>
      </div>
    </div>
  );
}

export default function VikunjaProjects() {
  const navigate = useNavigate();
  const { id } = useParams();
  const selectedProjectId = id ? Number(id) : null;
  const [projects, setProjects] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [taskLoading, setTaskLoading] = useState(false);
  const [error, setError] = useState('');
  const [taskError, setTaskError] = useState('');
  const [search, setSearch] = useState('');

  const loadProjects = useCallback(() => {
    setLoading(true);
    setError('');
    api.vikunjaProjects()
      .then((res) => setProjects(res.projects || []))
      .catch((e) => {
        setError(e.message || 'Erreur inconnue');
        setProjects([]);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);

  const loadTasks = useCallback((projectId, q = '') => {
    if (projectId == null) {
      setTasks([]);
      return;
    }
    setTaskLoading(true);
    setTaskError('');
    api.vikunjaProjectTasks(projectId, q)
      .then((res) => setTasks(res.tasks || []))
      .catch((e) => {
        setTaskError(e.message || 'Erreur inconnue');
        setTasks([]);
      })
      .finally(() => setTaskLoading(false));
  }, []);

  const activeProject = projects.find((p) => p.id === selectedProjectId);

  useEffect(() => {
    loadTasks(selectedProjectId);
  }, [selectedProjectId, loadTasks]);

  const handleSelectProject = (projectId) => {
    navigate(`/vikunja/projects/${projectId}`, { replace: false });
  };

  const handleSearch = (e) => {
    e.preventDefault();
    if (selectedProjectId != null) loadTasks(selectedProjectId, search);
  };

  const openTask = (taskId) => navigate(`/vikunja/tasks/${taskId}`);

  return (
    <div className="gmail-split vk-split">
      <div className="vk-list-pane">
        <div className="vk-list-pane-title">
          <FolderIcon />
          Projets
        </div>
        {loading ? (
          <div className="loading-text">Chargement...</div>
        ) : error ? (
          <EmptyState icon={<VikunjaIcon />} title="Vikunja injoignable" text={error} />
        ) : projects.length === 0 ? (
          <EmptyState icon={<VikunjaIcon />} title="Aucun projet" text="Aucun projet Vikunja trouvé." />
        ) : (
          <div className="vk-list-scroll">
            {projects.map((p) => (
              <ProjectCard key={p.id} project={p} selected={p.id === selectedProjectId} onSelect={handleSelectProject} />
            ))}
          </div>
        )}
      </div>

      <div className="vk-project-detail">
        {selectedProjectId == null || !activeProject ? (
          <div className="gmail-detail-empty">
         
            <p>Sélectionnez un projet pour voir ses tâches</p>
          </div>
        ) : (
          <>
            <div className="vk-project-header">
              <div>
                <div className="vk-project-header-title">{activeProject.title || `Projet #${activeProject.id}`}</div>
                <div className="vk-project-header-sub">Projet #{activeProject.id} · {activeProject.views && activeProject.views.length > 0 ? (activeProject.views.join(', ')) : 'vues par défaut'}</div>
              </div>
              <div className="vk-project-header-actions">
                <Link className="btn btn-ghost btn-sm" to={`/vikunja/projects/${activeProject.id}?tasks=1`} title="Ouvrir la liste des tâches">Voir les tâches</Link>
              </div>
            </div>

            {activeProject.description && (
              <div className="vk-project-desc">{activeProject.description}</div>
            )}

            <form className="odoo-search" onSubmit={handleSearch}>
              <input
                type="text"
                className="odoo-search-input"
                placeholder="Chercher dans ce projet..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <button type="submit" className="btn btn-primary btn-sm">Chercher</button>
            </form>

            <div className="vk-project-tasks-scroll">
              {taskLoading ? (
                <div className="loading-text">Chargement des tâches...</div>
              ) : taskError ? (
                <div className="form-error" style={{ margin: 16 }}>{taskError}</div>
              ) : tasks.length === 0 ? (
                <EmptyState icon={<VikunjaIcon />} title="Aucune tâche" text="Ce projet ne contient aucune tâche." />
              ) : (
                <div className="vk-project-task-list">
                  <div className="vk-project-task-count">{tasks.length} tâche(s)</div>
                  {tasks.map((t) => (
                    <ProjectTask key={t.id} task={t} onOpen={openTask} />
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}