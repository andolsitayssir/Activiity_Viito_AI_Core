import { useParams, useNavigate } from 'react-router-dom';
import RecordDetailPanel from '../components/RecordDetailPanel';
import { BackIcon } from '../components/icons';

const RH_TAB_MODEL = { conges: 'hr-leaves', employes: 'hr-employees' };

function resolveModelKey(modelKey, tab) {
  if (tab && RH_TAB_MODEL[tab]) return RH_TAB_MODEL[tab];
  return modelKey || 'contacts';
}

export default function OdooDetailPage({ modelKey }) {
  const { id, tab } = useParams();
  const navigate = useNavigate();
  const resolvedModel = resolveModelKey(modelKey, tab);

  return (
    <div className="gmail-split odoo-split odoo-detail-full">
      <div style={{ padding: '18px 24px', flex: '0 0 auto' }}>
        <button className="back-link" onClick={() => navigate(-1)}>
          <BackIcon /> Retour
        </button>
      </div>
      <RecordDetailPanel modelKey={resolvedModel} recordId={Number(id)} />
    </div>
  );
}