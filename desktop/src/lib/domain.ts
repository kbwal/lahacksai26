import type {
  AppId,
  Draft,
  Persona,
  TimeChoice,
  Workspace,
  PlaceholderEdits,
  PersonalDetails,
} from "../types";
export const APP_IDS: AppId[] = ["gmail", "slack", "discord", "outlook"];
export const APP_NAMES = { gmail: "Gmail", slack: "Slack", discord: "Discord", outlook: "Outlook" };
export const APP_LINKS = {
  gmail: "https://mail.google.com/mail/u/0/#drafts",
  slack: "https://app.slack.com",
  discord: "https://discord.com/channels/@me",
  outlook: "https://outlook.live.com/mail/0/drafts",
};
const HOSTS = [
  "mail.google.com",
  "app.slack.com",
  "discord.com",
  "outlook.live.com",
  "outlook.office.com",
  "outlook.office365.com",
];
export function isAllowedExternalUrl(value: string): boolean {
  try {
    const u = new URL(value);
    return (
      u.protocol === "https:" &&
      !u.username &&
      !u.password &&
      !u.port &&
      HOSTS.includes(u.hostname)
    );
  } catch {
    return false;
  }
}
export function normalizeComposedBody(body: string): string {
  return body.replace(/\[([A-Za-z][A-Za-z0-9_ '-]{0,79})\]/g, (_, label: string) => {
    const upper = label.trim().toUpperCase();
    const unprefixed = upper.replace(/^YOUR[ _]+/, "");
    const token = ["DATE", "TIME", "AMOUNT", "PHONE", "ADDRESS", "LINK"].includes(unprefixed) ? unprefixed : upper;
    return `[${token}]`;
  });
}
export function placeholders(text: string): string[] {
  return [...new Set(text.match(/\[[A-Z][A-Z0-9_ '-]*\]/g) || [])];
}
export function searchDrafts(
  drafts: Draft[],
  query: string,
  roles: Persona[],
): Draft[] {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return drafts;
  return drafts.filter((draft) => {
    const text = [
      draft.subject,
      draft.recipient,
      draft.address,
      draft.body,
      draft.context,
      APP_NAMES[draft.appId],
      roles.find((role) => role.id === draft.roleId)?.name || "",
    ]
      .join(" ")
      .toLocaleLowerCase();
    return words.every((word) => text.includes(word));
  });
}
export function allowedRoles(roles: Persona[], app: AppId | "all"): Persona[] {
  return roles.filter((r) =>
    app === "all"
      ? APP_IDS.some((id) => !r.excludedApps.includes(id))
      : !r.excludedApps.includes(app),
  );
}
export function formatTime({ date, time }: TimeChoice): string {
  if (!/^\d{2}:\d{2}$/.test(time)) throw new Error("Choose a valid time.");
  const [hour, minute] = time.split(":").map(Number);
  if (hour > 23 || minute > 59) throw new Error("Choose a valid time.");
  const clock = `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${hour < 12 ? "AM" : "PM"}`;
  if (!date) return clock;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date))
    throw new Error("Choose a valid date.");
  const day = new Date(date + "T12:00:00");
  if (
    !Number.isFinite(day.getTime()) ||
    [
      day.getFullYear(),
      String(day.getMonth() + 1).padStart(2, "0"),
      String(day.getDate()).padStart(2, "0"),
    ].join("-") !== date
  )
    throw new Error("Choose a valid date.");
  return `${day.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" })} at ${clock}`;
}
export function fillTime(draft: Draft, choice: TimeChoice): Draft {
  const template = draft.timeEdit?.template ?? draft.body;
  if (!template.includes("[TIME]"))
    throw new Error("This draft has no [TIME] placeholder.");
  if (
    draft.timeEdit &&
    template.replaceAll("[TIME]", draft.timeEdit.rendered) !== draft.body
  )
    throw new Error(
      "This draft changed since its time was inserted. Refresh it before editing the time.",
    );
  const rendered = formatTime(choice);
  return {
    ...draft,
    body: template.replaceAll("[TIME]", rendered),
    timeEdit: { template, choice: { ...choice }, rendered },
    localChanges: true,
  };
}
export interface DraftDetail {
  key: string;
  token: string;
  value?: string;
  input?: string;
}
const tokenPattern = () => /\[[A-Z][A-Z0-9_ '-]*\]/g;
export function detailEdits(draft: Draft): PlaceholderEdits {
  if (draft.placeholderEdits) return draft.placeholderEdits;
  let template = draft.timeEdit?.template ?? draft.body;
  const values: PlaceholderEdits["values"] = {};
  if (draft.timeEdit) {
    const legacy = draft.timeEdit;
    const clock = formatTime({ date: "", time: legacy.choice.time });
    const dateText = legacy.choice.date
      ? legacy.rendered.slice(0, -(clock.length + 4))
      : "";
    let index = 0;
    template = template.replace(tokenPattern(), (token) => {
      if (token !== "[TIME]") { index++; return token; }
      if (dateText) {
        values[String(index++)] = { value: dateText, input: legacy.choice.date };
      }
      values[String(index++)] = { value: clock, input: legacy.choice.time };
      return dateText ? "[DATE] at [TIME]" : "[TIME]";
    });
  }
  return { template, values };
}
export function renderDetails(edits: PlaceholderEdits): string {
  let index = 0;
  return edits.template.replace(
    tokenPattern(),
    (token) => edits.values[String(index++)]?.value ?? token,
  );
}
export function draftDetails(draft: Draft): DraftDetail[] {
  const edits = detailEdits(draft);
  return Array.from(
    edits.template.matchAll(tokenPattern()),
    (match, index) => ({
      key: String(index),
      token: match[0],
      ...edits.values[String(index)],
    }),
  );
}
export function draftParts(
  draft: Draft,
): { text: string; detail?: DraftDetail }[] {
  const edits = detailEdits(draft),
    details = draftDetails(draft);
  const parts: { text: string; detail?: DraftDetail }[] = [];
  let end = 0,
    index = 0;
  for (const match of edits.template.matchAll(tokenPattern())) {
    parts.push({ text: edits.template.slice(end, match.index) });
    const detail = details[index++];
    parts.push({ text: detail.value ?? detail.token, detail });
    end = match.index! + match[0].length;
  }
  parts.push({ text: edits.template.slice(end) });
  return parts;
}
export function detailValueError(token: string, value: string): string {
  if (!value.trim()) return "Enter a value before inserting it.";
  if (value.length > 4000) return "Keep this detail under 4,000 characters.";
  if (placeholders(value).length)
    return "Enter the actual detail, without placeholders.";
  if (token === "[LINK]") {
    if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return "";
    try {
      if (/\s/.test(value)) throw new Error();
      const hasScheme = /^[a-z][a-z0-9+.-]*:/i.test(value);
      const url = new URL(hasScheme ? value : `https://${value}`);
      if (
        !["http:", "https:"].includes(url.protocol) ||
        !url.hostname ||
        (!hasScheme && !url.hostname.includes(".")) ||
        url.username ||
        url.password
      )
        throw new Error();
    } catch {
      return "Enter a website, meeting link, or email address.";
    }
  }
  return "";
}
export function fillDetail(
  draft: Draft,
  key: string,
  value: string,
  input?: string,
): Draft {
  const edits = detailEdits(draft),
    detail = draftDetails(draft).find((item) => item.key === key);
  if (!detail) throw new Error("This detail is no longer in the draft.");
  if (renderDetails(edits) !== draft.body)
    throw new Error(
      "This draft changed. Refresh it before editing its details.",
    );
  const error = detailValueError(detail.token, value.trim());
  if (error) throw new Error(error);
  const placeholderEdits = {
    template: edits.template,
    values: {
      ...edits.values,
      [key]: { value: value.trim(), ...(input === undefined ? {} : { input }) },
    },
  };
  if (Object.keys(placeholderEdits.values).length > 1000)
    throw new Error("Too many filled details.");
  const body = renderDetails(placeholderEdits);
  if (body.length > 60000)
    throw new Error("This detail makes the draft too long.");
  const { timeEdit: _legacy, ...rest } = draft;
  return { ...rest, body, placeholderEdits, localChanges: true };
}
export const EMPTY_PERSONAL_DETAILS: PersonalDetails = {
  autoFill: false, name: "", email: "", phone: "", address: "", website: "",
};
export function personalDetailsError(details: PersonalDetails): string {
  for (const key of ["name", "email", "phone", "address", "website"] as const) {
    if (details[key].length > 1000 || details[key].includes("\0") || placeholders(normalizeComposedBody(details[key])).length)
      return "Use actual personal details, under 1,000 characters per field.";
  }
  if (details.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(details.email))
    return "Enter a valid email address.";
  if (details.website && detailValueError("[LINK]", details.website))
    return "Enter a valid website address.";
  return "";
}
export function applyPersonalDetails(draft: Draft, profile?: PersonalDetails): Draft {
  if (!profile?.autoFill) return draft;
  let result = draft;
  for (const detail of draftDetails(draft)) {
    if (detail.value) continue;
    const label = detail.token.slice(1, -1).replaceAll("_", " ").replace(/^YOUR /, "");
    const fields: Record<string, keyof Omit<PersonalDetails, "autoFill">> = {
      NAME: "name", "FULL NAME": "name", "SENDER NAME": "name",
      EMAIL: "email", "EMAIL ADDRESS": "email", PHONE: "phone", "PHONE NUMBER": "phone",
      ADDRESS: "address", "POSTAL ADDRESS": "address", WEBSITE: "website", "PERSONAL WEBSITE": "website",
    };
    const field = fields[label];
    if (field && profile[field].trim()) {
      try { result = fillDetail(result, detail.key, profile[field]); }
      catch { /* Keep the field unresolved when inserting it would exceed draft limits. */ }
    }
  }
  return result;
}
export function applyWorkspaceDetails(workspace: Workspace): Workspace {
  return {...workspace, drafts: workspace.drafts.map(d => applyPersonalDetails(d, workspace.personalDetails))};
}
export function roleFormErrors(
  name: string,
  description: string,
  roles: Persona[],
  editingId?: string,
): { name?: string; description?: string } {
  const errors: { name?: string; description?: string } = {};
  if (!name.trim()) errors.name = "Enter a persona name.";
  else if (
    roles.some(
      (r) =>
        r.id !== editingId &&
        r.name.trim().toLocaleLowerCase() === name.trim().toLocaleLowerCase(),
    )
  )
    errors.name =
      "A persona with this name already exists. Choose a different name.";
  if (!description.trim())
    errors.description = "Describe when to use this voice.";
  return errors;
}
function object(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v))
    throw new Error("Expected a JSON object.");
  return v as Record<string, unknown>;
}
function str(v: unknown, label: string, max = 1000): string {
  if (typeof v !== "string" || v.length > max)
    throw new Error(`Invalid ${label}.`);
  return v;
}
function bool(v: unknown): boolean {
  if (typeof v !== "boolean") throw new Error("Expected a boolean.");
  return v;
}
function array(v: unknown, max: number): unknown[] {
  if (!Array.isArray(v) || v.length > max)
    throw new Error("Invalid array length.");
  return v;
}
function appId(v: unknown): AppId {
  if (!APP_IDS.includes(v as AppId)) throw new Error("Unknown app.");
  return v as AppId;
}
function unique(items: { id: string }[]) {
  if (new Set(items.map((i) => i.id)).size !== items.length)
    throw new Error("Duplicate IDs in workspace.");
}
export function parseWorkspace(input: unknown): Workspace {
  const w = object(input);
  if (w.version !== 1 || !["demo", "local"].includes(w.mode as string))
    throw new Error("Unsupported workspace format.");
  const connections = array(w.connections, 4).map((v) => {
    const a = object(v),
      id = appId(a.id);
    return { id, name: APP_NAMES[id], connected: bool(a.connected) };
  });
  unique(connections);
  if (!connections.some(c => c.id === "discord"))
    connections.push({ id: "discord", name: "Discord", connected: false });
  if (connections.length !== 4)
    throw new Error("Include Gmail, Slack, Discord and Outlook connection records.");
  connections.sort((a, b) => APP_IDS.indexOf(a.id) - APP_IDS.indexOf(b.id));
  const roles = array(w.roles, 30).map((v) => {
    const r = object(v),
      id = str(r.id, "role ID", 100),
      name = str(r.name, "role name", 50).trim();
    if (
      !id ||
      !name ||
      !["default", "custom"].includes(r.kind as string) ||
      !Number.isInteger(r.usableExamples) ||
      (r.usableExamples as number) < 0
    )
      throw new Error("Invalid persona.");
    return {
      id,
      name,
      description: str(r.description, "description", 300),
      kind: r.kind as Persona["kind"],
      excludedApps: [...new Set(array(r.excludedApps, 4).map(appId))],
      usableExamples: r.usableExamples as number,
      trainingReady: bool(r.trainingReady),
    };
  });
  const removedAcademic = roles.find((r) => r.id === "academic" && r.kind === "default");
  if (removedAcademic && roles.length > 1) roles.splice(roles.indexOf(removedAcademic), 1);
  else if (removedAcademic) removedAcademic.kind = "custom";
  unique(roles);
  if (!roles.length) throw new Error("A workspace needs at least one persona.");
  const drafts = array(w.drafts, 500).map((v) => {
    const d = object(v),
      roleId = removedAcademic && d.roleId === removedAcademic.id && removedAcademic.kind === "default"
        ? (roles.find((r) => r.id === "professional") || roles[0]).id
        : str(d.roleId, "draft persona", 100),
      id = str(d.id, "draft ID", 100);
    if (!id || !roles.some((r) => r.id === roleId))
      throw new Error("Draft refers to an unknown persona.");
    const externalUrl =
      d.externalUrl === undefined
        ? undefined
        : str(d.externalUrl, "draft link", 4000);
    if (externalUrl && !isAllowedExternalUrl(externalUrl))
      throw new Error(
        "Draft link must be an official Gmail, Slack, Discord or Outlook HTTPS URL.",
      );
    const rawBody = str(d.body, "draft body", 60000);
    const body = d.composed === true && !d.placeholderEdits ? normalizeComposedBody(rawBody) : rawBody;
    let timeEdit: Draft["timeEdit"];
    if (d.timeEdit !== undefined) {
      const edit = object(d.timeEdit),
        choice = object(edit.choice);
      timeEdit = {
        template: str(edit.template, "time template", 60000),
        rendered: str(edit.rendered, "inserted time", 200),
        choice: {
          date: str(choice.date, "time date", 10),
          time: str(choice.time, "time value", 5),
        },
      };
      if (
        !timeEdit.template.includes("[TIME]") ||
        formatTime(timeEdit.choice) !== timeEdit.rendered ||
        timeEdit.template.replaceAll("[TIME]", timeEdit.rendered) !== body
      )
        throw new Error("Invalid inserted time metadata.");
    }
    let placeholderEdits: Draft["placeholderEdits"];
    if (d.placeholderEdits !== undefined) {
      if (timeEdit) throw new Error("Conflicting placeholder metadata.");
      const saved = object(d.placeholderEdits),
        values = object(saved.values);
      const template = str(saved.template, "detail template", 60000);
      const tokens = Array.from(
        template.matchAll(tokenPattern()),
        (match) => match[0],
      );
      if (Object.keys(values).length > 1000)
        throw new Error("Too many filled details.");
      placeholderEdits = { template, values: {} };
      for (const [key, raw] of Object.entries(values)) {
        if (!/^(0|[1-9]\d*)$/.test(key) || !tokens[Number(key)])
          throw new Error("Invalid detail index.");
        const fill = object(raw),
          value = str(fill.value, "filled detail", 4000);
        if (detailValueError(tokens[Number(key)], value))
          throw new Error("Invalid filled detail.");
        placeholderEdits.values[key] = {
          value,
          ...(fill.input === undefined
            ? {}
            : { input: str(fill.input, "detail input", 4000) }),
        };
      }
      if (renderDetails(placeholderEdits) !== body)
        throw new Error("Invalid filled detail metadata.");
    }
    let delivery: Draft["delivery"];
    if (d.delivery !== undefined) {
      const source = object(d.delivery);
      delivery = {
        accountId: str(source.accountId, "Gmail account", 320),
        generationId: source.generationId as number,
        gmailDraftId: str(source.gmailDraftId, "Gmail draft ID", 500),
        snapshot: str(source.snapshot, "Gmail snapshot", 64),
      };
      if (
        d.appId !== "gmail" ||
        !delivery.accountId ||
        !delivery.gmailDraftId ||
        !Number.isSafeInteger(delivery.generationId) ||
        delivery.generationId < 1 ||
        !/^[a-f0-9]{64}$/.test(delivery.snapshot)
      )
        throw new Error("Invalid Gmail delivery metadata.");
    }
    let chat: Draft["chat"];
    if (d.chat !== undefined) {
      const ref = object(d.chat);
      if (!["slack", "discord"].includes(ref.source as string) || ref.source !== d.appId)
        throw new Error("Invalid chat source.");
      chat = {
        source: ref.source as "slack" | "discord",
        accountId: str(ref.accountId, "chat account", 320),
        workspaceId: str(ref.workspaceId, "chat workspace", 320),
        channelId: str(ref.channelId, "chat channel", 320),
        threadId: str(ref.threadId, "chat thread", 320),
        suggestionId: ref.suggestionId as number,
        stale: ref.stale === undefined ? false : bool(ref.stale),
      };
      if (!chat.accountId || !chat.workspaceId || !chat.channelId || !chat.threadId ||
          !Number.isSafeInteger(chat.suggestionId) || chat.suggestionId < 1 || d.composed === true)
        throw new Error("Invalid chat suggestion metadata.");
    }
    if (d.source !== undefined && (d.source !== d.appId || d.source === "outlook"))
      throw new Error("Draft source does not match its app.");
    return {
      id,
      roleId,
      delivery,
      chat,
      source: d.appId === "outlook" ? undefined : appId(d.appId) as Draft["source"],
      appId: appId(d.appId),
      subject: str(d.subject, "subject", 1000),
      recipient: str(d.recipient, "recipient", 500),
      address: str(d.address, "address", 1000),
      context: str(d.context, "context", 10000),
      body,
      timeEdit,
      placeholderEdits,
      updatedAt: str(d.updatedAt, "timestamp", 100),
      externalUrl,
      composed: d.composed === undefined ? false : bool(d.composed),
      localChanges: d.localChanges === undefined ? false : bool(d.localChanges),
    };
  });
  unique(drafts);
  let personalDetails: PersonalDetails | undefined;
  if (w.personalDetails !== undefined) {
    const saved = object(w.personalDetails);
    personalDetails = {
      autoFill: bool(saved.autoFill),
      name: str(saved.name, "name", 1000),
      email: str(saved.email, "email", 1000),
      phone: str(saved.phone, "phone", 1000),
      address: str(saved.address, "address", 1000),
      website: str(saved.website, "website", 1000),
    };
    const issue = personalDetailsError(personalDetails);
    if (issue) throw new Error(issue);
  }
  const prefs = object(w.preferences);
  return {
    version: 1,
    mode: w.mode as Workspace["mode"],
    connections,
    roles,
    drafts,
    personalDetails,
    preferences: {
      automaticDrafts: true,
      learnFromEdits: bool(prefs.learnFromEdits),
    },
  };
}
