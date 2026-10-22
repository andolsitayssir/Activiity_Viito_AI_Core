import { BoltIcon } from './icons';
import ToolTag from './ToolTag';

export default function ListRow({ item, onDiscuss }) {
  return (
    <div className="list-row">
      <div className="row-icon"><BoltIcon /></div>
      <div className="row-main">
        <div className="row-title">{item.title}</div>
        <div className="row-sub">{item.summary || ''}</div>
      </div>
      <div className="row-meta">
        <ToolTag tool={item.tool} />
        <span className="row-time"> {formatTime(item.created_at)}</span>
       
      </div>
    </div>
  );
}

export function formatTime(iso) {
  if (!iso) return '';
  const then = new Date(iso);
  if (isNaN(then)) return '';
  return then.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
}
