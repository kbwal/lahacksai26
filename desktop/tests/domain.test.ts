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
