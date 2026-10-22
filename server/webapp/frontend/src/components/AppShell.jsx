import { NavLink, useLocation, Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { BrandMark, ChatIcon, BoltIcon, InfoIcon, MailIcon, ChevronIcon, InboxIcon, SentIcon, DraftIcon, TrashIcon, FolderIcon, DocIcon, WordIcon, ExcelIcon, PdfIcon, SlidesIcon, OdooIcon, TelegramIcon, VikunjaIcon } from './icons';

function useConnection() {
  const [connected, setConnected] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let alive = true;
    api
      .listConversations()
      .then(() => alive && setConnected(true))
      .catch(() => alive && setConnected(false))
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, []);
  return { connected, loaded };
}

function topbarFor(pathname) {
  if (pathname.startsWith('/gmail')) {
    const sub = pathname.split('/')[2] || 'config';
    const labels = { config: 'Configuration', inbox: 'Reçus', sent: 'Envoyés', drafts: 'Brouillons', trash: 'Corbeille' };
    return ['Gmail', labels[sub] || ''];
  }
  if (pathname.startsWith('/telegram')) {
    const sub = pathname.split('/')[2] || 'config';
    const labels = { config: 'Configuration', conversations: 'Conversations', channels: 'Canaux' };
    return ['Telegram', labels[sub] || ''];
  }
  if (pathname.startsWith('/files')) return ['Fichiers', 'Gestionnaire de fichiers local'];
  if (pathname.startsWith('/onlyoffice')) {
    const cat = pathname.split('/')[2] || 'word';
    const labels = { word: 'Word', cell: 'Excel', slide: 'Présentation', pdf: 'PDF' };
    return ['OnlyOffice', labels[cat] || cat];
  }
  if (pathname.startsWith('/odoo')) {
    const sub = pathname.split('/')[2] || '';
    const labels = {
      contacts: 'Contacts',
      crm: 'CRM',
      rh: 'RH',
      accounting: 'Factures',
      sales: 'Ventes',
      meetings: 'Réunions',
    };
    return ['Odoo', labels[sub] || ''];
  }
  if (pathname.startsWith('/vikunja')) {
    const sub = pathname.split('/')[2] || '';
    const labels = { config: 'Configuration', projects: 'Projets', tasks: 'Tâches' };
    return ['Vikunja', labels[sub] || ''];
  }

  if (pathname === '/about') return ['À propos', 'Comment fonctionne Viito'];
  return ['Conversations', 'Toutes les discussions gérées par Viito'];
}

function GmailAccordion({ location }) {
  const [open, setOpen] = useState(() => location.pathname.startsWith('/gmail'));
  const isActive = location.pathname.startsWith('/gmail');

  useEffect(() => {
    if (isActive && !open) setOpen(true);
  }, [isActive]);

  const gmailSubItems = [
    { to: '/gmail/config', label: 'Configuration', icon: <InboxIcon /> },
    { to: '/gmail/inbox', label: 'Reçus', icon: <InboxIcon /> },
    { to: '/gmail/sent', label: 'Envoyés', icon: <SentIcon /> },
    { to: '/gmail/drafts', label: 'Brouillons', icon: <DraftIcon /> },
    { to: '/gmail/trash', label: 'Corbeille', icon: <TrashIcon /> },
  ];

  return (
    <div className={`nav-accordion${isActive ? ' active' : ''}`}>
      <button
        className={'nav-item' + (isActive ? ' active' : '')}
        onClick={() => setOpen(!open)}
      >
        <MailIcon />
        Gmail
        <span className="accordion-chevron"><ChevronIcon open={open} /></span>
      </button>
      {open && (
        <div className="nav-sub">
          {gmailSubItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive: subActive }) => 'nav-sub-item' + (subActive ? ' active' : '')}
            >
              {item.icon}
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

function TelegramAccordion({ location }) {
  const [open, setOpen] = useState(() => location.pathname.startsWith('/telegram'));
  const isActive = location.pathname.startsWith('/telegram');

  useEffect(() => {
    if (isActive && !open) setOpen(true);
  }, [isActive]);

  const telegramSubItems = [
    { to: '/telegram/config', label: 'Configuration', icon: <InboxIcon /> },
    { to: '/telegram/conversations', label: 'Conversations', icon: <ChatIcon /> },
    { to: '/telegram/channels', label: 'Canaux', icon: <SentIcon /> },
  ];

  return (
    <div className={`nav-accordion${isActive ? ' active' : ''}`}>
      <button
        className={'nav-item' + (isActive ? ' active' : '')}
        onClick={() => setOpen(!open)}
      >
        <TelegramIcon />
        Telegram
        <span className="accordion-chevron"><ChevronIcon open={open} /></span>
      </button>
      {open && (
        <div className="nav-sub">
          {telegramSubItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive: subActive }) => 'nav-sub-item' + (subActive ? ' active' : '')}
            >
              {item.icon}
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

function OnlyOfficeAccordion({ location }) {
  const [open, setOpen] = useState(() => location.pathname.startsWith('/onlyoffice'));
  const isActive = location.pathname.startsWith('/onlyoffice');

  useEffect(() => {
    if (isActive && !open) setOpen(true);
  }, [isActive]);

  const subItems = [
    { to: '/onlyoffice/word', label: 'Word', icon: <WordIcon /> },
    { to: '/onlyoffice/cell', label: 'Excel', icon: <ExcelIcon /> },
    { to: '/onlyoffice/pdf', label: 'PDF', icon: <PdfIcon /> },
    { to: '/onlyoffice/slide', label: 'Présentation', icon: <SlidesIcon /> },
  ];

  return (
    <div className={`nav-accordion${isActive ? ' active' : ''}`}>
      <button
        className={'nav-item' + (isActive ? ' active' : '')}
        onClick={() => setOpen(!open)}
      >
        <DocIcon />
        OnlyOffice
        <span className="accordion-chevron"><ChevronIcon open={open} /></span>
      </button>
      {open && (
        <div className="nav-sub">
          {subItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive: subActive }) => 'nav-sub-item' + (subActive ? ' active' : '')}
            >
              {item.icon}
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

function OdooAccordion({ location }) {
  const [open, setOpen] = useState(() => location.pathname.startsWith('/odoo'));
  const isActive = location.pathname.startsWith('/odoo');

  useEffect(() => {
    if (isActive && !open) setOpen(true);
  }, [isActive]);

  const subItems = [
    { to: '/odoo/contacts', label: 'Contacts', icon: <InboxIcon /> },
    { to: '/odoo/crm', label: 'CRM', icon: <BoltIcon /> },
    { to: '/odoo/rh', label: 'RH', icon: <DocIcon /> },
    { to: '/odoo/accounting', label: 'Factures', icon: <ExcelIcon /> },
    { to: '/odoo/sales', label: 'Ventes', icon: <SlidesIcon /> },
    { to: '/odoo/meetings', label: 'Réunions', icon: <DocIcon /> },
  ];

  return (
    <div className={`nav-accordion${isActive ? ' active' : ''}`}>
      <button
        className={'nav-item' + (isActive ? ' active' : '')}
        onClick={() => setOpen(!open)}
      >
        <OdooIcon />
        Odoo
        <span className="accordion-chevron"><ChevronIcon open={open} /></span>
      </button>
      {open && (
        <div className="nav-sub">
          {subItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive: subActive }) => 'nav-sub-item' + (subActive ? ' active' : '')}
            >
              {item.icon}
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

function VikunjaAccordion({ location }) {
  const [open, setOpen] = useState(() => location.pathname.startsWith('/vikunja'));
  const isActive = location.pathname.startsWith('/vikunja');

  useEffect(() => {
    if (isActive && !open) setOpen(true);
  }, [isActive]);

  const subItems = [
    { to: '/vikunja/projects', label: 'Projets', icon: <FolderIcon /> },
    { to: '/vikunja/tasks', label: 'Tâches', icon: <DocIcon /> },
  ];

  return (
    <div className={`nav-accordion${isActive ? ' active' : ''}`}>
      <button
        className={'nav-item' + (isActive ? ' active' : '')}
        onClick={() => setOpen(!open)}
      >
        <VikunjaIcon />
        Vikunja
        <span className="accordion-chevron"><ChevronIcon open={open} /></span>
      </button>
      {open && (
        <div className="nav-sub">
          {subItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive: subActive }) => 'nav-sub-item' + (subActive ? ' active' : '')}
            >
              {item.icon}
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

export default function AppShell({ children }) {
  const { connected, loaded } = useConnection();
  const location = useLocation();
  const [title, sub] = topbarFor(location.pathname);

  const brand = (
    <div className="brand">
      <BrandMark />
      <span className="brand-name">activ<span className="accent">ii</span>ty</span>
    </div>
  );

  return (
    <div className="app">
      <aside className="sidebar">
        <Link to="/" style={{ color: 'inherit' }}>{brand}</Link>
        <div className="nav-label">NAVIGATION</div>
        <nav>
          <NavLink to="/" className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
            <ChatIcon />
            Conversations
          </NavLink>

          <GmailAccordion location={location} />
          <TelegramAccordion location={location} />
          <OnlyOfficeAccordion location={location} />
          <OdooAccordion location={location} />
          <VikunjaAccordion location={location} />
          <NavLink to="/files" className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
            <FolderIcon />
            Fichiers
          </NavLink>
          <NavLink to="/about" className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
            <InfoIcon />
            À propos
          </NavLink>
        </nav>
      </aside>

      <div className="main">
        <div className="topbar">
          <div>
            <h1>{title}</h1>
            <div className="topbar-sub">{sub}</div>
          </div>
         
        </div>
        {children}
      </div>
    </div>
  );
}
