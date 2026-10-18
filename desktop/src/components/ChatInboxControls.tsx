import { useEffect, useState } from "react";
import { ArrowClockwiseIcon, FolderOpenIcon } from "@phosphor-icons/react";
import type { ChatConnection, ChatInbox, ChatSource } from "../types";
import { APP_NAMES } from "../lib/domain";
import { chatKey, chatStatus } from "../lib/chat";
import AppMark from "./AppMark";

export default function ChatInboxControls({source, selection, onSelect, onLoaded}: {
  source: ChatSource;
  selection: string;
  onSelect: (selection: string) => void;
  onLoaded: (inbox: ChatInbox) => void;
}) {
  const [connections, setConnections] = useState<ChatConnection[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const name = APP_NAMES[source];
  const current = connections.find(c => chatKey(c.account_id, c.workspace_id) === selection)
    || (connections.length === 1 ? connections[0] : undefined);
  useEffect(() => {
    let active = true;
    if (!window.margin) return;
    setBusy("Finding connections…");
    window.margin.chatConnections(source).then(result => {
      if (active) { setConnections(result || []); setLoaded(true); }
    }).catch(e => { if (active) setError(e.message || "Could not find saved connections."); })
      .finally(() => { if (active) setBusy(""); });
    return () => { active = false; };
  }, [source]);
  async function choose() {
    if (!window.margin) return;
    setBusy("Choosing connection…"); setError("");
    try {
      const result = await window.margin.chatConnections(source, true);
      if (result) {
        setConnections(result); setLoaded(true);
        onSelect(result.length === 1 ? chatKey(result[0].account_id, result[0].workspace_id) : "");
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Could not open the connection."); }
    finally { setBusy(""); }
  }
  async function load(sync: boolean) {
    if (!window.margin || !current) return;
    setBusy(sync ? `Syncing and drafting for ${name}…` : `Loading ${name} drafts…`); setError("");
    try {
      const result = await (sync ? window.margin.syncChat : window.margin.chatInbox)(source, current.account_id, current.workspace_id);
      if (result) {
        if (result.source !== source || result.accountId !== current.account_id || result.workspaceId !== current.workspace_id)
          throw new Error("The worker returned a different connection.");
        onLoaded(result);
        onSelect(chatKey(result.accountId, result.workspaceId));
        setConnections(values => values.map(c => c === current ? result.status : c));
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Could not load suggestions."); }
    finally { setBusy(""); }
  }
  return <section className="chat-connection" aria-label={`${name} connection`} aria-busy={!!busy}>
    <div className="row"><AppMark app={source} /><h2>{name} inbox</h2></div>
    <p className="sub">Load replies from your saved connection. Review and copy them, then send in {name}.</p>
    {!window.margin ? <p className="sub spaced">Open the Mac app to load your {name} connection. Sample drafts are available here.</p> : <>
      {connections.length > 0 && <label className="field">{source === "slack" ? "Workspace and account" : "Server and account"}
        <select disabled={!!busy} value={current ? chatKey(current.account_id, current.workspace_id) : ""}
          onChange={e => onSelect(e.target.value)}>
          <option value="">Choose a connection</option>
          {connections.map(c => <option key={chatKey(c.account_id, c.workspace_id)} value={chatKey(c.account_id, c.workspace_id)}>
            {c.workspace_id} · {c.account_id}
          </option>)}
        </select>
      </label>}
      {loaded && !connections.length && <p className="sub spaced">No saved {name} connections found. Set up {name} with the local worker, then choose its connection below.</p>}
      <div className="row spaced chat-controls">
        <button disabled={!!busy || !current} onClick={() => void load(false)}>Load drafts</button>
        <button disabled={!!busy || !current} onClick={() => void load(true)}>
          <ArrowClockwiseIcon size={15} /> Sync &amp; draft
        </button>
        <button disabled={!!busy} onClick={() => void choose()}><FolderOpenIcon size={15} /> Use existing connection…</button>
      </div>
      {busy && <div className="chat-progress" role="status"><span className="chat-progress-line" />{busy}</div>}
      {!busy && current && <p className="sub spaced" role="status">{chatStatus(current)}</p>}
      {error && <p className="warning spaced" role="alert">{error}</p>}
    </>}
  </section>;
}
