import { createHmac } from "node:crypto";
import { Readable } from "node:stream";
import { afterEach, describe, expect, it, vi } from "vitest";
import { acceptSlack } from "../src/http.js";
import {
  serve,
  type VercelLikeRequest,
  type VercelLikeResponse,
} from "../src/vercel.js";

function response() {
  const res = {
    status: vi.fn(),
    json: vi.fn(),
    send: vi.fn(),
    setHeader: vi.fn(),
  };
  res.status.mockReturnValue(res);
  return res as unknown as VercelLikeResponse;
}

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
  KV_REST_API_URL: "https://kv.test",
  KV_REST_API_TOKEN: "test",
  QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test",
  QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
};

afterEach(() => vi.restoreAllMocks());

describe("Vercel request adapter", () => {
  it("preserves an unparsed request stream", async () => {
    const raw = '{"type":"url_verification","challenge":"ok"}';
    const req = Readable.from([raw]) as VercelLikeRequest;
    req.method = "POST";
    req.url = "/api/slack/events";
    req.headers = { "content-type": "application/json" };
    const res = response();

    await serve(req, res, async (request) =>
      Response.json({ raw: await request.text() }),
    );

    expect(res.send).toHaveBeenCalledWith(JSON.stringify({ raw }));
  });

  it.each([
    ["parsed string", '{"type":"url_verification","challenge":"ok"}'],
    ["parsed buffer", Buffer.from('{"type":"url_verification","challenge":"ok"}')],
  ])("preserves %s request bodies", async (_label, body) => {
    const req = Readable.from([]) as VercelLikeRequest & { body?: unknown };
    req.method = "POST";
    req.url = "/api/slack/events";
    req.headers = { "content-type": "application/json" };
    req.body = body;
    const res = response();

    await serve(req, res, async (request) =>
      Response.json({ raw: await request.text() }),
    );

    const expected =
      typeof body === "string"
        ? body
        : Buffer.isBuffer(body)
          ? body.toString("utf8")
          : JSON.stringify(body);
    expect(res.send).toHaveBeenCalledWith(
      JSON.stringify({ raw: expected }),
    );
  });

  it("prefers the raw stream when Vercel also exposes a parsed body", async () => {
    const raw = '{"event":{"text":"\\u4f60\\u597d"}}';
    const req = Readable.from([raw]) as VercelLikeRequest & { body?: unknown };
    req.method = "POST";
    req.url = "/api/slack/events";
    req.headers = { "content-type": "application/json" };
    req.body = { event: { text: "你好" } };
    const res = response();

    await serve(req, res, async (request) =>
      Response.json({ raw: await request.text() }),
    );

    expect(res.send).toHaveBeenCalledWith(JSON.stringify({ raw }));
  });

  it("preserves signed Slack bytes through serve before parsing JSON", async () => {
    const raw =
      '{"type":"event_callback","team_id":"T1","event":{"type":"message","channel":"C1","user":"U2","ts":"100.000001","text":"<@U1> \\u4f60\\u597d"}}';
    const timestamp = String(Math.floor(Date.now() / 1000));
    const req = Readable.from([raw]) as VercelLikeRequest & { body?: unknown };
    req.method = "POST";
    req.url = "/api/slack/events";
    req.headers = {
      "content-type": "application/json",
      "x-slack-request-timestamp": timestamp,
      "x-slack-signature":
        "v0=" +
        createHmac("sha256", "test")
          .update(`v0:${timestamp}:${raw}`)
          .digest("hex"),
    };
    req.body = JSON.parse(raw) as unknown;
    const res = response();
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));

    await serve(req, res, (request) => acceptSlack(request, env, fetcher));

    expect(res.status).toHaveBeenCalledWith(200);
    expect(res.send).toHaveBeenCalledWith(
      JSON.stringify({ action: "accepted", queueMessageId: "msg" }),
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("fails closed when only a parsed object remains", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const raw = '{"event":{"text":"\\u4f60\\u597d"}}';
    const timestamp = String(Math.floor(Date.now() / 1000));
    const req = Readable.from([]) as VercelLikeRequest & { body?: unknown };
    req.method = "POST";
    req.url = "/api/slack/events";
    req.headers = {
      "content-type": "application/json",
      "x-slack-request-timestamp": timestamp,
      "x-slack-signature":
        "v0=" +
        createHmac("sha256", "test")
          .update(`v0:${timestamp}:${raw}`)
          .digest("hex"),
    };
    req.body = JSON.parse(raw) as unknown;
    const res = response();
    const fetcher = vi.fn<typeof fetch>();

    await serve(req, res, (request) => acceptSlack(request, env, fetcher));

    expect(res.status).toHaveBeenCalledWith(401);
    expect(fetcher).not.toHaveBeenCalled();
    expect(console.warn).toHaveBeenCalledWith("vercel_adapter", {
      reason: "raw_body_unavailable",
      bodyType: "object",
    });
  });

  it("rejects parsed bodies over the limit before invoking the handler", async () => {
    const req = Readable.from([]) as VercelLikeRequest & { body?: unknown };
    req.method = "POST";
    req.headers = {};
    req.body = "x".repeat(256 * 1024 + 1);
    const res = response();
    const handler = vi.fn();

    await serve(req, res, handler);

    expect(res.status).toHaveBeenCalledWith(413);
    expect(handler).not.toHaveBeenCalled();
  });
});
