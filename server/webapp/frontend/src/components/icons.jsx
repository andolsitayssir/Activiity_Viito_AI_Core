export const BrandMark = () => (
  <svg className="brand-mark" viewBox="0 0 26 26" fill="none">
    <path d="M13 2C7 2 4 6 4 10.5C4 16 9 20 13 24C17 20 22 16 22 10.5C22 6 19 2 13 2Z" fill="#D6007A" />
    <circle cx="9.5" cy="10.5" r="1.6" fill="white" />
    <circle cx="16.5" cy="10.5" r="1.6" fill="white" />
  </svg>
);

export const ChatIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M3 8c0-2.5 2.5-4.5 6-4.5s6 2 6 4.5-2.5 4.5-6 4.5c-.7 0-1.4-.1-2-.3L4 14l.7-2.7C3.6 10.4 3 9.3 3 8z" />
  </svg>
);

export const BoltIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M11 2L4 12h5l-1 6 7-10h-5l1-6z" />
  </svg>
);

export const InfoIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <circle cx="10" cy="10" r="7" />
    <path d="M10 9v4.5M10 6.7v.1" />
  </svg>
);

export const PlusIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <path d="M10 4v12M4 10h12" />
  </svg>
);

export const EmptyChatIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4">
    <path d="M3 8c0-2.5 2.5-4.5 6-4.5s6 2 6 4.5-2.5 4.5-6 4.5c-.7 0-1.4-.1-2-.3L4 14l.7-2.7C3.6 10.4 3 9.3 3 8z" />
  </svg>
);

export const MicIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7">
    <rect x="7.5" y="3" width="5" height="9" rx="2.5" />
    <path d="M5 10a5 5 0 0010 0M10 15v2.5" />
  </svg>
);

export const SendIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
    <path d="M4 10h12M11 5l5 5-5 5" />
  </svg>
);

export const BackIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M12 4l-6 6 6 6" />
  </svg>
);

export const TrashIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M5 6h10M8 6V4.5A.5.5 0 018.5 4h3a.5.5 0 01.5.5V6m2 0v9a1 1 0 01-1 1H7a1 1 0 01-1-1V6h8z" />
  </svg>
);

export const InspectorIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" width="14" height="14">
    <circle cx="10" cy="10" r="3" />
    <path d="M10 3v2M10 15v2M3 10h2M15 10h2M5.05 5.05l1.41 1.41M13.54 13.54l1.41 1.41M5.05 14.95l1.41-1.41M13.54 6.46l1.41-1.41" />
  </svg>
);

export const MailIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="3" y="5" width="14" height="10" rx="1.5" />
    <path d="M3 6.5l7 4 7-4" />
  </svg>
);

export const ChevronIcon = ({ open }) => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" style={{ transform: open ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}>
    <path d="M8 5l5 5-5 5" />
  </svg>
);

export const InboxIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
    <rect x="3" y="4" width="14" height="12" rx="1.5" />
    <path d="M3 11h4l1.5 2h3l1.5-2h4" />
  </svg>
);

export const SentIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
    <path d="M4 10l12-6-4 14-3-5-5-3z" />
    <path d="M16 4l-6 8" />
  </svg>
);

export const DraftIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
    <path d="M14 3l3 3-10 10H4v-3L14 3z" />
  </svg>
);

export const PaperclipIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
    <path d="M14.5 5.5l-7 7a2.83 2.83 0 004 4l7-7a4.24 4.24 0 00-6-6l-7 7a5.66 5.66 0 008 8" />
  </svg>
);

export const FolderIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M3 7v8a1.5 1.5 0 001.5 1.5h11a1.5 1.5 0 001.5-1.5V8.5a1.5 1.5 0 00-1.5-1.5h-5l-1.5-2H4.5A1.5 1.5 0 003 7z" />
  </svg>
);

export const DocIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M14 2H6a1.5 1.5 0 00-1.5 1.5v13A1.5 1.5 0 006 18h8a1.5 1.5 0 001.5-1.5v-13A1.5 1.5 0 0014 2z" />
    <path d="M8 6h4M8 9h4M8 12h2" />
  </svg>
);

export const WordIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="3" y="2.5" width="14" height="15" rx="1.5" />
    <path d="M7 6.5l1.5 4 1.2-3 .8 3 1.5-4" />
  </svg>
);

export const ExcelIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="3" y="2.5" width="14" height="15" rx="1.5" />
    <path d="M7 6.5l1.5 3.5 1.5-3.5M10 10v-3.5M7 13.5h1.5L10 11l1.5 2.5H13" />
  </svg>
);

export const PdfIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M6 2.5H14l3 3v12H6z" />
    <path d="M13 2.5v3h3" />
    <path d="M6.5 10.5c.8 0 .8-2.5 0-2.5H5v4m0 0h5M5 14v2m2.5-4v2" />
  </svg>
);

export const SlidesIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="3" y="3" width="14" height="9" rx="1.5" />
    <path d="M10 12v3M7 18h6" />
  </svg>
);

export const OdooIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M3 10c0-3.9 3.1-7 7-7s7 3.1 7 7-3.1 7-7 7-7-3.1-7-7z" />
    <path d="M10 3v14M3 10h14" />
    <path d="M10 3a7 7 0 017 7M10 3a7 7 0 00-7 7" strokeWidth="0" />
    <path d="M7 10l3 3 3-3" />
  </svg>
);

export const TelegramIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M4 9.8l11.5-5.3c.6-.3 1.2.2 1 .8l-2.4 9.4c-.2.6-.8.8-1.3.5l-3-2.2-1.6 1.5c-.4.4-1 .2-1.1-.4l-.7-3.4-2.1-.8c-.6-.2-.7-.9-.3-1.1z" />
    <path d="M8.4 13.1l7.6-6.6-5.6 7.8" strokeWidth="0" fill="currentColor" />
    <circle cx="8.4" cy="13.1" r="0.8" fill="currentColor" />
  </svg>
);

export const VikunjaIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="3" y="4" width="14" height="12" rx="1.5" />
    <path d="M6.5 8h7M6.5 11.5h4" />
    <circle cx="12.5" cy="12.5" r="2.2" fill="currentColor" stroke="none" fillOpacity="0.45" />
  </svg>
);

export const CheckIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 10.5l4 4 8-9" />
  </svg>
);

export const CloseIcon = () => (
  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
    <path d="M5 5l10 10M15 5L5 15" />
  </svg>
);
