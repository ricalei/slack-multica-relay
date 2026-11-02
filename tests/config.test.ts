import { describe, expect, it } from "vitest";
import { loadRelayConfig } from "../src/config.js";

const env = {
  SLACK_SIGNING_SECRET: "test",
  SLACK_TEAM_ID: "T1",
  SLACK_ALLOWED_CHANNEL_IDS: "C1",
  SLACK_REACTION_NAME: "eyes",
  RELAY_OWNER_SLACK_USER_ID: "U1",
  SLACK_TARGET_USER_IDS: "U1",
  SLACK_USER_TOKEN: "test",
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
};

describe("Relay owner configuration", () => {
  it("loads one explicit owner independently of mention targets", () => {
    expect(loadRelayConfig({ ...env, RELAY_OWNER_SLACK_USER_ID: " U2 " }).ownerSlackUserId).toBe("U2");
  });
  it("fails closed when the owner is missing", () => {
    expect(() => loadRelayConfig({ ...env, RELAY_OWNER_SLACK_USER_ID: "" })).toThrow("relay_not_configured");
  });
  it.each(["U1,U2", "U1 U2", "S1", "rica"])("rejects an invalid owner: %s", (owner) => {
    expect(() => loadRelayConfig({ ...env, RELAY_OWNER_SLACK_USER_ID: owner })).toThrow("invalid_identifier");
  });
});

describe("release approval configuration", () => {
  it("keeps the feature disabled when the full triplet is absent", () => {
    const config = loadRelayConfig(env);
    expect(config.releaseApprovalChannelId).toBeUndefined();
    expect(config.releaseApprovalAppId).toBeUndefined();
    expect(config.releaseApprovalBotId).toBeUndefined();
  });

  it("loads the complete trusted Slack source", () => {
    const config = loadRelayConfig({
      ...env,
      SLACK_RELEASE_APPROVAL_CHANNEL_ID: " C08HV4GTJ2D ",
      SLACK_RELEASE_APPROVAL_APP_ID: " A080YA0KTJ9 ",
      SLACK_RELEASE_APPROVAL_BOT_ID: " B081PVALKC0 ",
    });
    expect(config).toMatchObject({
      releaseApprovalChannelId: "C08HV4GTJ2D",
      releaseApprovalAppId: "A080YA0KTJ9",
      releaseApprovalBotId: "B081PVALKC0",
    });
  });

  it.each([
    ["channel only", { SLACK_RELEASE_APPROVAL_CHANNEL_ID: "C08HV4GTJ2D" }],
    ["app only", { SLACK_RELEASE_APPROVAL_APP_ID: "A080YA0KTJ9" }],
    ["bot only", { SLACK_RELEASE_APPROVAL_BOT_ID: "B081PVALKC0" }],
    [
      "channel and app",
      {
        SLACK_RELEASE_APPROVAL_CHANNEL_ID: "C08HV4GTJ2D",
        SLACK_RELEASE_APPROVAL_APP_ID: "A080YA0KTJ9",
      },
    ],
  ])("fails closed for partial configuration: %s", (_name, partial) => {
    expect(() => loadRelayConfig({ ...env, ...partial })).toThrow(
      "invalid_release_approval_config",
    );
  });
});
