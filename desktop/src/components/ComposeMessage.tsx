import { useState, type FormEvent } from 'react';
import type { AppId, Draft, Persona } from '../types';
import { APP_IDS, APP_NAMES, normalizeComposedBody } from '../lib/domain';
import Modal from './Modal';
export default function ComposeMessage({personas, initialApp, onClose, onSave}: {
  personas: Persona[]; initialApp: AppId; onClose(): void; onSave(draft: Draft): void;
}) {
  const [appId, setApp] = useState(initialApp);
  const allowed = personas.filter(p => !p.excludedApps.includes(appId));
  const [selected, setSelected] = useState(allowed[0]?.id || '');
  const personaId = allowed.some(p => p.id === selected) ? selected : allowed[0]?.id || '';
  const [recipient, setRecipient] = useState('');
  const [subject, setSubject] = useState('');
  const [prompt, setPrompt] = useState('');
  const [changeModel, setChangeModel] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!window.margin || busy || !personaId || !prompt.trim()) return;
    setBusy(true); setError('');
    try {
      const result = await window.margin.composeMessage({appId, personaId, prompt:prompt.trim(), recipient:recipient.trim(), subject:subject.trim(), changeModel});
      if (!result) { setBusy(false); return; }
      onSave({id:crypto.randomUUID(), appId, roleId:personaId, subject:subject.trim() || 'New message', recipient:recipient.trim() || 'Recipient to choose', address:recipient.trim(), context:'', body:normalizeComposedBody(result.body), updatedAt:new Date().toISOString(), localChanges:true, composed:true});
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not draft this message.'); }
    finally { setBusy(false); }
  }
  return <Modal title="Draft a message" onClose={() => {if (!busy) onClose();}}>
    <form onSubmit={submit}>
      <p className="sub spaced">Start a new conversation. Describe what you want to say, then review the draft before sharing it.</p>
      <fieldset disabled={busy} className="compose-fields">
        <label className="field">App<select value={appId} onChange={e => setApp(e.target.value as AppId)}>{APP_IDS.map(id => <option key={id} value={id}>{APP_NAMES[id]}</option>)}</select></label>
        <label className="field">Persona<select data-initial-focus value={personaId} onChange={e => setSelected(e.target.value)}>{!allowed.length && <option value="">No personas allowed for this app</option>}{allowed.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label className="field">To <span className="sub">(optional)</span><input maxLength={320} value={recipient} onChange={e => setRecipient(e.target.value)} placeholder={appId === 'slack' ? 'Person or channel' : 'Name or email address'} /></label>
        <label className="field">Subject <span className="sub">(optional)</span><input maxLength={300} value={subject} onChange={e => setSubject(e.target.value)} placeholder="What’s this about?" /></label>
        <label className="field compose-prompt">What would you like to say?<textarea rows={4} maxLength={6000} required value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Ask Alex to join our project demo next week. Keep it brief and include a meeting link placeholder." /></label>
        <label className="row spaced"><input type="checkbox" checked={changeModel} onChange={e => setChangeModel(e.target.checked)} />Choose a different local model</label>
      </fieldset>
      <p className="sub spaced">Style follows your persona description. Training on your writing history isn’t connected here yet. Drafts stay on your Mac until you copy them into your app.</p>
      {!window.margin && <p className="warning spaced">Open Margin on your Mac to generate with a local model.</p>}
      {busy && <p role="status" className="sub spaced">Drafting on your Mac… Loading the model can take a minute.</p>}
      {error && <p role="alert" className="error spaced">{error}</p>}
      <div className="modal-footer"><button type="button" disabled={busy} onClick={onClose}>Cancel</button><button className="primary" disabled={busy || !personaId || !prompt.trim() || !window.margin}>{busy ? 'Drafting…' : 'Generate draft'}</button></div>
    </form>
  </Modal>;
}
