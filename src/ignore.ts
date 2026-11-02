import { timingSafeEqual } from "node:crypto";
import {
  ApiError,
  getIssue,
  isActiveRun,
  listIssueRuns,
  updateIssueStatus,
  type ApiConfig,
} from "./multica-api.js";

const reasons = new Set([
  "not_review_request",
  "policy_ignored",
  "system_message",
]);
const multicaId =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;

interface IgnoreConfig extends ApiConfig {
  ignoreToken: string;
}

function loadIgnoreConfig(env: NodeJS.ProcessEnv): IgnoreConfig {
  const required = (key: string): string => {
    const value = env[key]?.trim();
    if (!value) throw new Error("relay_not_configured");
    return value;
  };
  // Ignoring is a single-Agent decision; it must not close shared squad work.
  const assigneeType = env.MULTICA_ASSIGNEE_TYPE?.trim() || "agent";
  if (assigneeType !== "agent") throw new Error("relay_not_configured");
  const agentId = env.MULTICA_ASSIGNEE_ID?.trim() || required("MULTICA_AGENT_ID");
  const baseUrl = required("MULTICA_API_BASE_URL");
  const parsed = new URL(baseUrl);
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.hash ||
    parsed.search
  )
    throw new Error("relay_not_configured");
  return {
    ignoreToken: required("RELAY_IGNORE_TOKEN"),
    multicaApiBaseUrl: baseUrl.replace(/\/+$/u, ""),
    multicaApiToken: required("MULTICA_API_TOKEN"),
    multicaWorkspaceId: required("MULTICA_WORKSPACE_ID"),
    multicaProjectId: required("MULTICA_PROJECT_ID"),
    multicaAgentId: agentId,
  };
}

function authorized(header: string | null, expected: string): boolean {
  const prefix = "Bearer ";
  if (!header?.startsWith(prefix)) return false;
  const actual = Buffer.from(header.slice(prefix.length));
  const wanted = Buffer.from(expected);
  return actual.length === wanted.length && timingSafeEqual(actual, wanted);
}

function json(value: unknown, status = 200): Response {
  return Response.json(value, { status });
}

export async function ignoreIssue(
  request: Request,
  env: NodeJS.ProcessEnv = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (request.method !== "POST")
    return json({ error: "method_not_allowed" }, 405);
  let config: IgnoreConfig;
  try {
    config = loadIgnoreConfig(env);
  } catch {
    return json({ error: "relay_not_configured" }, 500);
  }
  if (!authorized(request.headers.get("authorization"), config.ignoreToken))
    return json({ error: "unauthorized" }, 401);

  let body: unknown;
  try {
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > 8 * 1024)
      return json({ error: "body_too_large" }, 413);
    body = JSON.parse(Buffer.from(bytes).toString("utf8"));
  } catch {
    return json({ error: "invalid_request" }, 400);
  }
  if (
    !body ||
    typeof body !== "object" ||
    Array.isArray(body) ||
    typeof (body as Record<string, unknown>).issueId !== "string" ||
    typeof (body as Record<string, unknown>).runId !== "string" ||
    typeof (body as Record<string, unknown>).reason !== "string"
  )
    return json({ error: "invalid_request" }, 400);
  const { issueId, runId, reason } = body as {
    issueId: string;
    runId: string;
    reason: string;
  };
  if (
    !multicaId.test(issueId) ||
    !multicaId.test(runId) ||
    !reasons.has(reason)
  )
    return json({ error: "invalid_request" }, 400);

  try {
    const issue = await getIssue(config, issueId, fetchImpl);
    if (issue.status === "done")
      return json({ action: "already_ignored", issueId, status: "done" });
    if (!issue.status || !["todo", "in_progress"].includes(issue.status))
      return json({ error: "issue_not_ignorable" }, 409);

    const active = (await listIssueRuns(config, issueId, fetchImpl)).filter(
      isActiveRun,
    );
    if (active.length > 1) return json({ error: "concurrent_run" }, 409);
    const current = active[0];
    if (
      !current ||
      current.id !== runId ||
      current.agent_id !== config.multicaAgentId
    )
      return json({ error: "run_not_current" }, 409);

    await updateIssueStatus(config, issueId, "done", fetchImpl);
    const saved = await getIssue(config, issueId, fetchImpl);
    if (saved.status !== "done")
      return json({ error: "status_not_confirmed" }, 502);
    console.info("relay_ignore", { issueId, runId, reason });
    return json({ action: "ignored", issueId, status: "done" });
  } catch (error) {
    if (error instanceof Error && error.message === "invalid_issue_scope")
      return json({ error: "invalid_issue_scope" }, 403);
    if (error instanceof ApiError)
      console.warn("relay_ignore", { reason: "multica_http_error" });
    else console.warn("relay_ignore", { reason: "upstream_failed" });
    return json({ error: "upstream_failed" }, 502);
  }
}
