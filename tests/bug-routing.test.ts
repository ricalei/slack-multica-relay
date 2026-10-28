import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { acceptSlack, consumeQueue } from "../src/http.js";

vi.mock("@upstash/qstash", () => ({ Receiver: class { verify = async () => true; } }));
afterEach(() => vi.restoreAllMocks());
const env = {
  SLACK_SIGNING_SECRET: "test", SLACK_TEAM_ID: "T1", RELAY_OWNER_SLACK_USER_ID: "U1",
  SLACK_TARGET_USER_IDS: "U1", SLACK_TARGET_SUBTEAM_IDS: "S1", SLACK_ALLOWED_CHANNEL_IDS: "C1",
  SLACK_USER_TOKEN: "review-token", SLACK_BOT_TOKEN: "bug-token", SLACK_BUG_BOT_USER_ID: "UBUG",
  MULTICA_API_BASE_URL: "https://multica.test", MULTICA_API_TOKEN: "test",
  MULTICA_WORKSPACE_ID: "review-ws", MULTICA_PROJECT_ID: "review-project", MULTICA_AGENT_ID: "review-agent",
  MULTICA_BUG_WORKSPACE_ID: "bug-ws", MULTICA_BUG_PROJECT_ID: "bug-project", MULTICA_BUG_AGENT_ID: "bug-agent",
  KV_REST_API_URL: "https://kv.test", KV_REST_API_TOKEN: "test", QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test", QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
};
const root = { type: "message", channel: "C1", user: "U2", ts: "100.000001", text: "<@UBUG> investigate" };
function request(event: unknown) {
  const body = JSON.stringify({ type: "event_callback", team_id: "T1", event });
  const ts = String(Math.floor(Date.now() / 1000));
  return new Request("https://relay.test/api/slack/events", { method: "POST", body, headers: {
    "x-slack-request-timestamp": ts,
    "x-slack-signature": "v0=" + createHmac("sha256", "test").update(`v0:${ts}:${body}`).digest("hex"),
  } });
}
function fixture() {
  const queued: { body: any; headers: Headers }[] = [];
  const kv = new Map<string, string>();
  const issues: any[] = [], comments: any[] = [], runs: any[] = [];
  const slackActors: string[] = [];
  let loseCreate = false, hideSearch = false;
  const fetcher: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    const headers = new Headers(init?.headers);
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    const ws = headers.get("x-workspace-id");
    if (url.host === "qstash.upstash.io") {
      queued.push({ body, headers });
      return Response.json({ messageId: "queued" });
    }
    if (url.host === "kv.test") {
      const [op, key, value] = body;
      if (op === "GET") return Response.json({ result: kv.get(key) ?? null });
      if (op === "SET") {
        if (body.includes("NX") && kv.has(key)) return Response.json({ result: null });
        kv.set(key, value); return Response.json({ result: "OK" });
      }
      if (op === "EVAL") { if (kv.get(body[3]) === body[4]) kv.delete(body[3]); return Response.json({ result: 1 }); }
    }
    if (url.host === "slack.com") {
      slackActors.push(headers.get("authorization")!);
      if (url.pathname.endsWith("auth.test")) return Response.json({ ok: true, user_id: "UBUG" });
      if (url.pathname.endsWith("reactions.get")) return Response.json({ ok: true, message: { reactions: [] } });
    }
    if (url.pathname.startsWith("/api/agents/")) return Response.json({ id: url.pathname.split("/").at(-1), workspace_id: ws });
    if (url.pathname.endsWith("/search")) return Response.json({ issues: hideSearch ? [] : issues.filter(x => x.workspace_id === ws && x.description.includes(url.searchParams.get("q"))) });
    if (url.pathname === "/api/issues") {
      const issue = { ...body, id: `issue-${issues.length}`, workspace_id: ws }; issues.push(issue);
      runs.push({ id: `run-${issues.length}`, issue_id: issue.id, workspace_id: ws, agent_id: body.assignee_id, status: "running" });
      if (loseCreate) throw new Error("lost create response");
      return Response.json(issue);
    }
    const issue = issues.find(x => x.id === url.pathname.split("/")[3] && x.workspace_id === ws);
    if (issue) {
      if (url.pathname.endsWith("/task-runs")) return Response.json(runs.filter(x => x.issue_id === issue.id));
      if (url.pathname.endsWith("/cancel")) { runs.find(x => x.id === url.pathname.split("/").at(-2)).status = "cancelled"; return Response.json({}); }
      if (url.pathname.endsWith("/comments")) {
        if (init?.method === "POST") { const comment = { ...body, id: `comment-${comments.length}`, issue_id: issue.id }; comments.push(comment); return Response.json(comment); }
        return Response.json(comments.filter(x => x.issue_id === issue.id));
      }
      return Response.json(issue);
    }
    throw new Error(`unexpected ${url.pathname}`);
  };
  const admit = async (event: unknown, config = env) => acceptSlack(request(event), config, fetcher);
  const consume = async (body: unknown, config = env) => consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify(body) }), config, fetcher);
  return { queued, kv, issues, comments, runs, slackActors, admit, consume,
    loseCreateResponse() { loseCreate = true; hideSearch = true; },
    restoreSearch() { loseCreate = false; hideSearch = false; },
  };
}

describe("Bug and Review routes", () => {
  it.each(["message", "app_mention"])("accepts %s as a Bug request and creates a FinTech Resolver issue", async type => {
    const f = fixture();
    expect(await (await f.admit({ ...root, type })).json()).toMatchObject({ action: "accepted" });
    expect(f.queued[0]?.body).toMatchObject({ routeId: "bug", taskType: "bug_resolver" });
    expect(await (await f.consume(f.queued[0]?.body)).json()).toMatchObject({ action: "created" });
    expect(f.issues[0]).toMatchObject({ workspace_id: "bug-ws", project_id: "bug-project", assignee_id: "bug-agent", assignee_type: "agent" });
  });
  it("coalesces paired message/app_mention delivery without a second issue or comment", async () => {
    const f = fixture();
    await f.admit(root); await f.admit({ ...root, type: "app_mention" });
    expect(f.queued).toHaveLength(2);
    expect(f.queued[0]!.headers.get("Upstash-Deduplication-Id")).toBe(f.queued[1]!.headers.get("Upstash-Deduplication-Id"));
    await f.consume(f.queued[0]!.body); await f.consume(f.queued[1]!.body);
    expect(f.issues).toHaveLength(1); expect(f.comments).toHaveLength(0);
  });
  it("isolates two routes in one thread while preserving the Review target", async () => {
    const f = fixture();
    await f.admit({ ...root, text: "<@U1> review" }); await f.consume(f.queued[0]!.body);
    await f.admit({ ...root, ts: "101.000001", thread_ts: root.ts }); await f.consume(f.queued[1]!.body);
    expect(f.issues.map(x => [x.workspace_id, x.project_id, x.assignee_id])).toEqual([
      ["review-ws", "review-project", "review-agent"], ["bug-ws", "bug-project", "bug-agent"],
    ]);
    expect(f.queued[0]!.headers.get("Upstash-Flow-Control-Key")).not.toBe(f.queued[1]!.headers.get("Upstash-Flow-Control-Key"));
  });
  it("gives explicit bot mentions priority even when owner also mentions a group or self", async () => {
    const f = fixture();
    await f.admit({ ...root, user: "U1", text: "<@UBUG> <!subteam^S1> <@U1> fix" });
    expect(f.queued).toHaveLength(1); expect(f.queued[0]!.body.routeId).toBe("bug");
  });
  it("never lets Slack-supplied routing metadata select an agent", async () => {
    const f = fixture();
    await f.admit({ ...root, text: "<@U1> review", routeId: "bug", routeTarget: "forged" });
    await f.consume(f.queued[0]!.body);
    expect(f.issues[0].assignee_id).toBe("review-agent");
  });
  it.each([
    { bot_id: "B1" }, { user: "UBUG" }, { channel: "C2" }, { subtype: "message_changed" },
  ])("does not forward excluded Bug input %j", async change => {
    const f = fixture(); await f.admit({ ...root, ...change }); expect(f.queued).toHaveLength(0);
  });
  it("rejects unknown routes before touching state", async () => {
    const f = fixture();
    const response = await f.consume({ teamId: "T1", channelId: "C1", senderUserId: "U2", threadTs: root.ts, messageTs: root.ts, text: "<@U1> review", mention: { type: "user", id: "U1" }, routeId: "unknown", routeTarget: "x" });
    expect(await response.json()).toMatchObject({ action: "rejected", error: "invalid_event" });
    expect(f.kv.size).toBe(0); expect(f.issues).toHaveLength(0);
  });
  it.each(["MULTICA_BUG_AGENT_ID", "MULTICA_BUG_PROJECT_ID", "MULTICA_BUG_WORKSPACE_ID"])("does not retarget queued work after %s changes", async key => {
    const f = fixture(); await f.admit(root);
    const response = await f.consume(f.queued[0]?.body, { ...env, [key]: "changed" });
    expect(await response.json()).toMatchObject({ action: "ignored", reason: "route_changed" });
    expect(f.issues).toHaveLength(0); expect(f.kv.size).toBe(0);
  });
  it("fails closed for partial Bug routing configuration", async () => {
    const f = fixture();
    expect((await f.admit(root, { ...env, MULTICA_BUG_AGENT_ID: "" })).status).toBe(500);
    expect(f.queued).toHaveLength(0);
  });
  it.each(["U1", "U2"])("allows owner/requester %s to cancel Bug work using Bot cleanup", async user => {
    const f = fixture(); await f.admit(root); await f.consume(f.queued[0]?.body);
    await f.admit({ ...root, user, ts: "102.000001", thread_ts: root.ts, text: "<@UBUG> 取消" });
    expect(await (await f.consume(f.queued[1]?.body)).json()).toMatchObject({ action: "cancelled" });
    expect(f.runs[0].status).toBe("cancelled");
    expect(f.slackActors.length).toBeGreaterThan(0);
    expect(new Set(f.slackActors)).toEqual(new Set(["Bearer bug-token"]));
  });
  it("keeps requester cancellation retryable until an ambiguous create is recovered", async () => {
    const f = fixture(); f.loseCreateResponse();
    await f.admit(root); expect((await f.consume(f.queued[0]?.body)).status).toBe(503);
    await f.admit({ ...root, ts: "102.000001", thread_ts: root.ts, text: "<@UBUG> cancel" });
    const pending = await f.consume(f.queued[1]?.body);
    expect(pending.status).toBe(503);
    expect(await pending.json()).toMatchObject({ error: "cancellation_pending", retryable: true });
    await f.admit({ ...root, ts: "102.500000", thread_ts: root.ts, text: "<@UBUG> detail during cancellation" });
    expect(await (await f.consume(f.queued[2]?.body)).json()).toMatchObject({ action: "ignored" });
    f.restoreSearch();
    expect(await (await f.consume(f.queued[1]?.body)).json()).toMatchObject({ action: "cancelled" });
    expect(f.issues).toHaveLength(1); expect(f.runs[0].status).toBe("cancelled");
  });
  it("reuses legacy Review state when the queued payload has no route metadata", async () => {
    const f = fixture(); await f.admit({ ...root, text: "<@U1> review" });
    const { routeId, routeTarget, ...legacy } = f.queued[0]!.body;
    expect(await (await f.consume(legacy)).json()).toMatchObject({ action: "created" });
    expect(await (await f.consume(f.queued[0]!.body)).json()).toMatchObject({ action: "duplicate" });
    expect(f.issues).toHaveLength(1);
  });
  it("rejects a different requester's cancellation without poisoning later messages", async () => {
    const f = fixture(); await f.admit(root); await f.consume(f.queued[0]?.body);
    await f.admit({ ...root, user: "U3", ts: "102.000001", thread_ts: root.ts, text: "<@UBUG> cancel" });
    expect(await (await f.consume(f.queued[1]?.body)).json()).toMatchObject({ action: "ignored" });
    expect(f.runs[0].status).toBe("running");
    await f.admit({ ...root, ts: "103.000001", thread_ts: root.ts, text: "<@UBUG> extra detail" });
    expect(await (await f.consume(f.queued[2]?.body)).json()).toMatchObject({ action: "comment_persisted" });
  });
});
