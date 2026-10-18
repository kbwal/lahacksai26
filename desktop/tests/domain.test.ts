import { describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import {
  allowedRoles,
  fillTime,
  formatTime,
  isAllowedExternalUrl,
  parseWorkspace,
  placeholders,
  roleFormErrors,
} from "../src/lib/domain";
import { sampleWorkspace } from "../src/lib/sample";
const require = createRequire(import.meta.url);
const nativePolicy = require("../electron/policy.cjs") as {
  isAllowedExternalUrl(value: unknown): boolean;
};
describe("placeholder handling", () => {
  it("recognizes model-invented uppercase placeholder types and deduplicates them", () => {
    expect(
      placeholders(
        "At [TIME] on [DATE]. [TIME] [DINING] [PERSON NAME] [lowercase]",
      ),
    ).toEqual(["[TIME]", "[DATE]", "[DINING]", "[PERSON NAME]"]);
  });
  it("fills every time token without changing other unresolved details", () => {
    const original = {
      ...sampleWorkspace().drafts[0],
      body: "Meet at [TIME] or [TIME] on [DATE].",
    };
    const result = fillTime(original, { time: "00:05", date: "" });
    expect(result.body).toBe("Meet at 12:05 AM or 12:05 AM on [DATE].");
    expect(result.localChanges).toBe(true);
    expect(original.body).toContain("[TIME]");
    expect(placeholders(result.body)).toEqual(["[DATE]"]);
  });
  it("formats noon and rejects impossible dates and times", () => {
    expect(formatTime({ time: "12:00", date: "" })).toBe("12:00 PM");
    expect(formatTime({ time: "14:30", date: "2026-10-20" })).toContain(
      "Tuesday, Oct 20 at 2:30 PM",
    );
    for (const choice of [
      { time: "24:00", date: "" },
      { time: "12:60", date: "" },
      { time: "12:00", date: "2026-02-30" },
    ])
      expect(() => formatTime(choice)).toThrow();
  });
});
describe("role policy", () => {
  it("reports blank fields and duplicate names without rejecting an unchanged role", () => {
    const roles = sampleWorkspace().roles;
    expect(roleFormErrors("   ", "   ", roles)).toEqual({
      name: "Enter a role name.",
      description: "Describe when to use this voice.",
    });
    expect(roleFormErrors(" professional ", "Clients", roles).name).toContain(
      "already exists",
    );
    expect(
      roleFormErrors("Professional", roles[0].description, roles, roles[0].id),
    ).toEqual({});
    expect(
      roleFormErrors("Community", "Volunteer coordination", roles),
    ).toEqual({});
  });
  it("uses all apps by default and applies exclusions independently per role", () => {
    const roles = sampleWorkspace().roles;
    expect(allowedRoles(roles, "outlook")).toHaveLength(3);
    roles[0].excludedApps = ["gmail"];
    expect(allowedRoles(roles, "gmail").map((r) => r.name)).toEqual([
      "Social",
      "Academic",
    ]);
    expect(allowedRoles(roles, "slack")).toHaveLength(3);
    expect(allowedRoles(roles, "all")).toHaveLength(3);
  });
});
describe("import boundary", () => {
  it("keeps inserted times editable after saving and reopening a workspace", () => {
    const data = sampleWorkspace();
    data.drafts[0].body =
      "The old meeting was at 9:00 AM. Meet at [TIME] on [DATE].";
    data.drafts[0] = fillTime(data.drafts[0], { time: "09:00", date: "" });
    const reopened = parseWorkspace(JSON.parse(JSON.stringify(data)));
    const changed = fillTime(reopened.drafts[0], { time: "16:30", date: "" });
    expect(changed.body).toBe(
      "The old meeting was at 9:00 AM. Meet at 4:30 PM on [DATE].",
    );
    expect(changed.timeEdit?.choice.time).toBe("16:30");
    expect(placeholders(changed.body)).toEqual(["[DATE]"]);
  });
  it("rejects stale time metadata instead of overwriting a draft changed elsewhere", () => {
    const data = sampleWorkspace();
    data.drafts[0] = fillTime(data.drafts[0], { time: "14:00", date: "" });
    data.drafts[0].body += " A new sentence.";
    expect(() => fillTime(data.drafts[0], { time: "16:30", date: "" })).toThrow(
      "changed since",
    );
    expect(() => parseWorkspace(data)).toThrow("time metadata");
  });
  it("round-trips a valid snapshot without inventing training readiness", () => {
    const data = sampleWorkspace();
    data.roles[0].usableExamples = 3;
    data.roles[0].trainingReady = false;
    expect(
      parseWorkspace(JSON.parse(JSON.stringify(data))).roles[0].trainingReady,
    ).toBe(false);
  });
  it("rejects drafts with missing roles, duplicate IDs and unsafe links", () => {
    const unknown = sampleWorkspace();
    unknown.drafts[0].roleId = "missing";
    expect(() => parseWorkspace(unknown)).toThrow();
    const duplicate = sampleWorkspace();
    duplicate.roles.push({ ...duplicate.roles[0] });
    expect(() => parseWorkspace(duplicate)).toThrow();
    const unsafe = sampleWorkspace();
    unsafe.drafts[0].externalUrl = "javascript:alert(1)";
    expect(() => parseWorkspace(unsafe)).toThrow();
  });
});
describe("external link boundary", () => {
  it.each([
    "https://mail.google.com/mail/u/0/#drafts",
    "https://app.slack.com/client/T/C",
    "https://outlook.office.com/mail/drafts",
  ])("allows official URL %s in native and renderer policies", (url) => {
    expect(isAllowedExternalUrl(url)).toBe(true);
    expect(nativePolicy.isAllowedExternalUrl(url)).toBe(true);
  });
  it.each([
    "file:///etc/passwd",
    "javascript:alert(1)",
    "http://mail.google.com",
    "https://mail.google.com.evil.test",
    "https://evil.test@google.com",
    "https://user:secret@mail.google.com",
    "https://app.slack.com:9999/",
  ])("blocks unsafe URL %s in both policies", (url) => {
    expect(isAllowedExternalUrl(url)).toBe(false);
    expect(nativePolicy.isAllowedExternalUrl(url)).toBe(false);
  });
});

describe("inbox search", () => {
  it("matches words across people, text, context, apps and roles without case sensitivity", async () => {
    const { searchDrafts } = await import("../src/lib/domain");
    const w = sampleWorkspace();
    expect(
      searchDrafts(w.drafts, "  MAYA ui  ", w.roles).map((d) => d.id),
    ).toEqual(["handoff"]);
    expect(
      searchDrafts(w.drafts, "gmail professional", w.roles).map((d) => d.id),
    ).toEqual(["feedback"]);
    expect(
      searchDrafts(w.drafts, "professor.chen@university.example", w.roles),
    ).toHaveLength(1);
    expect(searchDrafts(w.drafts, "nonexistent", w.roles)).toEqual([]);
    expect(searchDrafts(w.drafts, "   ", w.roles)).toHaveLength(5);
  });
  it("retains validated Gmail delivery metadata across saved snapshots", () => {
    const w = sampleWorkspace();
    w.mode = "local";
    w.drafts[0].delivery = {
      accountId: "me@example.com",
      generationId: 1,
      gmailDraftId: "d",
      snapshot: "a".repeat(64),
    };
    expect(parseWorkspace(w).drafts[0].delivery).toEqual(w.drafts[0].delivery);
    w.drafts[0].delivery.snapshot = "invalid";
    expect(() => parseWorkspace(w)).toThrow("delivery metadata");
  });
});

describe("model-marked details", () => {
  it("fills every mask.py type, preserves known facts, and remains editable after reopening", async () => {
    const { fillDetail, draftDetails } = await import("../src/lib/domain");
    const w = sampleWorkspace();
    w.drafts[0].body =
      "Known: https://example.com. [DATE] [TIME] [LINK] [AMOUNT] [PHONE] [ADDRESS] [MEETING_LINK_2]";
    const values = [
      "October 20, 2026",
      "2:00 PM",
      "https://meet.google.com/abc-defg-hij",
      "$125",
      "+1 310 555 0148",
      "123 Main Street",
      "Lobby entrance",
    ];
    values.forEach((value, index) => {
      w.drafts[0] = fillDetail(w.drafts[0], String(index), value);
    });
    expect(placeholders(w.drafts[0].body)).toEqual([]);
    expect(w.drafts[0].body).toContain("Known: https://example.com.");
    const reopened = parseWorkspace(JSON.parse(JSON.stringify(w)));
    expect(draftDetails(reopened.drafts[0])).toHaveLength(7);
    const edited = fillDetail(
      reopened.drafts[0],
      "2",
      "https://zoom.us/j/123456789",
    );
    expect(edited.body).toContain("https://zoom.us/j/123456789");
    expect(edited.body).not.toContain("meet.google.com");
    expect(edited.body).toContain("October 20, 2026 2:00 PM");
  });
  it("treats repeated placeholders as separate decisions", async () => {
    const { fillDetail, draftDetails } = await import("../src/lib/domain");
    const original = {
      ...sampleWorkspace().drafts[0],
      body: "Meet: [LINK]. Notes: [LINK].",
    };
    const first = fillDetail(
      original,
      "0",
      "https://meet.google.com/abc-defg-hij",
    );
    expect(first.body).toBe(
      "Meet: https://meet.google.com/abc-defg-hij. Notes: [LINK].",
    );
    expect(draftDetails(first).filter((detail) => !detail.value)).toHaveLength(
      1,
    );
    const second = fillDetail(
      first,
      "1",
      "https://docs.google.com/document/d/notes",
    );
    expect(second.body).toContain(
      "Meet: https://meet.google.com/abc-defg-hij.",
    );
    expect(second.body).toContain(
      "Notes: https://docs.google.com/document/d/notes.",
    );
    expect(original.body).toBe("Meet: [LINK]. Notes: [LINK].");
  });
  it("migrates existing inserted times without losing their values", async () => {
    const { fillDetail, draftDetails } = await import("../src/lib/domain");
    const original = {
      ...sampleWorkspace().drafts[0],
      body: "At [TIME], join [LINK]. Old meeting: 9:00 AM.",
    };
    const timed = fillTime(original, { date: "2026-10-20", time: "09:00" });
    const linked = fillDetail(
      timed,
      "2",
      "https://meet.google.com/abc-defg-hij",
    );
    expect(linked.timeEdit).toBeUndefined();
    expect(draftDetails(linked)[0].value).toContain(
      "Tuesday, Oct 20",
    );
    const w = sampleWorkspace();
    w.drafts[0] = linked;
    const reopened = parseWorkspace(JSON.parse(JSON.stringify(w))).drafts[0];
    expect(fillDetail(reopened, "1", "4:30 PM", "16:30").body).toBe(
      "At Tuesday, Oct 20 at 4:30 PM, join https://meet.google.com/abc-defg-hij. Old meeting: 9:00 AM.",
    );
  });
  it("rejects stale or forged edits and unfinished replacements", async () => {
    const { fillDetail } = await import("../src/lib/domain");
    const original = { ...sampleWorkspace().drafts[0], body: "Join [LINK]." };
    for (const invalid of [
      "javascript:alert(1)",
      "file:///tmp/private",
      "not a link",
      "[MEETING_LINK_2]",
    ])
      expect(() => fillDetail(original, "0", invalid)).toThrow();
    const filled = fillDetail(original, "0", "person@example.com");
    expect(() =>
      fillDetail({ ...filled, body: "Changed elsewhere" }, "0", "example.org"),
    ).toThrow("changed");
    const w = sampleWorkspace();
    w.drafts[0] = filled;
    w.drafts[0].placeholderEdits!.values["4"] = { value: "bad" };
    expect(() => parseWorkspace(w)).toThrow("detail index");
  });
});
