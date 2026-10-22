import { Routes, Route, Navigate } from 'react-router-dom';
import AppShell from './components/AppShell';
import ConversationsPage from './pages/ConversationsPage';

import About from './pages/About';
import GmailConfig from './pages/GmailConfig';
import GmailMessages from './pages/GmailMessages';
import TelegramConfig from './pages/TelegramConfig';
import TelegramConversations from './pages/TelegramConversations';
import TelegramChannels from './pages/TelegramChannels';
import FileBrowser from './pages/FileBrowser';
import OnlyOfficePage from './pages/OnlyOfficePage';
import OdooListPage from './pages/OdooListPage';
import OdooRhPage from './pages/OdooRhPage';
import VikunjaConfig from './pages/VikunjaConfig';
import VikunjaProjects from './pages/VikunjaProjects';
import VikunjaTasks from './pages/VikunjaTasks';

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to="/conversations" replace />} />
        <Route path="/conversations" element={<ConversationsPage />} />
        <Route path="/conversations/:conversationId" element={<ConversationsPage />} />
        
        
        <Route path="/files" element={<FileBrowser />} />
        <Route path="/files/*" element={<FileBrowser />} />
        <Route path="/onlyoffice/:category" element={<OnlyOfficePage />} />
        <Route path="/onlyoffice" element={<OnlyOfficePage />} />
        <Route path="/odoo/contacts/:id" element={<OdooListPage modelKey="contacts" />} />
        <Route path="/odoo/contacts" element={<OdooListPage modelKey="contacts" />} />
        <Route path="/odoo/crm/:id" element={<OdooListPage modelKey="crm" />} />
        <Route path="/odoo/crm" element={<OdooListPage modelKey="crm" />} />
        <Route path="/odoo/rh/:tab/:id" element={<OdooRhPage />} />
        <Route path="/odoo/rh/:tab" element={<OdooRhPage />} />
        <Route path="/odoo/rh" element={<OdooRhPage />} />
        <Route path="/odoo/accounting/:id" element={<OdooListPage modelKey="accounting" />} />
        <Route path="/odoo/accounting" element={<OdooListPage modelKey="accounting" />} />
        <Route path="/odoo/sales/:id" element={<OdooListPage modelKey="sales" />} />
        <Route path="/odoo/sales" element={<OdooListPage modelKey="sales" />} />
        <Route path="/odoo/meetings/:id" element={<OdooListPage modelKey="meetings" />} />
        <Route path="/odoo/meetings" element={<OdooListPage modelKey="meetings" />} />
        <Route path="/odoo" element={<Navigate to="/odoo/contacts" replace />} />
        <Route path="/vikunja/config" element={<VikunjaConfig />} />
        <Route path="/vikunja/projects/:id" element={<VikunjaProjects />} />
        <Route path="/vikunja/projects" element={<VikunjaProjects />} />
        <Route path="/vikunja/tasks/:id" element={<VikunjaTasks />} />
        <Route path="/vikunja/tasks" element={<VikunjaTasks />} />
        <Route path="/gmail/config" element={<GmailConfig />} />
        <Route path="/gmail/inbox" element={<GmailMessages type="inbox" />} />
        <Route path="/gmail/sent" element={<GmailMessages type="sent" />} />
        <Route path="/gmail/drafts" element={<GmailMessages type="drafts" />} />
        <Route path="/gmail/trash" element={<GmailMessages type="trash" />} />
        <Route path="/telegram/config" element={<TelegramConfig />} />
        <Route path="/telegram/conversations" element={<TelegramConversations />} />
        <Route path="/telegram/channels" element={<TelegramChannels />} />
        <Route path="/about" element={<About />} />
        <Route path="*" element={<Navigate to="/conversations" replace />} />
      </Routes>
    </AppShell>
  );
}