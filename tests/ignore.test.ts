import { describe, expect, it, vi } from "vitest";
import { ignoreIssue } from "../src/ignore.js";

const env = {
  RELAY_IGNORE_TOKEN: "ignore-secret",
  MULTICA_API_BASE_URL: "https://multica.test",
  MULTICA_API_TOKEN: "multica-secret",
  MULTICA_WORKSPACE_ID: "ws",
  MULTICA_PROJECT_ID: "project",
  MULTICA_AGENT_ID: "agent",
};

const issueId = "01a0b3f9-86b0-7bdf-8f02-af2ee5e26c98";
const runId = "01a0b3f9-86c0-7265-adfe-f13d23a67771";

function request(
  body: unknown,
  token = env.RELAY_IGNORE_TOKEN,
): Request {
  return new Request("https://relay.test/api/multica/ignore", {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

function fixture(options: {
  issueStatus?: string;
  runs?: Array<Record<string, unknown>>;
  readbackStatus?: string;
} = {}) {
  const calls: Array<{ method: string; url: string; body: unknown }> = [];
  let issueReads = 0;
  const runs = options.runs ?? [
    {
      id: runId,
      issue_id: issueId,
      workspace_id: "ws",
      agent_id: "agent",
      status: "running",
    },
  ];
  const fetcher = vi.fn<typeof fetch>(async (input, init = {}) => {
    const url = String(input);
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    if (url.endsWith(`/api/issues/${issueId}/task-runs`))
      return Response.json(runs);
    if (url.endsWith(`/api/issues/${issueId}`) && method === "GET") {
      issueReads++;
      return Response.json({
        id: issueId,
        title: "Slack mention",
        project_id: "project",
        assignee_type: "agent",
        assignee_id: "agent",
        status:
          issueReads === 1
            ? options.issueStatus ?? "todo"
            : options.readbackStatus ?? "done",
      });
    }
    if (url.endsWith(`/api/issues/${issueId}`) && method === "PUT")
      return Response.json({ id: issueId, title: "Slack mention", status: "done" });
    throw new Error(`unexpected request: ${method} ${url}`);
  });
  return { fetcher, calls };
}

describe("ignored Multica task finalizer", () => {
  it("accepts the new Agent assignment configuration without the legacy variable", async () => {
    const f = fixture();
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "not_review_request" }),
      { ...env, MULTICA_AGENT_ID: undefined, MULTICA_ASSIGNEE_TYPE: "agent", MULTICA_ASSIGNEE_ID: "agent" },
      f.fetcher,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ action: "ignored", status: "done" });
  });

  it.each(["squad", "unknown"])("rejects %s assignment before accessing shared work", async (type) => {
    const f = fixture();
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "not_review_request" }),
      { ...env, MULTICA_ASSIGNEE_TYPE: type, MULTICA_ASSIGNEE_ID: "team" },
      f.fetcher,
    );
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "relay_not_configured" });
    expect(f.fetcher).not.toHaveBeenCalled();
  });

  it("marks the scoped issue done for its only active run and verifies readback", async () => {
    const f = fixture();
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "not_review_request" }),
      env,
      f.fetcher,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      action: "ignored",
      issueId,
      status: "done",
    });
    expect(f.calls).toEqual([
      {
        method: "GET",
        url: `https://multica.test/api/issues/${issueId}`,
        body: undefined,
      },
      {
        method: "GET",
        url: `https://multica.test/api/issues/${issueId}/task-runs`,
        body: undefined,
      },
      {
        method: "PUT",
        url: `https://multica.test/api/issues/${issueId}`,
        body: { status: "done", suppress_run: true },
      },
      {
        method: "GET",
        url: `https://multica.test/api/issues/${issueId}`,
        body: undefined,
      },
    ]);
  });

  it("rejects missing or incorrect caller authentication before Multica access", async () => {
    const f = fixture();
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "not_review_request" }, "wrong"),
      env,
      f.fetcher,
    );
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "unauthorized" });
    expect(f.fetcher).not.toHaveBeenCalled();
  });

  it("rejects a stale or unrelated run", async () => {
    const f = fixture({
      runs: [
        {
          id: "01a0b3dc-883b-71b8-bd06-790c87818650",
          issue_id: issueId,
          workspace_id: "ws",
          agent_id: "agent",
          status: "running",
        },
      ],
    });
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "not_review_request" }),
      env,
      f.fetcher,
    );
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "run_not_current" });
    expect(f.calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("rejects concurrent active runs instead of closing shared work", async () => {
    const f = fixture({
      runs: [
        {
          id: runId,
          issue_id: issueId,
          workspace_id: "ws",
          agent_id: "agent",
          status: "running",
        },
        {
          id: "01a0b3dc-883b-71b8-bd06-790c87818650",
          issue_id: issueId,
          workspace_id: "ws",
          agent_id: "agent",
          status: "queued",
        },
      ],
    });
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "not_review_request" }),
      env,
      f.fetcher,
    );
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "concurrent_run" });
    expect(f.calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("treats an already done scoped issue as idempotent success", async () => {
    const f = fixture({ issueStatus: "done", runs: [] });
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "policy_ignored" }),
      env,
      f.fetcher,
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      action: "already_ignored",
      issueId,
      status: "done",
    });
    expect(f.calls).toHaveLength(1);
  });

  it.each([
    [{ issueId, runId, reason: "completed" }, "invalid_request"],
    [{ issueId: "bad", runId, reason: "not_review_request" }, "invalid_request"],
    [{ issueId, runId: "bad", reason: "not_review_request" }, "invalid_request"],
  ])("rejects malformed or unsupported payload %j", async (body, error) => {
    const f = fixture();
    const response = await ignoreIssue(request(body), env, f.fetcher);
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error });
    expect(f.fetcher).not.toHaveBeenCalled();
  });

  it("fails when Multica does not confirm done after the write", async () => {
    const f = fixture({ readbackStatus: "todo" });
    const response = await ignoreIssue(
      request({ issueId, runId, reason: "system_message" }),
      env,
      f.fetcher,
    );
    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "status_not_confirmed" });
  });
});
