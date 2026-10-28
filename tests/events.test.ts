import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { acceptSlack, consumeQueue, consumerFailure } from "../src/http.js";
vi.mock("@upstash/qstash", () => ({
  Receiver: class {
    verify = vi.fn().mockResolvedValue(true);
  },
}));
const env = {
  SLACK_SIGNING_SECRET: "test",
  SLACK_TEAM_ID: "T1",
  RELAY_OWNER_SLACK_USER_ID: "U1",
  SLACK_TARGET_USER_IDS: "U1",
  SLACK_ALLOWED_CHANNEL_IDS: "C1",
  MULTICA_API_BASE_URL: "https://multica.test",
  MULTICA_API_TOKEN: "test",
  MULTICA_WORKSPACE_ID: "ws",
  MULTICA_PROJECT_ID: "project",
  MULTICA_AGENT_ID: "agent",
  SLACK_USER_TOKEN: "test",
  SLACK_REACTION_NAME: "eyes", // Legacy setting must not enable Relay-owned reactions.
  KV_REST_API_URL: "https://kv.test",
  KV_REST_API_TOKEN: "test",
  QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test",
  QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
};
function request(event: unknown, teamId = "T1"): Request {
  const body = JSON.stringify({
      type: "event_callback",
      team_id: teamId,
      event,
    }),
    ts = String(Math.floor(Date.now() / 1000));
  return new Request("https://relay.test/api/slack/events", {
    method: "POST",
    headers: {
      "x-slack-request-timestamp": ts,
      "x-slack-signature":
        "v0=" +
        createHmac("sha256", "test")
          .update("v0:" + ts + ":" + body)
          .digest("hex"),
    },
    body,
  });
}
const event = {
  type: "message",
  channel: "C1",
  user: "U2",
  ts: "100.000001",
  text: "<@U1> test",
};
const releaseEnv = {
  ...env,
  SLACK_ALLOWED_CHANNEL_IDS: "all",
  SLACK_RELEASE_APPROVAL_CHANNEL_ID: "C08HV4GTJ2D",
  SLACK_RELEASE_APPROVAL_APP_ID: "A080YA0KTJ9",
  SLACK_RELEASE_APPROVAL_BOT_ID: "B081PVALKC0",
};
const releaseText = `:rotating_light: *RELEASE APPROVAL REQUIRED*
\`\`\`
║  VERSION  v1.0.117
║  STATUS   WAITING FOR APPROVAL
\`\`\`
• *版本 PR：* <https://github.com/MoeGolibrary/moego-finance-kit/pull/354|#354 chore(ci): prepare v1.0.117>
• *本次包含的业务 PR（1 个）：* <https://github.com/MoeGolibrary/moego-finance-kit/pull/353|#353>`;
const releaseEvent = {
  type: "message",
  channel: "C08HV4GTJ2D",
  user: "U081137G64T",
  bot_id: "B081PVALKC0",
  app_id: "A080YA0KTJ9",
  ts: "1789637508.526609",
  text: releaseText,
};
afterEach(() => vi.restoreAllMocks());
describe("owner group-only mention gate", () => {
  const groupEnv = { ...env, SLACK_TARGET_USER_IDS: "U1,U3", SLACK_TARGET_SUBTEAM_IDS: "S1" };
  const groupText = "<https://github.com/MoeGolibrary/moego-bff/pull/1750|PR>\n<!subteam^S1> review";

  it.each([
    groupText,
    "<!subteam^S1> test review",
    "testing <!subteam^S1> review",
    "testcase <!subteam^S1> review",
    "[test]review <!subteam^S1>",
    "test中文 <!subteam^S1|fintech-front-end> review",
    "> test <!subteam^S1> review",
    "<@U3> <!subteam^S1> review",
    "test <!subteam^S1> review",
    "[test] <!subteam^S1> review",
    "<!subteam^S1> 取消",
  ])("ignores the owner's group mention without directly mentioning self: %s", async (text) => {
    const fetcher = vi.fn<typeof fetch>();
    const response = await acceptSlack(request({ ...event, user: "U1", text }), groupEnv, fetcher);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ action: "ignored", reason: "self_group_ignored" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    ["U1", "<@U1> review"],
    ["U1", "test <@U1> review"],
    ["U1", "<@U1> <!subteam^S1> review"],
    ["U1", "<!subteam^S1> <@U1|rica> review"],
    ["U1", "test <@U1> <!subteam^S1> review"],
    ["U2", groupText],
    ["U3", groupText],
    ["U2", "test <!subteam^S1> review"],
  ])("keeps permitted requests from %s: %s", async (user, text) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(request({ ...event, user, text }), groupEnv, fetcher);
    expect(await response.json()).toEqual({ action: "accepted", queueMessageId: "msg" });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetcher.mock.calls[0]![1]!.body))).toMatchObject({ operation: "dispatch", senderUserId: user });
  });

  it.each([
    [undefined, groupText],
    ["dispatch", "test <!subteam^S1> review"],
  ])("rechecks already queued owner group requests (%s): %s", async (operation, text) => {
    const fetcher = vi.fn<typeof fetch>();
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, {
      method: "POST",
      body: JSON.stringify({
        teamId: "T1", channelId: "C1", senderUserId: "U1", messageTs: "100.000002", threadTs: event.ts,
        text, mention: { type: "subteam", id: "S1" }, operation,
      }),
    }), groupEnv, fetcher);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ action: "ignored", reason: "self_group_ignored" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("keeps direct self cancellation usable without a group mention", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "cancel" }));
    const response = await acceptSlack(request({ ...event, user: "U1", text: "<@U1> 取消", thread_ts: event.ts }), groupEnv, fetcher);
    expect(await response.json()).toEqual({ action: "accepted", queueMessageId: "cancel" });
    expect(JSON.parse(String(fetcher.mock.calls[0]![1]!.body))).toMatchObject({ operation: "cancel" });
  });
});
describe("durable admission", () => {
  it.each(["invalid_event","invalid_thread_state","ambiguous_issue_mapping","invalid_issue_scope",
    "relay_payload_too_large","task_presentation_too_large","comment_lookup_limit"])("acknowledges permanent consumer rejection %s",async(code)=>{
    const response=consumerFailure(code);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({action:"rejected",error:code,retryable:false});
  });
  it("keeps transient consumer failures retryable",async()=>{
    expect(consumerFailure("timeout").status).toBe(503);
  });
  it("acknowledges a signed but invalid queued event without retrying",async()=>{
    const response=await consumeQueue(new Request(env.RELAY_CONSUMER_URL,{method:"POST",body:"{}"}),env,vi.fn<typeof fetch>());
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({action:"rejected",error:"invalid_event",retryable:false});
  });
  it("only publishes to queue before acknowledging", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(request(event), env, fetcher);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      action: "accepted",
      queueMessageId: "msg",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0]![0])).toContain(
      "/v2/publish/https://relay.test/api/queue/consume",
    );
  });
  it("projects attachment metadata before publishing to QStash and hashes log identity", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
    const files=[{id:"F1",name:"photo.png",mimetype:"image/png",size:12,
      url_private:"https://private.test/file",thumb_360:"https://private.test/thumb",shares:{private:{}}},
      ...Array.from({length:5},(_,i)=>({id:`F${i+2}`,name:`${i+2}.png`,mimetype:"image/png"}))];
    await acceptSlack(request({...event,files}),env,fetcher);
    const queued=JSON.parse(String(fetcher.mock.calls[0]![1]?.body));
    expect(queued.files).toHaveLength(5);expect(queued.files[0]).toEqual({id:"F1",name:"photo.png",mime:"image/png",size:12,contentStatus:"not_loaded"});
    expect(queued.filesTruncated).toBe(true);
    expect(JSON.stringify(queued)).not.toContain("private.test");
    expect(JSON.stringify(info.mock.calls)).not.toContain(event.text);
  });
  it("queues a trusted release candidate without requiring a target mention", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "release-msg" }));
    const response = await acceptSlack(
      request(releaseEvent),
      releaseEnv,
      fetcher,
    );
    expect(await response.json()).toEqual({
      action: "accepted",
      queueMessageId: "release-msg",
    });
    const queued = JSON.parse(String(fetcher.mock.calls[0]![1]!.body));
    expect(queued).toEqual({
      routeId: "review",
      routeTarget: expect.stringMatching(/^[a-f0-9]{64}$/u),
      taskType: "release_approval",
      teamId: "T1",
      channelId: "C08HV4GTJ2D",
      messageTs: "1789637508.526609",
      threadTs: "1789637508.526609",
      senderUserId: "U081137G64T",
      text: releaseText,
      operation: "dispatch",
      releaseApproval: {
        version: "v1.0.117",
        prUrl: "https://github.com/MoeGolibrary/moego-finance-kit/pull/354",
        sourceAppId: "A080YA0KTJ9",
        sourceBotId: "B081PVALKC0",
      },
    });
  });
  it.each([
    ["ordinary bot message", { text: "release completed" }],
    ["wrong app", { app_id: "A_OTHER" }],
    ["wrong bot", { bot_id: "B_OTHER" }],
    ["wrong channel", { channel: "C_OTHER" }],
    ["thread reply", { thread_ts: releaseEvent.ts }],
  ])("does not queue a release lookalike: %s", async (_name, change) => {
    const fetcher = vi.fn<typeof fetch>();
    const response = await acceptSlack(
      request({ ...releaseEvent, ...change }),
      releaseEnv,
      fetcher,
    );
    expect(await response.json()).toMatchObject({ action: "ignored" });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it.each([
    { channel: "C2" },
    { user: undefined },
    { text: "ordinary", thread_ts: "1.000001" },
    { bot_id: "B1" },
    { subtype: "message_changed" },
  ])("no queue side effects for %j", async (change) => {
    const fetcher = vi.fn<typeof fetch>();
    await acceptSlack(request({ ...event, ...change }), env, fetcher);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("rejects another Slack team", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptSlack(request(event, "T2"), env, f);
    expect(f).not.toHaveBeenCalled();
  });
  it("accepts all as an explicit channel allowlist", async () => {
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(
      request({ ...event, channel: "C2" }),
      { ...env, SLACK_ALLOWED_CHANNEL_IDS: "all" },
      f,
    );
    expect(response.status).toBe(200);
    expect(f).toHaveBeenCalledTimes(1);
  });
  it.each([undefined, "", ", ,"])(
    "fails closed for a missing or empty channel allowlist %s",
    async (value) => {
      const f = vi.fn<typeof fetch>();
      const candidate = { ...env, SLACK_ALLOWED_CHANNEL_IDS: value };
      expect((await acceptSlack(request(event), candidate, f)).status).toBe(500);
      expect(f).not.toHaveBeenCalled();
    },
  );
  it("blocks a channel even when the allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(
      request({ ...event, channel: "C2" }),
      {
        ...env,
        SLACK_ALLOWED_CHANNEL_IDS: "all",
        SLACK_BLOCKED_CHANNEL_IDS: "C2",
      },
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "not_allowed",
    });
    expect(f).not.toHaveBeenCalled();
  });
  it("applies sender policy", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptSlack(
      request(event),
      { ...env, SLACK_ALLOWED_SENDER_IDS: "U3" },
      f,
    );
    expect(f).not.toHaveBeenCalled();
  });
  it("blocks a sender even when the sender allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(
      request(event),
      {
        ...env,
        SLACK_ALLOWED_SENDER_IDS: "all",
        SLACK_BLOCKED_SENDER_IDS: "U2",
      },
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "not_allowed",
    });
    expect(f).not.toHaveBeenCalled();
  });
  it("keeps Slack retry ownership when queue publish fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = vi.fn<typeof fetch>().mockRejectedValue(new Error("secret body"));
    const response = await acceptSlack(request(event), env, f);
    expect(response.status).toBe(503);
    expect(JSON.stringify(vi.mocked(console.warn).mock.calls)).not.toContain(
      "secret body",
    );
  });
  it("rejects invalid signature before network", async () => {
    const f = vi.fn<typeof fetch>();
    expect(
      (
        await acceptSlack(
          new Request("https://relay.test", { method: "POST", body: "{}" }),
          env,
          f,
        )
      ).status,
    ).toBe(401);
    expect(f).not.toHaveBeenCalled();
  });
  it("rechecks channel policy for queued messages", async () => {
    const f = vi.fn<typeof fetch>();
    const queued = {
      teamId: "T1",
      channelId: "C2",
      senderUserId: "U2",
      messageTs: "1.000001",
      threadTs: "1.000001",
      text: "<@U1> test",
      mention: { type: "user", id: "U1" },
    };
    const response = await consumeQueue(
      new Request(env.RELAY_CONSUMER_URL, {
        method: "POST",
        body: JSON.stringify(queued),
      }),
      env,
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "policy_changed",
    });
    expect(f).not.toHaveBeenCalled();
  });
  it.each([
    ["source app", { sourceAppId: "A0OTHER" }],
    ["source bot", { sourceBotId: "B0OTHER" }],
  ])("rejects a queued release payload with stale %s policy", async (_name, releaseChange) => {
    const fetcher = vi.fn<typeof fetch>();
    const response = await consumeQueue(
      new Request(releaseEnv.RELAY_CONSUMER_URL, {
        method: "POST",
        body: JSON.stringify({
          taskType: "release_approval",
          teamId: "T1",
          channelId: releaseEvent.channel,
          senderUserId: releaseEvent.user,
          messageTs: releaseEvent.ts,
          threadTs: releaseEvent.ts,
          text: releaseText,
          operation: "dispatch",
          releaseApproval: {
            version: "v1.0.117",
            prUrl:
              "https://github.com/MoeGolibrary/moego-finance-kit/pull/354",
            sourceAppId: releaseEvent.app_id,
            sourceBotId: releaseEvent.bot_id,
            ...releaseChange,
          },
        }),
      }),
      releaseEnv,
      fetcher,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "policy_changed",
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("creates a Multica task from a valid queued release payload", async () => {
    const kv = new Map<string, string>();
    let description = "";
    const fetcher: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url === releaseEnv.KV_REST_API_URL) {
        const [command, key, value, mode] = JSON.parse(
          String(init?.body),
        ) as string[];
        if (command === "GET")
          return Response.json({ result: kv.get(key!) ?? null });
        if (command === "SET") {
          if (mode === "NX" && kv.has(key!))
            return Response.json({ result: null });
          kv.set(key!, value!);
          return Response.json({ result: "OK" });
        }
        if (command === "EVAL") return Response.json({ result: 1 });
      }
      if (url === "https://multica.test/api/agents/agent")
        return Response.json({
          id: "agent",
          workspace_id: "ws",
          model: "gpt-6-astra",
          service_tier: "default",
        });
      if (url.includes("/api/issues/search?")) return Response.json({ issues: [] });
      if (url.endsWith("/api/issues")) {
        description = JSON.parse(String(init?.body)).description;
        return Response.json({ id: "release-issue", title: "release" });
      }
      throw new Error(`unexpected endpoint ${url}`);
    };
    const payload = {
      taskType: "release_approval",
      teamId: "T1",
      channelId: releaseEvent.channel,
      senderUserId: releaseEvent.user,
      messageTs: releaseEvent.ts,
      threadTs: releaseEvent.ts,
      text: releaseText,
      operation: "dispatch",
      releaseApproval: {
        version: "v1.0.117",
        prUrl: "https://github.com/MoeGolibrary/moego-finance-kit/pull/354",
        sourceAppId: releaseEvent.app_id,
        sourceBotId: releaseEvent.bot_id,
      },
    };
    const response = await consumeQueue(
      new Request(releaseEnv.RELAY_CONSUMER_URL, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
      releaseEnv,
      fetcher,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      action: "created",
      issueId: "release-issue",
    });
    const delivered = JSON.parse(
      description.match(/```json\n([\s\S]*?)\n```/)![1]!,
    );
    expect(delivered.eventPayload).toMatchObject(payload);
    expect(delivered.eventPayload).not.toHaveProperty("mention");
  });

  it("fetches configuration in the consumer and delivers a separate trusted reply context", async () => {
    const kv = new Map<string, string>();
    const agentUrls: string[] = [];
    const slackReactionCalls: string[] = [];
    let description = "";
    const fetcher: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url === env.KV_REST_API_URL) {
        const [command, key, value, mode] = JSON.parse(String(init?.body)) as string[];
        if (command === "GET") return Response.json({ result: kv.get(key!) ?? null });
        if (command === "SET") {
          if (mode === "NX" && kv.has(key!)) return Response.json({ result: null });
          kv.set(key!, value!);
          return Response.json({ result: "OK" });
        }
        if (command === "EVAL") return Response.json({ result: 1 });
      }
      if (url === "https://multica.test/api/agents/agent") {
        agentUrls.push(url);
        return Response.json({ id: "agent", workspace_id: "ws", model: "gpt-6-astra", service_tier: "default" });
      }
      if (url.includes("/api/issues/search?")) return Response.json({ issues: [] });
      if (url.endsWith("/api/issues")) {
        const body = JSON.parse(String(init?.body));
        description = body.description;
        return Response.json({ id: "issue", title: body.title });
      }
      if (url === "https://slack.com/api/reactions.add") {
        slackReactionCalls.push(String(init?.body));
        return Response.json({ ok: true });
      }
      throw new Error("unexpected endpoint");
    };
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, {
      method: "POST",
      body: JSON.stringify({
        teamId: "T1", channelId: "C1", senderUserId: "U2", messageTs: "100.000001", threadTs: "100.000001",
        text: "<@U1> test", mention: { type: "user", id: "U1" },
        replyContext: { model: "spoofed", serviceTier: "priority" },
      }),
    }), env, fetcher);
    expect(response.status).toBe(200);
    expect(agentUrls).toHaveLength(1);
    const delivered = JSON.parse(description.match(/```json\n([\s\S]*?)\n```/)![1]!);
    expect(delivered.replyContext).toMatchObject({ type: "slack_reply_context", source: "agent_config", status: "available", model: "gpt-6-astra", serviceTier: "default" });
    expect(delivered.eventPayload).not.toHaveProperty("replyContext");
    expect(description).not.toContain("spoofed");
    expect(slackReactionCalls).toEqual([]);
  });
});

describe("取消指令的事件路由", () => {
  it("授权取消入队并保存指令类型", async () => {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "cancel-msg" }));
    const response = await acceptSlack(request({ ...event, user: "U1", text: "<@U1> CANCEL", thread_ts: event.ts }), env, f);
    expect(response.status).toBe(200);
    expect(JSON.parse(String(f.mock.calls[0]![1]!.body))).toMatchObject({ operation: "cancel", senderUserId: "U1" });
  });
  it("非目标用户的取消指令不会入队或作为新任务处理", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(request({ ...event, text: "<@U1> 取消" }), env, f);
    expect(await response.json()).toEqual({ action: "ignored", reason: "cancel_not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it("自定义关键词替换默认值，普通讨论仍入队为 dispatch", async () => {
    for (const [text, operation] of [["<@U1> stop", "cancel"], ["<@U1> cancel", "dispatch"], ["<@U1> 帮我取消", "dispatch"]]) {
      const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
      await acceptSlack(request({ ...event, user: "U1", text }), { ...env, SLACK_CANCEL_KEYWORDS: "stop,停止" }, f);
      expect(JSON.parse(String(f.mock.calls[0]![1]!.body)).operation).toBe(operation);
    }
  });
  it("消费时重新核对取消身份，不能借队列绕过权限", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify({
      teamId: "T1", channelId: "C1", senderUserId: "U2", messageTs: "102.000001", threadTs: "100.000001",
      text: "<@U1> cancel", mention: { type: "user", id: "U1" }, operation: "cancel",
    }) }), env, f);
    expect(await response.json()).toEqual({ action: "ignored", reason: "cancel_not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it.each([
    ["cancel", "<@U1>", "user", "U1"],
    [undefined, "<@U1>", "user", "U1"],
    ["cancel", "<!subteam^S1>", "subteam", "S1"],
    [undefined, "<!subteam^S1>", "subteam", "S1"],
  ])("消费取消 %s %s 不建卡、不加 reaction；已分类取消不随关键词变更变成任务", async (operation, mentionText, type, id) => {
    const calls: string[] = [];
    const kv = new Map<string, string>();
    const f: typeof fetch = async (input, init) => {
      const url = String(input); calls.push(url);
      if (url === env.KV_REST_API_URL) {
        const [command, key, value] = JSON.parse(String(init?.body));
        if (command === "GET") return Response.json({ result: kv.get(key) ?? null });
        if (command === "SET") { kv.set(key, value); return Response.json({ result: "OK" }); }
        if (command === "EVAL") return Response.json({ result: 1 });
      }
      if (url.includes("/api/issues/search?")) return Response.json({ issues: [] });
      throw new Error("不应调用建卡或 reaction 接口");
    };
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify({
      teamId: "T1", channelId: "C1", senderUserId: "U1", messageTs: "102.000001", threadTs: "100.000001",
      text: `${mentionText} cancel`, mention: { type, id }, operation,
    }) }), { ...env, SLACK_TARGET_SUBTEAM_IDS: "S1", ...(operation ? { SLACK_CANCEL_KEYWORDS: "stop" } : {}) }, f);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(type === "subteam"
      ? { action: "ignored", reason: "self_group_ignored" }
      : { action: "ignored" });
    expect(calls.every((url) => url === env.KV_REST_API_URL || url.includes("/api/issues/search?"))).toBe(true);
  });
});

describe("Team configuration admission", () => {
  it("accepts a Team without the legacy agent variable", async () => {
    const { MULTICA_AGENT_ID: _legacy, ...base } = env;
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "m" }));
    expect((await acceptSlack(request(event), { ...base, MULTICA_ASSIGNEE_TYPE: "squad", MULTICA_ASSIGNEE_ID: "team" }, f)).status).toBe(200);
  });
  it.each([
    { MULTICA_ASSIGNEE_TYPE: "unknown", MULTICA_ASSIGNEE_ID: "team" },
    { MULTICA_ASSIGNEE_TYPE: "squad" },
    { MULTICA_ASSIGNEE_TYPE: "squad", MULTICA_ASSIGNEE_ID: "team", MULTICA_LEGACY_AGENT_ID: "agent", MULTICA_THREAD_SCOPE_ID: "wrong" },
  ])("rejects invalid Team configuration %j", async (extra) => {
    const f = vi.fn<typeof fetch>();
    expect((await acceptSlack(request(event), { ...env, ...extra }, f)).status).toBe(500);
    expect(f).not.toHaveBeenCalled();
  });
});
