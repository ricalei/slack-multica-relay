import { describe, expect, it } from "vitest";
import { loadRelayConfig } from "../src/config.js";
import { canCancelTask, isCancelCommand } from "../src/mentions.js";

const baseEnv = {
  SLACK_SIGNING_SECRET: "test-secret",
  SLACK_TEAM_ID: "T1",
  SLACK_ALLOWED_CHANNEL_IDS: "C1",
  RELAY_OWNER_SLACK_USER_ID: "U123",
  MULTICA_API_BASE_URL: "https://multica.test",
  MULTICA_API_TOKEN: "test",
  MULTICA_WORKSPACE_ID: "ws",
  MULTICA_PROJECT_ID: "project",
  MULTICA_AGENT_ID: "agent",
  KV_REST_API_URL: "https://kv.test",
  KV_REST_API_TOKEN: "test",
  QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test",
  QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
  SLACK_USER_TOKEN: "test-token",
  SLACK_TARGET_USER_IDS: "U123,U456",
  SLACK_TARGET_SUBTEAM_IDS: "S123",
};

function matches(text: string, keywords?: string): boolean {
  const config = loadRelayConfig({
    ...baseEnv,
    SLACK_CANCEL_KEYWORDS: keywords,
  });
  return isCancelCommand(
    text,
    config.targetUserIds,
    config.targetSubteamIds,
    config.cancelKeywords,
  );
}

describe("review reaction 清理身份", () => {
  it.each([
    [" bot-token ", "user-token", "user-token"],
    [undefined, " user-token ", "user-token"],
    ["", "user-token", "user-token"],
    [" \n ", "user-token", "user-token"],
  ])("bot 配置为 %j、user 配置为 %j 时始终选择 %s", (bot, user, expected) => {
    const config = loadRelayConfig({
      ...baseEnv,
      SLACK_BOT_TOKEN: bot,
      SLACK_USER_TOKEN: user,
    });
    expect(config.slackReactionToken).toBe(expected);
  });

  it("即使有 bot token，缺少 user token 仍拒绝运行", () => {
    expect(() => loadRelayConfig({
      ...baseEnv, SLACK_BOT_TOKEN: "bot-token", SLACK_USER_TOKEN: " ",
    })).toThrow("relay_not_configured");
  });
});

describe("取消关键词配置", () => {
  it.each([undefined, "", "  ", ", ,"])(
    "配置为空时使用默认关键词：%s",
    (value) => {
      expect(matches("<@U123> cancel", value)).toBe(true);
      expect(matches("<@U123> 取消", value)).toBe(true);
    },
  );

  it("自定义配置替换默认值，并支持空白、重复项和大小写", () => {
    const keywords = " STOP , 停止 , stop, ";
    expect(matches("<@U123> stop", keywords)).toBe(true);
    expect(matches("<@U123> 停止", keywords)).toBe(true);
    expect(matches("<@U123> cancel", keywords)).toBe(false);
    expect(matches("<@U123> 取消", keywords)).toBe(false);
  });
});

describe("取消指令识别", () => {
  it.each([
    "<@U123> cancel",
    "<@U123|Alice> 取消",
    "  <@U123> \n CANCEL  ",
    "<!subteam^S123|team> 取消",
    "<@U123> <@U456> cancel",
  ])("识别目标 mention 与完整关键词：%s", (text) => {
    expect(matches(text)).toBe(true);
  });

  it.each([
    "cancel",
    "<@U999> cancel",
    "<!subteam^S999> 取消",
    "<@U123>",
    "<@U123> 帮我取消这个任务",
    "<@U123> cancel please",
    "<@U123> cancelled",
    "<@U123> 取消！",
    "<@U123> <@U999> cancel",
    "<@U123> can cel",
  ])("普通讨论或不匹配的指令不能取消：%s", (text) => {
    expect(matches(text)).toBe(false);
  });
});

describe("取消权限", () => {
  it.each([
    ["U123", true],
    ["U456", true],
    ["U999", false],
    ["S123", false],
    [undefined, false],
  ] as const)("发送者 %s 的权限为 %s", (sender, allowed) => {
    const config = loadRelayConfig(baseEnv);
    expect(canCancelTask(sender, config.targetUserIds)).toBe(allowed);
  });

  it("仅配置 mention 用户组不会授予取消权限", () => {
    const config = loadRelayConfig({ ...baseEnv, SLACK_TARGET_USER_IDS: "" });
    expect(
      isCancelCommand(
        "<!subteam^S123> cancel",
        config.targetUserIds,
        config.targetSubteamIds,
        config.cancelKeywords,
      ),
    ).toBe(true);
    expect(canCancelTask("U123", config.targetUserIds)).toBe(false);
  });
});
