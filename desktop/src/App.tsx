import { useEffect, useRef, useState, type ChangeEvent } from "react";
import {
  ArrowUpRightIcon as ArrowUpRight,
  MagnifyingGlassIcon,
  PaperPlaneTiltIcon,
  ArrowClockwiseIcon,
  XIcon,
  CheckIcon as Check,
  PenNibIcon as Feather,
  HardDriveIcon as HardDrive,
  EnvelopeSimpleIcon as Mail,
  PencilSimpleIcon as Pencil,
  GearSixIcon as Settings,
  UsersThreeIcon as UsersRound,
  UploadSimpleIcon as Upload,
  WarningCircleIcon as AlertCircle,
  CaretRightIcon as ChevronRight,
} from "@phosphor-icons/react";
import type { AppId, Draft, Page, Persona, Workspace } from "./types";
import {
  allowedRoles,
  searchDrafts,
  APP_LINKS,
  APP_NAMES,
  fillDetail,
  draftDetails,
  draftParts,
  parseWorkspace,
  placeholders,
} from "./lib/domain";
import { loadWorkspace, openExternal, saveWorkspace } from "./lib/storage";
import { sampleWorkspace } from "./lib/sample";
import ComposeMessage from "./components/ComposeMessage";
import RoleEditor from "./components/RoleEditor";
import AppMark from "./components/AppMark";
import DetailEditor, { DetailToken } from "./components/DetailEditor";
function message(error: unknown) {
  return error instanceof Error ? error.message : "Something went wrong.";
}
export default function App() {
  const [initial, setInitial] = useState<Workspace>(),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    loadWorkspace()
      .then((w) => {
        if (active) setInitial(w);
      })
      .catch((e) => {
        if (active) setError(message(e));
      });
    return () => {
      active = false;
    };
  }, []);
  if (error)
    return (
      <main className="startup">
        <Feather />
        <h1>We couldn’t open your workspace.</h1>
        <p>{error}</p>
        <p className="sub">Your saved file has not been overwritten.</p>
        <button
          onClick={() => {
            setError("");
            setInitial(sampleWorkspace());
          }}
        >
          Open a sample workspace instead
        </button>
      </main>
    );
  return initial ? (
    <Dashboard initial={initial} />
  ) : (
    <main className="startup">
      <Feather />
      <p>Opening your local workspace…</p>
    </main>
  );
}
function Dashboard({ initial }: { initial: Workspace }) {
  const [state, setState] = useState(initial),
    [page, setPage] = useState<Page>("drafts");
  const [app, setApp] = useState<AppId | "all">("gmail"),
    [draftId, setDraftId] = useState("office");
  const [roleEditor, setRoleEditor] = useState<Persona | "new" | null>(null),
    [detailEditor, setDetailEditor] = useState<{
      draft: Draft;
      key: string;
    } | null>(null);
  const [error, setError] = useState(""),
    [saved, setSaved] = useState(false),
    [notice, setNotice] = useState("");
  const [composing, setComposing] = useState(false);
  const importRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [mailBusy, setMailBusy] = useState(false),
    [sendingId, setSendingId] = useState("");
  const sendingRef = useRef(false);
  const [uncertainIds, setUncertainIds] = useState<string[]>([]);
  const [gmailAccounts, setGmailAccounts] = useState<string[]>([]);
  const [gmailAccount, setGmailAccount] = useState(
    initial.drafts.find((d) => d.delivery)?.delivery?.accountId || "",
  );
  async function refreshGmail() {
    if (!window.margin || mailBusy) return;
    if (
      state.drafts.some((d) => d.appId === "gmail" && d.localChanges) &&
      !window.confirm(
        "Refresh Gmail drafts? Local edits to Gmail drafts will be replaced with their current Gmail versions.",
      )
    )
      return;
    setMailBusy(true);
    try {
      const accounts = await window.margin.gmailAccounts();
      setGmailAccounts(accounts);
      if (!accounts.length)
        throw new Error(
          "No connected Gmail account found. Use your existing Gmail worker connection in Settings.",
        );
      const account =
        gmailAccount && accounts.includes(gmailAccount)
          ? gmailAccount
          : accounts.length === 1
            ? accounts[0]
            : "";
      if (!account) {
        setPage("settings");
        setNotice("Choose a Gmail account, then load its drafts.");
        return;
      }
      const result = await window.margin.gmailInbox(account);
      const roles =
        state.mode === "demo"
          ? state.roles.map((r) => ({
              ...r,
              usableExamples: 0,
              trainingReady: false,
            }))
          : state.roles;
      const incoming = result.drafts.map((d) => ({
        ...d,
        roleId: roles.some((r) => r.id === d.roleId) ? d.roleId : roles[0].id,
      }));
      const next = parseWorkspace({
        ...state,
        mode: "local",
        roles,
        connections: state.connections.map((c) => ({
          ...c,
          connected: c.id === "gmail" || (state.mode !== "demo" && c.connected),
        })),
        drafts: [
          ...(state.mode === "demo"
            ? state.drafts.filter((d) => d.composed)
            : state.drafts.filter((d) => d.appId !== "gmail" || d.composed)),
          ...incoming,
        ],
      });
      setState(next);
      setGmailAccount(result.accountId);
      selectApp("gmail");
      setQuery("");
      setNotice(
        `Loaded ${incoming.length} Gmail ${incoming.length === 1 ? "draft" : "drafts"} for ${result.accountId}.`,
      );
    } catch (e) {
      setError(message(e));
    } finally {
      setMailBusy(false);
    }
  }
  async function chooseGmailConnection() {
    if (!window.margin) return;
    try {
      const accounts = await window.margin.chooseGmailConnection();
      if (accounts) {
        setGmailAccounts(accounts);
        setGmailAccount(accounts.length === 1 ? accounts[0] : "");
        setNotice(
          accounts.length
            ? "Gmail connection selected. Load its drafts to review and send."
            : "This connection has no Gmail accounts. Connect Gmail with the local worker first.",
        );
      }
    } catch (e) {
      setError(message(e));
    }
  }
  async function sendReply(draft: Draft) {
    if (!window.margin || sendingRef.current) return;
    sendingRef.current = true;
    setSendingId(draft.id);
    try {
      await saveWorkspace(state);
      const result = await window.margin.sendDraft(draft.id);
      if (result.status === "sent") {
        setState((s) => ({
          ...s,
          drafts: s.drafts.filter((d) => d.id !== draft.id),
        }));
        setNotice(result.message || `Reply sent to ${draft.recipient}.`);
      } else if (result.status === "uncertain") {
        setUncertainIds((ids) => [...ids, draft.id]);
        setError(
          result.message ||
            "Delivery is unconfirmed. Check Gmail before taking further action.",
        );
      }
    } catch (e) {
      setError(message(e));
    } finally {
      sendingRef.current = false;
      setSendingId("");
    }
  }
  function sendUnavailable(draft: Draft) {
    if (draft.composed) return "New messages are saved locally. Copy this draft into your app to send it.";
    if (!window.margin) return "Sending is available in the Mac app.";
    if (draft.appId !== "gmail")
      return `${APP_NAMES[draft.appId]} sending is not connected yet.`;
    if (state.mode === "demo" || !draft.delivery)
      return "Load drafts from your connected Gmail account to send replies.";
    if (uncertainIds.includes(draft.id))
      return "Check Gmail to confirm delivery. This draft will not be resent.";
    if (placeholders(draft.body).length)
      return "Fill every missing detail before sending.";
    if (!state.connections.find((c) => c.id === draft.appId)?.connected)
      return "Reconnect Gmail before sending.";
    if (!draft.body.trim()) return "This draft is empty.";
    return "";
  }
  useEffect(() => {
    let active = true;
    setSaved(false);
    saveWorkspace(state)
      .then(() => {
        if (active) {
          setSaved(true);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError("Could not save: " + message(e));
      });
    return () => {
      active = false;
    };
  }, [state]);
  const appDrafts = state.drafts.filter(
    (d) => app === "all" || d.appId === app,
  );
  const drafts = searchDrafts(appDrafts, query, state.roles);
  const activeDraft = drafts.find((d) => d.id === draftId) || drafts[0];
  const details = activeDraft ? draftDetails(activeDraft) : [];
  const pendingDetails = details.filter((detail) => !detail.value);
  const connection = state.connections.find((c) => c.id === app);
  const activeRole = state.roles.find((r) => r.id === activeDraft?.roleId);
  function selectApp(id: AppId | "all") {
    setApp(id);
    setDraftId("");
    setPage("drafts");
    setNotice("");
  }
  async function investigate(draft: Draft) {
    try {
      await openExternal(draft.externalUrl || APP_LINKS[draft.appId]);
    } catch (e) {
      setError(message(e));
    }
  }
  function saveRole(role: Persona) {
    setState((s) => ({
      ...s,
      roles: s.roles.some((r) => r.id === role.id)
        ? s.roles.map((r) => (r.id === role.id ? role : r))
        : [...s.roles, role],
    }));
    setRoleEditor(null);
  }
  async function importSnapshot(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      if (file.size > 10 * 1024 * 1024)
        throw new Error("Choose a JSON snapshot smaller than 10 MB.");
      const imported = parseWorkspace(JSON.parse(await file.text()));
      if (
        !window.confirm(
          "Replace this workspace with the imported data? This replaces the drafts, personas and preferences shown here.",
        )
      )
        return;
      setState({ ...imported, mode: "local" });
      setNotice(
        "Local workspace imported. Nothing was sent to an external service.",
      );
    } catch (e) {
      setError("Import failed: " + message(e));
    }
  }
  const count = (id: AppId | "all") =>
    state.drafts.filter((d) => id === "all" || d.appId === id).length;
  return (
    <div className="app">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to workspace
      </a>
      <header className="top">
        <div className="row">
          <span className="brand">
            <span className="brand-mark" aria-hidden="true">
              m<span />
            </span>
            margin
          </span>
          <span className="rule" />
          <span className="tagline">Your writing, in every context.</span>
        </div>
        <div className="row">
          <span className="workspace-mode">
            {state.mode === "demo" ? "Sample workspace" : "Local workspace"}
          </span>
          <span className="local-status">
            <HardDrive size={15} />
            On your Mac
          </span>
        </div>
      </header>
      {error && (
        <div className="banner error" role="alert">
          <AlertCircle size={16} />
          <span>{error}</span>
          <button onClick={() => setError("")} aria-label="Dismiss error">
            Dismiss
          </button>
        </div>
      )}
      {notice && (
        <div className="banner" role="status">
          {notice}
          <button onClick={() => setNotice("")}>Dismiss</button>
        </div>
      )}
      <div className="layout">
        <aside className="sidebar">
          <span className="overline side-heading">Your apps</span>
          <nav aria-label="Filter drafts by app">
            {(
              [
                { id: "all", name: "All apps", connected: true },
                ...state.connections,
              ] as const
            ).map((c) => (
              <button
                key={c.id}
                className="nav-app"
                aria-pressed={page === "drafts" && app === c.id}
                onClick={() => selectApp(c.id)}
              >
                <AppMark app={c.id} />
                <span className="nav-info">
                  <strong>{c.name}</strong>
                  <small>
                    {c.id === "all"
                      ? "All your drafts"
                      : c.connected
                        ? state.mode === "demo"
                          ? "Sample"
                          : "Connected"
                        : "Not connected"}
                  </small>
                </span>
                <span className="count">{count(c.id)}</span>
              </button>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <button
              className="side-action"
              aria-pressed={page === "settings"}
              onClick={() => setPage("settings")}
            >
              <Settings size={17} />
              Settings
            </button>
            <button
              className="side-action"
              aria-pressed={page === "roles"}
              onClick={() => setPage("roles")}
            >
              <UsersRound size={17} />
              Edit your personas
            </button>
            <p className="sub">
              {saved ? (
                <>
                  <Check size={13} />
                  Saved on this device
                </>
              ) : (
                "Saving locally…"
              )}
            </p>
          </div>
        </aside>
        <main
          className={`workspace workspace-${page}`}
          id="main-content"
          tabIndex={-1}
        >
          {page === "drafts" && (
            <>
              <section className="page-head">
                <div className="row between">
                  <div>
                    <p className="overline">Drafts</p>
                    <h1>{app === "all" ? "All apps" : APP_NAMES[app]}</h1>
                  </div>
                  <div className="row">
                    <button className="primary" onClick={() => setComposing(true)} disabled={!!sendingId || mailBusy}>
                      <Pencil size={16} /> Draft a message
                    </button>
                  <span className="automation-status">
                    <span
                      className={`status-dot${state.preferences.automaticDrafts ? " on" : ""}`}
                    />
                    {state.preferences.automaticDrafts
                      ? "Automatic drafts on"
                      : "Manual drafting"}
                  </span>
                  </div>
                </div>
                <p className="description">
                  {app === "all"
                    ? "A message for every part of your life."
                    : `Review your drafts, then pick up the conversation in ${APP_NAMES[app]}.`}
                </p>
                <div className="role-chips">
                  <span className="sub">
                    {app === "all"
                      ? "Personas across your apps:"
                      : "Allowed Personas:"}
                  </span>
                  {allowedRoles(state.roles, app).map((r) => (
                    <span key={r.id} className="role-label">
                      {r.name}
                    </span>
                  ))}
                  {!allowedRoles(state.roles, app).length && (
                    <span className="sub">None allowed</span>
                  )}
                  <button className="ghost" onClick={() => setPage("roles")}>
                    <Pencil size={14} />
                    Edit your personas
                  </button>
                </div>
              </section>
              <div className="inbox-toolbar">
                <div className="inbox-search">
                  <MagnifyingGlassIcon size={17} aria-hidden="true" />
                  <input
                    type="search"
                    aria-label="Search inbox"
                    placeholder="Search people, messages, personas…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                  {query && (
                    <button
                      className="ghost"
                      aria-label="Clear search"
                      onClick={() => setQuery("")}
                    >
                      <XIcon size={14} />
                    </button>
                  )}
                </div>
                {window.margin && (app === "gmail" || app === "all") && (
                  <button
                    disabled={mailBusy || !!sendingId}
                    onClick={() => void refreshGmail()}
                  >
                    <ArrowClockwiseIcon size={15} />
                    {mailBusy ? "Loading Gmail…" : "Refresh Gmail"}
                  </button>
                )}
              </div>
              {activeDraft && activeRole ? (
                <div className="draft-layout">
                  <section className="draft-list" aria-label="Current drafts">
                    <div className="row between list-heading">
                      <h2>Current drafts</h2>
                      <span className="sub">{drafts.length}</span>
                    </div>
                    {drafts.map((d) => {
                      const role = state.roles.find((r) => r.id === d.roleId)!;
                      const tokens = draftDetails(d).filter(
                        (detail) => !detail.value,
                      );
                      return (
                        <button
                          key={d.id}
                          className="draft-item"
                          data-app={d.appId}
                          aria-pressed={activeDraft.id === d.id}
                          onClick={() => setDraftId(d.id)}
                        >
                          <div className="row between">
                            <span className="draft-meta">
                              {app === "all" && (
                                <span className="app-tag" data-app={d.appId}>
                                  <AppMark app={d.appId} size={12} />
                                  {APP_NAMES[d.appId]}
                                </span>
                              )}
                              <span className="draft-role">{role.name}</span>
                            </span>
                            <span className="sub">
                              {new Date(d.updatedAt).toLocaleDateString(
                                "en-US",
                                { month: "short", day: "numeric" },
                              )}
                            </span>
                          </div>
                          <strong>{d.subject}</strong>
                          <span className="sub">To {d.recipient}</span>
                          <p className="excerpt">
                            {d.body.replaceAll("\n", " ")}
                          </p>
                          <span className={tokens.length ? "warning" : "sub"}>
                            {tokens.length
                              ? `${tokens.length} ${tokens.length === 1 ? "detail" : "details"} to fill`
                              : d.localChanges
                                ? "Updated locally"
                                : "Ready for review"}
                          </span>
                        </button>
                      );
                    })}
                  </section>
                  <section className="draft-detail" aria-label="Draft preview">
                    <div className="row between">
                      <span className="overline">Draft preview</span>
                      <span
                        className="badge draft-source"
                        data-app={activeDraft.appId}
                      >
                        <AppMark app={activeDraft.appId} size={14} />
                        {app === "all" && `${APP_NAMES[activeDraft.appId]} · `}
                        {activeDraft.composed
                          ? "New message · saved locally"
                          : activeDraft.localChanges
                          ? "Updated locally"
                          : state.mode === "demo"
                            ? "Sample draft"
                            : APP_NAMES[activeDraft.appId]}
                      </span>
                    </div>
                    <h2>{activeDraft.subject}</h2>
                    <p className="address">
                      To {activeDraft.recipient} · {activeDraft.address}
                    </p>
                    {activeDraft.context && <p className="sub context">
                      In reply to: “{activeDraft.context}”
                    </p>}
                    {(activeRole.excludedApps.includes(activeDraft.appId) ||
                      !state.connections.find((c) => c.id === activeDraft.appId)
                        ?.connected) && (
                      <p className="warning spaced">
                        Existing draft · future drafting is paused for this persona
                        in this app.
                      </p>
                    )}
                    <div className="draft-body">
                      {draftParts(activeDraft).map((part, index) =>
                        part.detail ? (
                          <DetailToken
                            key={index}
                            token={part.detail.token}
                            value={part.detail.value}
                            disabled={!!sendingId || mailBusy}
                            onClick={() =>
                              setDetailEditor({
                                draft: activeDraft,
                                key: part.detail!.key,
                              })
                            }
                          />
                        ) : (
                          <span key={index}>{part.text}</span>
                        ),
                      )}
                    </div>
                    {details.length > 0 && (
                      <div className="time-prompt">
                        <div>
                          <h3>
                            {pendingDetails.length
                              ? `${pendingDetails.length} ${pendingDetails.length === 1 ? "detail" : "details"} to fill`
                              : "Details filled"}
                          </h3>
                          <p className="sub">
                            {pendingDetails.length
                              ? "Click each highlighted detail to fill it before sending."
                              : "Click an inserted detail to change it."}
                          </p>
                        </div>
                        {pendingDetails.length > 0 && (
                          <button
                            disabled={!!sendingId || mailBusy}
                            onClick={() =>
                              setDetailEditor({
                                draft: activeDraft,
                                key: pendingDetails[0].key,
                              })
                            }
                          >
                            Fill next detail
                          </button>
                        )}
                      </div>
                    )}
                    <footer className="draft-footer">
                      <div className="row between">
                        <span className="voice-label">
                          <UsersRound size={14} />
                          {activeRole.name} voice
                        </span>
                        {activeDraft.composed && <button onClick={async () => {
                          try { if (window.margin) await window.margin.copyDraft(activeDraft.id); else await navigator.clipboard.writeText(activeDraft.body); setNotice("Draft copied. Paste it into your app to send."); }
                          catch { setError("Could not copy. Select and copy the draft text instead."); }
                        }}>Copy draft</button>}
                        <div className="row draft-actions">
                          <button onClick={() => void investigate(activeDraft)}>
                            Open in {APP_NAMES[activeDraft.appId]}
                            <ArrowUpRight size={16} />
                          </button>
                          <button
                            className="primary"
                            disabled={
                              !!sendUnavailable(activeDraft) ||
                              !!sendingId ||
                              mailBusy
                            }
                            title={
                              sendUnavailable(activeDraft) ||
                              "Review and send this reply"
                            }
                            onClick={() => void sendReply(activeDraft)}
                          >
                            <PaperPlaneTiltIcon size={16} />
                            {sendingId === activeDraft.id ? "Sending…" : "Send"}
                          </button>
                        </div>
                      </div>
                      <p className="link-note">
                        {sendUnavailable(activeDraft) ||
                          "You’ll review the recipient and message before sending."}
                      </p>
                      <p className="link-note">
                        {activeDraft.localChanges
                          ? "This edit is saved locally. Live app sync is not connected yet."
                          : activeDraft.externalUrl
                            ? "Review, edit and send in the original app."
                            : `No specific draft link supplied. Opens ${APP_NAMES[activeDraft.appId] === "Gmail" ? "Gmail’s Drafts folder" : APP_NAMES[activeDraft.appId]}.`}
                      </p>
                    </footer>
                  </section>
                </div>
              ) : (
                <section className="empty">
                  <Mail size={28} />
                  <h2>
                    {query.trim()
                      ? "No matching drafts."
                      : connection && !connection.connected
                        ? `Connect ${connection.name} to see your drafts.`
                        : "You’re all caught up."}
                  </h2>
                  <p className="description">
                    {query.trim()
                      ? `No drafts match “${query.trim()}” in ${app === "all" ? "All apps" : APP_NAMES[app]}. Try another person or phrase.`
                      : connection && !connection.connected
                        ? "All your personas are allowed by default. You can add exceptions in Edit your personas."
                        : "Drafts for messages likely to need a reply will appear here. Manual drafting stays available in your apps."}
                  </p>
                  {query.trim() ? (
                    <button onClick={() => setQuery("")}>Clear search</button>
                  ) : (
                    <button onClick={() => setPage("settings")}>
                      Manage connections
                      <ChevronRight size={15} />
                    </button>
                  )}
                </section>
              )}
            </>
          )}
          {page === "roles" && (
            <>
              <section className="page-head">
                <div className="row between">
                  <div>
                    <p className="overline">Personalization</p>
                    <h1>Edit your personas</h1>
                  </div>
                </div>
                <p className="description">
                  Give each part of your life its own voice. Every persona uses all
                  connected apps by default.
                </p>
              </section>
              <section className="roles-list">
                {state.roles.map((r) => (
                  <article className="role-row" key={r.id}>
                    <div className="row between">
                      <div className="row">
                        <span className="initial">{r.name[0]}</span>
                        <div>
                          <h2>{r.name}</h2>
                          <p className="sub">
                            {r.kind === "default"
                              ? "Default persona"
                              : "Custom persona"}{" "}
                            ·{" "}
                            {r.trainingReady
                              ? "Voice ready"
                              : `${r.usableExamples} examples · collecting data`}
                          </p>
                        </div>
                      </div>
                      <button onClick={() => setRoleEditor(r)}>
                        <Pencil size={14} />
                        Edit persona
                      </button>
                    </div>
                    <p className="description">{r.description}</p>
                    <div className="row spaced">
                      {state.connections.map((c) => (
                        <span className="chip" key={c.id}>
                          {c.name}
                          <span className="sub">
                            {r.excludedApps.includes(c.id)
                              ? "Excluded"
                              : "Allowed"}
                          </span>
                        </span>
                      ))}
                    </div>
                  </article>
                ))}
              </section>
            </>
          )}
          {page === "settings" && (
            <>
              <section className="page-head">
                <p className="overline">Preferences</p>
                <h1>Settings</h1>
                <p className="description">
                  Your accounts, drafting preferences and local data.
                </p>
              </section>
              <section className="settings-body">
                <h2>Connected apps</h2>
                <p className="sub spaced">
                  Connect once. Every persona is included unless you add an
                  exception.
                </p>
                {state.connections.map((c) => (
                  <div className="option-row" key={c.id}>
                    <div className="row">
                      <AppMark
                        app={c.id}
                        size={24}
                        className="connection-mark"
                      />
                      <div>
                        <h3>{c.name}</h3>
                        <small>
                          {c.connected
                            ? state.mode === "demo"
                              ? "Sample connection"
                              : "Connected via imported workspace"
                            : "Not connected"}
                        </small>
                      </div>
                    </div>
                    <button
                      disabled={state.mode !== "demo"}
                      onClick={() =>
                        setState((s) => ({
                          ...s,
                          connections: s.connections.map((a) =>
                            a.id === c.id
                              ? { ...a, connected: !a.connected }
                              : a,
                          ),
                        }))
                      }
                    >
                      {state.mode === "demo"
                        ? `${c.connected ? "Disconnect" : "Connect"} · demo`
                        : "Connector managed"}
                    </button>
                  </div>
                ))}
                <p className="sub spaced">
                  {state.mode === "demo"
                    ? "Sample connections don’t access real accounts."
                    : "Live account linking is handled by your local connector."}
                </p>
                {window.margin && (
                  <section className="gmail-inbox-settings">
                    <h2 className="section-title">Gmail inbox</h2>
                    <p className="sub spaced">
                      Load drafts from your existing Gmail connection to review
                      and send them here.
                    </p>
                    {!!gmailAccounts.length && (
                      <label className="field">
                        Gmail account
                        <select
                          value={gmailAccount}
                          onChange={(e) => setGmailAccount(e.target.value)}
                        >
                          <option value="">Choose an account</option>
                          {gmailAccounts.map((account) => (
                            <option key={account} value={account}>
                              {account}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}
                    <div className="row spaced">
                      <button
                        disabled={mailBusy || !!sendingId}
                        onClick={() => void refreshGmail()}
                      >
                        {mailBusy ? "Loading Gmail…" : "Load Gmail drafts"}
                      </button>
                      <button
                        disabled={mailBusy || !!sendingId}
                        onClick={() => void chooseGmailConnection()}
                      >
                        Use existing connection…
                      </button>
                    </div>
                  </section>
                )}
                <h2 className="section-title">Drafting</h2>
                <label className="option-row">
                  <span>
                    <h3>Automatic drafts</h3>
                    <small>
                      Only for messages the model predicts need a reply.
                    </small>
                  </span>
                  <input
                    type="checkbox"
                    checked={state.preferences.automaticDrafts}
                    onChange={(e) =>
                      setState((s) => ({
                        ...s,
                        preferences: {
                          ...s.preferences,
                          automaticDrafts: e.target.checked,
                        },
                      }))
                    }
                  />
                </label>
                <p className="sub spaced">
                  This preference is saved locally. “Draft in my voice” stays
                  available through the app integration.
                </p>
                <h2 className="section-title">Workspace data</h2>
                <p className="sub spaced">
                  {state.mode === "demo"
                    ? "Exploring sample data. No email or Slack account is connected."
                    : "Using a local snapshot. Draft text and persona changes stay on this device."}
                </p>
                <div className="row spaced">
                  <button onClick={() => importRef.current?.click()}>
                    <Upload size={15} />
                    Import local snapshot
                  </button>
                  <input
                    ref={importRef}
                    hidden
                    type="file"
                    accept="application/json,.json"
                    onChange={(e) => void importSnapshot(e)}
                  />
                  <button
                    onClick={() => {
                      if (
                        window.confirm(
                          "Replace the current workspace with sample data? Unsynced local edits will be replaced.",
                        )
                      ) {
                        setState(sampleWorkspace());
                        setNotice("Sample workspace loaded.");
                      }
                    }}
                  >
                    Load sample data
                  </button>
                </div>
              </section>
            </>
          )}
        </main>
      </div>
      {composing && <ComposeMessage
        personas={state.roles}
        initialApp={app === "all" ? "gmail" : app}
        onClose={() => setComposing(false)}
        onSave={(draft) => {
          setState(s => parseWorkspace({...s, drafts: [draft, ...s.drafts]}));
          setApp(draft.appId); setDraftId(draft.id); setQuery(""); setComposing(false);
          setNotice("New message drafted locally. Review its details, then copy it into your app.");
        }}
      />}
      {roleEditor && (
        <RoleEditor
          key={roleEditor === "new" ? "new" : roleEditor.id}
          role={roleEditor === "new" ? undefined : roleEditor}
          connections={state.connections}
          roles={state.roles}
          onClose={() => setRoleEditor(null)}
          onSave={saveRole}
        />
      )}
      {detailEditor && (
        <DetailEditor
          key={`${detailEditor.draft.id}:${detailEditor.key}`}
          draft={detailEditor.draft}
          detailKey={detailEditor.key}
          onClose={() => setDetailEditor(null)}
          onSave={(value, input) => {
            const current = state.drafts.find(
              (draft) => draft.id === detailEditor.draft.id,
            );
            if (!current || current.body !== detailEditor.draft.body)
              throw new Error(
                "The draft changed while you were editing. Open the detail again.",
              );
            const updated = fillDetail(current, detailEditor.key, value, input);
            setState((s) => ({
              ...s,
              drafts: s.drafts.map((draft) =>
                draft.id === updated.id ? updated : draft,
              ),
            }));
            setDetailEditor(null);
            setNotice(
              "Detail saved on this device. Sending from Margin includes your changes.",
            );
          }}
        />
      )}
    </div>
  );
}
