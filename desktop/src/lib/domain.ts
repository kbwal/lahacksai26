import type { AppId, Draft, Persona, TimeChoice, Workspace } from "../types";
export const APP_IDS: AppId[] = ["gmail", "slack", "outlook"];
export const APP_NAMES = { gmail: "Gmail", slack: "Slack", outlook: "Outlook" };
export const APP_LINKS = {
  gmail: "https://mail.google.com/mail/u/0/#drafts",
  slack: "https://app.slack.com",
  outlook: "https://outlook.live.com/mail/0/drafts",
};
const HOSTS = [
  "mail.google.com",
  "app.slack.com",
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
export function placeholders(text: string): string[] {
  return [...new Set(text.match(/\[[A-Z][A-Z_ ']*\]/g) || [])];
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
export function roleFormErrors(
  name: string,
  description: string,
  roles: Persona[],
  editingId?: string,
): { name?: string; description?: string } {
  const errors: { name?: string; description?: string } = {};
  if (!name.trim()) errors.name = "Enter a role name.";
  else if (
    roles.some(
      (r) =>
        r.id !== editingId &&
        r.name.trim().toLocaleLowerCase() === name.trim().toLocaleLowerCase(),
    )
  )
    errors.name =
      "A role with this name already exists. Choose a different name.";
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
  const connections = array(w.connections, 3).map((v) => {
    const a = object(v),
      id = appId(a.id);
    return { id, name: APP_NAMES[id], connected: bool(a.connected) };
  });
  unique(connections);
  if (connections.length !== 3)
    throw new Error("Include Gmail, Slack and Outlook connection records.");
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
      throw new Error("Invalid role.");
    return {
      id,
      name,
      description: str(r.description, "description", 300),
      kind: r.kind as Persona["kind"],
      excludedApps: [...new Set(array(r.excludedApps, 3).map(appId))],
      usableExamples: r.usableExamples as number,
      trainingReady: bool(r.trainingReady),
    };
  });
  unique(roles);
  if (!roles.length) throw new Error("A workspace needs at least one role.");
  const drafts = array(w.drafts, 500).map((v) => {
    const d = object(v),
      roleId = str(d.roleId, "draft role", 100),
      id = str(d.id, "draft ID", 100);
    if (!id || !roles.some((r) => r.id === roleId))
      throw new Error("Draft refers to an unknown role.");
    const externalUrl =
      d.externalUrl === undefined
        ? undefined
        : str(d.externalUrl, "draft link", 4000);
    if (externalUrl && !isAllowedExternalUrl(externalUrl))
      throw new Error(
        "Draft link must be an official Gmail, Slack or Outlook HTTPS URL.",
      );
    const body = str(d.body, "draft body", 60000);
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
    return {
      id,
      roleId,
      delivery,
      appId: appId(d.appId),
      subject: str(d.subject, "subject", 1000),
      recipient: str(d.recipient, "recipient", 500),
      address: str(d.address, "address", 1000),
      context: str(d.context, "context", 10000),
      body,
      timeEdit,
      updatedAt: str(d.updatedAt, "timestamp", 100),
      externalUrl,
      localChanges: d.localChanges === undefined ? false : bool(d.localChanges),
    };
  });
  unique(drafts);
  const prefs = object(w.preferences);
  return {
    version: 1,
    mode: w.mode as Workspace["mode"],
    connections,
    roles,
    drafts,
    preferences: {
      automaticDrafts: bool(prefs.automaticDrafts),
      learnFromEdits: bool(prefs.learnFromEdits),
    },
  };
}
