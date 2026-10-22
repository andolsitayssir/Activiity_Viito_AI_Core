export const DOMAINS = [
  { key: 'crm',            label: 'CRM',                 tool: 'Odoo' },
  { key: 'accounting',     label: 'Comptabilité',        tool: 'Odoo' },
  { key: 'sales',          label: 'Ventes / Devis',      tool: 'Odoo' },
  { key: 'hr',             label: 'RH',                  tool: 'Odoo' },
  { key: 'contacts',       label: 'Contacts',            tool: 'Odoo' },
  { key: 'tasking',        label: 'Tâches',              tool: 'Vikunja' },
  { key: 'office',         label: 'Bureautique',         tool: 'OnlyOffice' },
  { key: 'messaging',      label: 'Messagerie',          tool: 'Telegram' },
  { key: 'email_action',   label: 'Email',               tool: 'Gmail' },
  { key: 'file_management', label: 'Fichiers',            tool: 'Nextcloud' },
  { key: 'calendaring',    label: 'Agenda',              tool: 'Odoo' },
  { key: 'knowledge_kb',   label: 'Base de connaissances', tool: 'RAG' },
  { key: 'general',        label: 'Réponse libre',       tool: 'Viito' },
];

export const domainMeta = (key) => DOMAINS.find((d) => d.key === key);
