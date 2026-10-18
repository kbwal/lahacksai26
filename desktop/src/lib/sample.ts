import type { Workspace } from "../types";
export function sampleWorkspace(): Workspace {
  return {
    version: 1,
    mode: "demo",
    connections: [
      { id: "gmail", name: "Gmail", connected: true },
      { id: "slack", name: "Slack", connected: true },
      { id: "outlook", name: "Outlook", connected: false },
    ],
    roles: [
      {
        id: "professional",
        name: "Professional",
        description:
          "Clear, thoughtful writing for colleagues, clients and work conversations.",
        kind: "default",
        excludedApps: [],
        usableExamples: 336,
        trainingReady: true,
      },
      {
        id: "social",
        name: "Social",
        description:
          "Relaxed and conversational with friends, family and your communities.",
        kind: "default",
        excludedApps: [],
        usableExamples: 412,
        trainingReady: true,
      },
    ],
    drafts: [
      {
        id: "office",
        appId: "gmail",
        roleId: "professional",
        subject: "Re: Office hours this week",
        recipient: "Professor Chen",
        address: "professor.chen@university.example",
        context: "What time would work for you to discuss your project?",
        body: "Hi Professor Chen,\n\nThanks for making time. [DATE] at [TIME] works for me. We can meet here: [LINK]. I’d love to go over your feedback on the project and a couple of questions I had.\n\nBest,\nSudarsan",
        updatedAt: "2026-10-17T16:42:00-07:00",
      },
      {
        id: "feedback",
        appId: "gmail",
        roleId: "professional",
        subject: "Re: Feedback on the project proposal",
        recipient: "Alex Rivera",
        address: "alex@team.example",
        context:
          "Could you incorporate the onboarding changes in the next revision?",
        body: "Hi Alex,\n\nThank you for the detailed feedback. I’ll incorporate the changes to the proposal and share an updated version once it’s ready.\n\nThe point about simplifying the onboarding flow makes a lot of sense. I’ll start there.\n\nBest,\nSudarsan",
        updatedAt: "2026-10-17T16:38:00-07:00",
      },
      {
        id: "weekend",
        appId: "gmail",
        roleId: "social",
        subject: "Re: Plans for the weekend",
        recipient: "Jamie",
        address: "jamie@friends.example",
        context:
          "Want to come along this weekend? We’re still figuring out the plan.",
        body: "hey!\n\nyeah that sounds fun, i’m in. send me the details once you’ve figured it out and i’ll take a look.\n\nsee you soon!",
        updatedAt: "2026-10-17T16:40:00-07:00",
      },
      {
        id: "handoff",
        appId: "slack",
        roleId: "professional",
        subject: "Demo handoff · # project-team",
        recipient: "Maya",
        address: "# project-team · thread",
        context: "Could you cover the UI walkthrough in the demo?",
        body: "I can take the UI walkthrough. Send me the endpoint details when they’re ready and I’ll connect the demo.\n\nI’ll also put together a short fallback recording, just in case.",
        updatedAt: "2026-10-17T16:41:00-07:00",
      },
      {
        id: "dinner",
        appId: "slack",
        roleId: "social",
        subject: "Dinner plans · Jamie",
        recipient: "Jamie",
        address: "Direct message · Jamie",
        context: "we’re grabbing dinner later, want to join?",
        body: "yeah i’m in! send me the place when you pick one and i’ll meet you there",
        updatedAt: "2026-10-17T16:39:00-07:00",
      },
    ],
    preferences: { automaticDrafts: true, learnFromEdits: true },
  };
}
