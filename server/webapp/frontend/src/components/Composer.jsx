import { useEffect, useRef, useState } from 'react';
import { MicIcon, SendIcon, PaperclipIcon } from './icons';
import { api } from '../api/client';

const mediaSupported =
  typeof navigator !== 'undefined' && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;

function pickMimeType() {
  const types = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ];
  if (typeof MediaRecorder === 'undefined') return '';
  return types.find((t) => MediaRecorder.isTypeSupported(t)) || '';
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function Composer({ onSend, disabled }) {
  const [text, setText] = useState('');
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [micDisabled, setMicDisabled] = useState(!mediaSupported);
  const [voiceErr, setVoiceErr] = useState('');
  const [files, setFiles] = useState([]);
  const recorderRef = useRef(null);
  const chunksRef = useRef([]);
  const taRef = useRef(null);
  const fileInputRef = useRef(null);
  const filesRef = useRef([]);

  useEffect(() => {
    filesRef.current = files;
  }, [files]);

  const autoGrow = () => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 80) + 'px';
  };

  const handleKey = (e) => {
    autoGrow();
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const submit = async () => {
    if ((!text.trim() && files.length === 0) || !onSend || disabled) return;
    const value = text.trim();
    const pending = files;
    try {
      await deliver(value, pending);
      setText('');
      setFiles([]);
      filesRef.current = [];
      autoGrow();
    } catch (err) {
      setVoiceErr(err?.message ? `Envoi interrompu : ${err.message}` : 'Envoi interrompu — le fichier n\'a pas pu être attaché.');
      console.error('[composer] send failed:', err);
    }
  };

  const deliver = async (value, pendingFiles) => {
    let attachments = null;
    if (pendingFiles.length > 0) {
      const result = await api.uploadFiles(pendingFiles);
      attachments = result.files || [];
      if (attachments.length !== pendingFiles.length) {
        const uploaded = attachments.map((a) => a.filename);
        const missing = pendingFiles.map((f) => f.name).filter((n) => !uploaded.includes(n));
        throw new Error(
          `Fichier${missing.length > 1 ? 's' : ''} non pris en charge (${missing.join(', ')}).`
        );
      }
    }
    onSend(value, attachments);
  };

  const handleFilePick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = (e) => {
    const picked = Array.from(e.target.files || []);
    if (picked.length > 0) {
      setFiles((prev) => [...prev, ...picked].slice(0, 5));
    }
    e.target.value = '';
  };

  const removeFile = (idx) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const toggleMic = async () => {
    if (recording) {
      if (recorderRef.current && recorderRef.current.state === 'recording') {
        recorderRef.current.stop();
      }
      return;
    }

    setVoiceErr('');
    const mimeType = pickMimeType();
    if (!mimeType) {
      setMicDisabled(true);
      setVoiceErr('Audio non supporté par ce navigateur');
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream, { mimeType });
      chunksRef.current = [];

      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        const blob = new Blob(chunksRef.current, { type: mimeType });
        if (blob.size < 100) {
          setVoiceErr('Audio trop court, réessayez');
          return;
        }

        setTranscribing(true);
        try {
          const result = await api.transcribeAudio(blob);
          const transcript = result?.text || '';
          const pending = filesRef.current;
          if (transcript || pending.length > 0) {
            await deliver(transcript, pending);
            setFiles([]);
            filesRef.current = [];
          } else {
            setVoiceErr('Aucun texte détecté');
          }
        } catch (err) {
          console.error('[voice] transcribe failed:', err);
          setVoiceErr(err.message || 'Erreur de transcription');
        } finally {
          setTranscribing(false);
        }
      };

      rec.onerror = (e) => {
        console.error('[voice] recorder error:', e);
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        setMicDisabled(true);
        setVoiceErr('Erreur d\'enregistrement');
      };

      recorderRef.current = rec;
      setRecording(true);
      rec.start();
    } catch (err) {
      console.error('[voice] getUserMedia failed:', err);
      setMicDisabled(true);
      setVoiceErr('Microphone inaccessible');
    }
  };

  const micLabel = transcribing
    ? 'Transcription...'
    : recording
      ? 'Arrêter'
      : 'Requête audio';

  return (
    <div className="composer-wrap">
      {(recording || transcribing) && (
        <div className="rec-indicator show">
          <span className="rec-dot"></span>
          {transcribing ? 'Transcription en cours...' : 'Enregistrement en cours...'}
        </div>
      )}
      {voiceErr && (
        <div className="rec-indicator show" style={{ color: '#DC2626' }}>
          {voiceErr}
        </div>
      )}
      {files.length > 0 && (
        <div className="composer-files">
          {files.map((f, i) => (
            <div key={i} className="composer-file-chip">
              <span className="composer-file-name">{f.name}</span>
              <span className="composer-file-size">{formatSize(f.size)}</span>
              <button className="composer-file-remove" onClick={() => removeFile(i)} title="Retirer">
                &times;
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="composer">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: 'none' }}
          onChange={handleFileChange}
        />
        <button
          className="icon-btn attach"
          onClick={handleFilePick}
          disabled={disabled || transcribing || files.length >= 5}
          title="Joindre un fichier"
        >
          <PaperclipIcon />
        </button>
        <textarea
          ref={taRef}
          rows="1"
          placeholder="Écrivez à Viito..."
          value={text}
          disabled={disabled || transcribing}
          onChange={(e) => { setText(e.target.value); autoGrow(); }}
          onKeyDown={handleKey}
        />
        <button
          className={`icon-btn mic${recording ? ' recording' : ''}${transcribing ? ' transcribing' : ''}`}
          onClick={toggleMic}
          disabled={micDisabled || disabled || transcribing}
          title={micLabel}
        >
          <MicIcon />
        </button>
        <button
          className="icon-btn send"
          onClick={submit}
          disabled={(!text.trim() && files.length === 0) || disabled || transcribing}
          title="Envoyer"
        >
          <SendIcon />
        </button>
      </div>
    </div>
  );
}
