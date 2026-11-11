import { describe, expect, it } from "vitest";
import type { SlackMessageEvent } from "../src/mentions.js";
import {
  findReleaseApproval,
  type ReleaseApprovalConfig,
} from "../src/release-approval.js";

const config: ReleaseApprovalConfig = {
  releaseApprovalChannelId: "C08HV4GTJ2D",
  releaseApprovalAppId: "A080YA0KTJ9",
  releaseApprovalBotId: "B081PVALKC0",
};

const text = `:rotating_light: *RELEASE APPROVAL REQUIRED*

\`\`\`
║  FINANCE KIT · RELEASE APPROVAL
║  VERSION  v1.0.117
║  STATUS   WAITING FOR APPROVAL
\`\`\`

<@U051BV5QYDB> 本次发布候选已更新，请审核版本 PR 并确认发布范围。

• *版本 PR：* <https://github.com/MoeGolibrary/moego-finance-kit/pull/354|#354 chore(ci): prepare v1.0.117>
• *本次包含的业务 PR（1 个）：* <https://github.com/MoeGolibrary/moego-finance-kit/pull/353|#353>

Action link: <https://github.com/MoeGolibrary/moego-finance-kit/actions/runs/35205522471>`;

const event: SlackMessageEvent = {
  type: "message",
  channel: "C08HV4GTJ2D",
  user: "U081137G64T",
  bot_id: "B081PVALKC0",
  app_id: "A080YA0KTJ9",
  ts: "1789637508.526609",
  text,
};

describe("release approval classifier", () => {
  it("extracts only the labeled version PR from the trusted release message", () => {
    expect(findReleaseApproval(event, config)).toEqual({
      version: "v1.0.117",
      prUrl: "https://github.com/MoeGolibrary/moego-finance-kit/pull/354",
      sourceAppId: "A080YA0KTJ9",
      sourceBotId: "B081PVALKC0",
    });
  });

  it.each([
    ["channel", "C_OTHER"],
    ["app_id", "A_OTHER"],
    ["bot_id", "B_OTHER"],
  ])("rejects a lookalike with the wrong %s", (field, value) => {
    expect(findReleaseApproval({ ...event, [field]: value }, config)).toBeUndefined();
  });

  it.each([
    ["thread reply", { thread_ts: event.ts }],
    ["edited message", { subtype: "message_changed" }],
    ["deleted message", { subtype: "message_deleted" }],
    ["bot_message subtype", { subtype: "bot_message" }],
  ])("rejects a %s", (_name, change) => {
    expect(findReleaseApproval({ ...event, ...change }, config)).toBeUndefined();
  });

  it.each([
    ["missing heading", text.replace("RELEASE APPROVAL REQUIRED", "RELEASE READY")],
    ["missing waiting status", text.replace("WAITING FOR APPROVAL", "READY")],
    ["missing version", text.replace("VERSION  v1.0.117", "VERSION  unknown")],
    ["version mismatch", text.replace("prepare v1.0.117", "prepare v1.0.118")],
    [
      "business PR only",
      text.replace("*版本 PR：*", "*触发本次审批的业务 PR：*"),
    ],
    [
      "two version PR targets",
      text +
        "\n• *版本 PR：* <https://github.com/MoeGolibrary/moego-finance-kit/pull/355|#355 chore(ci): prepare v1.0.117>",
    ],
    [
      "non-canonical GitHub target",
      text.replace(
        "https://github.com/MoeGolibrary/moego-finance-kit/pull/354",
        "https://example.com/MoeGolibrary/moego-finance-kit/pull/354",
      ),
    ],
  ])("rejects %s", (_name, changedText) => {
    expect(
      findReleaseApproval({ ...event, text: changedText }, config),
    ).toBeUndefined();
  });

  it("is disabled unless the complete trusted source is configured", () => {
    expect(findReleaseApproval(event, {})).toBeUndefined();
  });
});
