import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { ArrowUpIcon, CaretDownIcon, CheckIcon, SlidersHorizontalIcon } from "@phosphor-icons/react";
import type { AppId, Draft, Persona } from "../types";
import { APP_IDS, APP_NAMES, normalizeComposedBody } from "../lib/domain";
import AppMark from "./AppMark";
import Modal from "./Modal";
export default function ComposeMessage({personas, initialApp, onClose, onSave}: {
  personas: Persona[]; initialApp: AppId; onClose(): void; onSave(draft: Draft): void;
}) {
  const [appId, setApp] = useState(initialApp);
  const allowed = personas.filter(p => !p.excludedApps.includes(appId));
  const [selected, setSelected] = useState(allowed[0]?.id || "");
  const persona = allowed.find(p => p.id === selected) || allowed[0];
  const [recipient, setRecipient] = useState("");
  const [subject, setSubject] = useState("");
  const [prompt, setPrompt] = useState("");
  const [changeModel, setChangeModel] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerId = useId();
  const picker = useRef<HTMLDivElement>(null);
  const pickerButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (pickerOpen) picker.current?.querySelector<HTMLButtonElement>("button[aria-pressed='true'], button")?.focus();
  }, [pickerOpen]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!window.margin || busy || !persona || !prompt.trim()) return;
    setBusy(true); setError(""); setPickerOpen(false);
    try {
      const result = await window.margin.composeMessage({appId, personaId:persona.id, prompt:prompt.trim(), recipient:recipient.trim(), subject:subject.trim(), changeModel});
      if (!result) return;
      onSave({id:crypto.randomUUID(), appId, roleId:persona.id, subject:subject.trim() || "New message", recipient:recipient.trim() || "Recipient to choose", address:recipient.trim(), context:"", body:normalizeComposedBody(result.body), updatedAt:new Date().toISOString(), localChanges:true, composed:true});
    } catch (e) {
      const message = e instanceof Error ? e.message : "Could not draft this message.";
      setError(message.replace(/^Error invoking remote method '[^']+': Error: /, ""));
    } finally {setBusy(false);}
  }
  return <Modal title="New message" className="chat-compose" onClose={() => {if (!busy) onClose();}}>
    <form onSubmit={submit} onClick={event => {
      if (!(event.target as HTMLElement).closest(".persona-picker")) setPickerOpen(false);
    }}>
      <div className="compose-topline">
        <div className="persona-picker" onKeyDown={event => {
          if (event.key === "Escape" && pickerOpen) {
            event.preventDefault(); event.stopPropagation(); setPickerOpen(false); pickerButton.current?.focus();
          }
        }}>
          <button ref={pickerButton} type="button" className="persona-trigger" disabled={busy} aria-expanded={pickerOpen} aria-controls={pickerId} aria-label={`Choose persona: ${persona?.name || "None available"}`} onClick={() => setPickerOpen(open => !open)}>
            {persona?.name || "Choose a persona"}<CaretDownIcon size={16} />
          </button>
          {pickerOpen && <div ref={picker} id={pickerId} className="persona-options" role="group" aria-label="Choose persona">
            <span className="overline">Your personas</span>
            {!allowed.length && <p className="sub">No personas are allowed for {APP_NAMES[appId]}. Update your persona’s app settings.</p>}
            {allowed.map(item => <button type="button" key={item.id} aria-pressed={persona?.id === item.id} onClick={() => {setSelected(item.id); setPickerOpen(false); pickerButton.current?.focus();}}>
              <span><strong>{item.name}</strong><small>{item.description}</small></span>
              {persona?.id === item.id && <CheckIcon size={17} />}
            </button>)}
          </div>}
        </div>
        <span className="sub">Writing style</span>
      </div>
      <label className="compose-intro" htmlFor={`${pickerId}-prompt`}>What should the message say?</label>
      <div className="chat-prompt">
        <textarea id={`${pickerId}-prompt`} data-initial-focus aria-label="Message prompt" rows={5} maxLength={6000} required disabled={busy} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Invite Alex to our project demo next week. Keep it friendly and brief…" onKeyDown={event => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {event.preventDefault(); event.currentTarget.form?.requestSubmit();}
        }} />
        <div className="chat-prompt-toolbar">
          <label className="compose-app" data-app={appId}><AppMark app={appId} size={16} /><select aria-label="Destination app" value={appId} disabled={busy} onChange={e => setApp(e.target.value as AppId)}>{APP_IDS.map(id => <option key={id} value={id}>{APP_NAMES[id]}</option>)}</select></label>
          <button className="compose-submit primary" type="submit" aria-label={busy ? "Drafting message" : "Create draft"} disabled={busy || !persona || !prompt.trim() || !window.margin}><span>{busy ? "Drafting…" : "Create draft"}</span><ArrowUpIcon size={17} /></button>
        </div>
      </div>
      <details className="compose-options"><summary><SlidersHorizontalIcon size={15} />Message details <span className="sub">Optional</span></summary><fieldset disabled={busy} className="compose-fields">
        <label className="field">To<input maxLength={320} value={recipient} onChange={e => setRecipient(e.target.value)} placeholder={(appId === "slack" || appId === "discord") ? "Person or channel" : "Name or email address"} /></label>
        <label className="field">Subject<input maxLength={300} value={subject} onChange={e => setSubject(e.target.value)} placeholder="What’s this about?" /></label>
        <label className="row spaced"><input type="checkbox" checked={changeModel} onChange={e => setChangeModel(e.target.checked)} />Choose a different local model</label>
      </fieldset></details>
      <p className="compose-note sub">Drafts use your persona’s trained style when available. Review the draft, then copy it into your app.</p>
      {!window.margin && <p className="warning spaced">Open Margin on your Mac to generate with a local model.</p>}
      {busy && <p role="status" className="sub spaced">Drafting on your Mac… Loading the model can take a minute.</p>}
      {error && <p role="alert" className="error spaced">{error}</p>}
    </form>
  </Modal>;
}
