import { Buffer } from 'node:buffer';
import { Receiver } from "@upstash/qstash";
import { loadRelayConfig, type RelayConfig } from "./config.js";
import { verifySlackSignature } from "./signature.js";
import {
  findTargetMention,
  isCancelCommand,
  canCancelTask,
  isSupportedMessage,
  isOwnerGroupWithoutSelfMention,
  type SlackMessageEvent,
} from "./mentions.js";
import {
  findReleaseApproval,
  type ReleaseApprovalMatch,
} from "./release-approval.js";
import {
  routeSlackThreadEvent,
  digest,
  threadKey,
  messageKey,
  type SlackThreadEvent,
} from "./thread-router.js";
import { assignee, threadScopeId } from "./multica-api.js";
import { UpstashThreadStore } from "./thread-store.js";
import { compactEvent } from "./task-presentation.js";

export function json(value: unknown, status = 200): Response {
  return Response.json(value, { status });
}
async function readBody(request: Request): Promise<string> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.length;
      if (bytes > 256 * 1024) throw new Error("body_too_large");
      parts.push(part.value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  return Buffer.concat(parts).toString("utf8");
}
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function routeConfig(id: "review" | "bug", config: RelayConfig): RelayConfig | undefined {
  if (id === "review") return config;
  const bug = config.bugRoute;
  if (!bug) return undefined;
  return { ...config, ...bug, targetUserIds: new Set([bug.botUserId]), targetSubteamIds: new Set() };
}
function routeTarget(config: RelayConfig): string {
  const target = assignee(config);
  return digest(JSON.stringify([config.multicaApiBaseUrl, config.multicaWorkspaceId,
    config.multicaProjectId, target.type, target.id, threadScopeId(config)]));
}
function admitted(event: SlackThreadEvent, config: RelayConfig): boolean {
  if (event.taskType === "release_approval") {
    if (
      event.teamId !== config.teamId ||
      config.blockedChannelIds.has(event.channelId) ||
      event.operation !== "dispatch" ||
      event.threadTs !== event.messageTs
    )
      return false;
    const current = findReleaseApproval(
      {
        type: "message",
        channel: event.channelId,
        user: event.senderUserId,
        bot_id: event.releaseApproval.sourceBotId,
        app_id: event.releaseApproval.sourceAppId,
        ts: event.messageTs,
        text: event.text,
      },
      config,
    );
    return (
      current?.version === event.releaseApproval.version &&
      current.prUrl === event.releaseApproval.prUrl &&
      current.sourceAppId === event.releaseApproval.sourceAppId &&
      current.sourceBotId === event.releaseApproval.sourceBotId
    );
  }
  return (
    event.teamId === config.teamId &&
    (config.allowAllChannels ||
      config.allowedChannelIds.has(event.channelId)) &&
    !config.blockedChannelIds.has(event.channelId) &&
    (config.allowAllSenders ||
      config.allowedSenderIds.has(event.senderUserId)) &&
    !config.blockedSenderIds.has(event.senderUserId) &&
    !!findTargetMention(
      event.text,
      config.targetUserIds,
      config.targetSubteamIds,
    )
  );
}
function parsedEvent(value: unknown): SlackThreadEvent {
  const invalidBase =
    !record(value) ||
    ["teamId", "channelId", "senderUserId"].some(
      (k) =>
        typeof value[k] !== "string" ||
        !/^[A-Z][A-Z0-9]+$/u.test(value[k] as string),
    ) ||
    ["messageTs", "threadTs"].some(
      (k) =>
        typeof value[k] !== "string" || !/^\d+\.\d{1,6}$/u.test(value[k] as string),
    ) ||
    typeof value.text !== "string";
  if (invalidBase || !record(value)) throw new Error("invalid_event");
  if (value.routeId !== undefined || value.routeTarget !== undefined) {
    if (!["review", "bug"].includes(String(value.routeId)) ||
      typeof value.routeTarget !== "string" || !/^[a-f0-9]{64}$/u.test(value.routeTarget) ||
      (value.routeId === "bug" && value.taskType === "release_approval"))
      throw new Error("invalid_event");
  }
  if (value.taskType === "release_approval") {
    const approval = value.releaseApproval;
    if (
      value.operation !== "dispatch" ||
      value.mention !== undefined ||
      !record(approval) ||
      typeof approval.version !== "string" ||
      !/^v\d+\.\d+\.\d+$/u.test(approval.version) ||
      typeof approval.prUrl !== "string" ||
      !/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9]\d*$/u.test(
        approval.prUrl,
      ) ||
      typeof approval.sourceAppId !== "string" ||
      typeof approval.sourceBotId !== "string" ||
      !/^[A-Z][A-Z0-9]+$/u.test(approval.sourceAppId) ||
      !/^[A-Z][A-Z0-9]+$/u.test(approval.sourceBotId)
    )
      throw new Error("invalid_event");
    return value as unknown as SlackThreadEvent;
  }
  if (
    (value.taskType !== undefined && value.taskType !== "mention_review" && value.taskType !== "bug_resolver") ||
    ((value.routeId === "bug") !== (value.taskType === "bug_resolver")) ||
    (value.operation !== undefined &&
      value.operation !== "dispatch" &&
      value.operation !== "cancel") ||
    !record(value.mention) ||
    !["user", "subteam"].includes(String(value.mention.type)) ||
    typeof value.mention.id !== "string"
  )
    throw new Error("invalid_event");
  return value as unknown as SlackThreadEvent;
}

function releasePayload(
  body: Record<string, unknown>,
  event: SlackMessageEvent,
  approval: ReleaseApprovalMatch,
): SlackThreadEvent {
  return parsedEvent({
    taskType: "release_approval",
    teamId: body.team_id,
    channelId: event.channel,
    messageTs: event.ts,
    threadTs: event.ts,
    senderUserId: event.user,
    text: event.text,
    operation: "dispatch",
    releaseApproval: approval,
  });
}
function reason(error: unknown): string {
  if (
    error instanceof Error &&
    ["TimeoutError", "AbortError"].includes(error.name)
  )
    return "timeout";
  const codes = [
    "body_too_large",
    "invalid_event",
    "thread_lock_busy",
    "ambiguous_issue_create",
    "ambiguous_comment_create",
    "invalid_thread_state",
    "ambiguous_issue_mapping",
    "invalid_issue_scope",
    "relay_payload_too_large",
    "task_presentation_too_large",
    "comment_lookup_limit",
    "multica_http_error",
    "invalid_multica_response",
    "invalid_comment_cursor",
    "cancellation_pending",
    "cancellation_run_missing",
    "cancellation_new_run",
    "reaction_cleanup_failed",
  ];
  return error instanceof Error && codes.includes(error.message)
    ? error.message
    : "upstream_failed";
}
const permanentConsumerErrors = new Set([
  "invalid_event",
  "invalid_thread_state",
  "ambiguous_issue_mapping",
  "invalid_issue_scope",
  "relay_payload_too_large",
  "task_presentation_too_large",
  "comment_lookup_limit",
]);
export function consumerFailure(code: string): Response {
  if (permanentConsumerErrors.has(code))
    return json({ action: "rejected", error: code, retryable: false });
  return json({ error: code, retryable: true }, 503);
}
export async function acceptSlack(
  request: Request,
  env: NodeJS.ProcessEnv = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  const start = Date.now();
  if (request.method !== "POST")
    return json({ error: "method_not_allowed" }, 405);
  let config: RelayConfig;
  try {
    config = loadRelayConfig(env);
  } catch {
    return json({ error: "relay_not_configured" }, 500);
  }
  let raw: string;
  try {
    raw = await readBody(request);
  } catch {
    return json({ error: "body_too_large" }, 413);
  }
  if (
    !verifySlackSignature(
      raw,
      request.headers.get("x-slack-request-timestamp") ?? undefined,
      request.headers.get("x-slack-signature") ?? undefined,
      config.signingSecret,
    )
  ) {
    console.warn("relay_admission", {
      reason: "invalid_slack_signature",
      hasTimestamp: request.headers.has("x-slack-request-timestamp"),
      hasSignature: request.headers.has("x-slack-signature"),
      contentType: request.headers.get("content-type") ?? "missing",
      bodyBytes: Buffer.byteLength(raw),
    });
    return json({ error: "invalid_slack_signature" }, 401);
  }
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  if (!record(body)) return json({ error: "invalid_envelope" }, 400);
  if (body.type === "url_verification" && typeof body.challenge === "string")
    return json({ challenge: body.challenge });
  if (
    body.type !== "event_callback" ||
    !record(body.event)
  )
    return json({ action: "ignored" });
  const event = body.event as SlackMessageEvent;
  const releaseApproval = findReleaseApproval(event, config);
  if (releaseApproval) {
    let payload: SlackThreadEvent;
    try {
      payload = { ...releasePayload(body, event, releaseApproval), routeId: "review", routeTarget: routeTarget(config) };
    } catch {
      return json({ error: "invalid_event" }, 400);
    }
    if (!admitted(payload, config))
      return json({ action: "ignored", reason: "not_allowed" });
    return publish(payload, config, start, fetchImpl);
  }
  if (!isSupportedMessage(event) || event.user === config.bugRoute?.botUserId)
    return json({ action: "ignored" });
  const routeId = config.bugRoute && findTargetMention(event.text as string,
    new Set([config.bugRoute.botUserId]), new Set()) ? "bug" : "review";
  config = routeConfig(routeId, config)!;
  const mention = findTargetMention(
    event.text as string,
    config.targetUserIds,
    config.targetSubteamIds,
  );
  if (!mention) return json({ action: "ignored", reason: "not_addressed" });
  let payload: SlackThreadEvent;
  try {
    payload = parsedEvent({
      routeId, routeTarget: routeTarget(config),
      ...(routeId === "bug" ? { taskType: "bug_resolver" } : {}),
      teamId: body.team_id,
      channelId: event.channel,
      messageTs: event.ts,
      threadTs: event.thread_ts ?? event.ts,
      senderUserId: event.user,
      text: event.text,
      mention,
      ...(event.files ? { files: event.files } : {}),
    });
  } catch {
    return json({ error: "invalid_event" }, 400);
  }
  payload = compactEvent(payload);
  if (!admitted(payload, config))
    return json({ action: "ignored", reason: "not_allowed" });
  if (payload.routeId !== "bug" && isOwnerGroupWithoutSelfMention(
    payload.text,
    payload.senderUserId,
    config.ownerSlackUserId,
    config.targetSubteamIds,
  ))
    return json({ action: "ignored", reason: "self_group_ignored" });
  payload.operation = isCancelCommand(
    payload.text,
    config.targetUserIds,
    config.targetSubteamIds,
    config.cancelKeywords,
  )
    ? "cancel"
    : "dispatch";
  if (
    payload.operation === "cancel" && payload.routeId !== "bug" &&
    !canCancelTask(payload.senderUserId, config.targetUserIds)
  )
    return json({ action: "ignored", reason: "cancel_not_allowed" });
  return publish(payload, config, start, fetchImpl);
}

async function publish(
  payload: SlackThreadEvent,
  config: RelayConfig,
  start: number,
  fetchImpl: typeof fetch,
): Promise<Response> {
  try {
    const response = await fetchImpl(
      config.queueUrl + "/v2/publish/" + config.consumerUrl,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${config.queueToken}`,
          "content-type": "application/json",
          "Upstash-Deduplication-Id": digest(
            config.consumerUrl + ":" + messageKey(payload),
          ),
          "Upstash-Retries": "3",
          "Upstash-Timeout": "50s",
          "Upstash-Flow-Control-Key": digest(
            config.consumerUrl + ":" + (payload.routeId === "bug" ? "bug:" : "") + threadKey(payload),
          ),
          "Upstash-Flow-Control-Value": "parallelism=1",
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(2000),
      },
    );
    if (!response.ok) throw new Error("queue_publish_failed");
    const queued: unknown = await response.json();
    if (!record(queued) || typeof queued.messageId !== "string")
      throw new Error("invalid_queue_response");
    console.info("relay_admission", {
      eventId: digest(messageKey(payload)),
      queueMessageId: queued.messageId,
      durationMs: Date.now() - start,
    });
    return json({ action: "accepted", queueMessageId: queued.messageId });
  } catch (error) {
    console.warn("relay_admission", {
      eventId: digest(messageKey(payload)),
      reason: reason(error),
      durationMs: Date.now() - start,
    });
    return json({ error: "queue_unavailable", retryable: true }, 503);
  }
}
export async function consumeQueue(
  request: Request,
  env: NodeJS.ProcessEnv = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (request.method !== "POST")
    return json({ error: "method_not_allowed" }, 405);
  let config: RelayConfig;
  try {
    config = loadRelayConfig(env);
  } catch {
    return json({ error: "relay_not_configured" }, 500);
  }
  let raw: string;
  try {
    raw = await readBody(request);
  } catch {
    return json({ error: "body_too_large" }, 413);
  }
  try {
    const valid = await new Receiver({
      currentSigningKey: config.queueCurrentSigningKey,
      nextSigningKey: config.queueNextSigningKey,
      devMode: false,
    }).verify({
      body: raw,
      signature: request.headers.get("upstash-signature") ?? "",
      url: config.consumerUrl,
    });
    if (!valid) return json({ error: "invalid_queue_signature" }, 401);
  } catch {
    return json({ error: "invalid_queue_signature" }, 401);
  }
  let event: SlackThreadEvent;
  try {
    event = parsedEvent(JSON.parse(raw));
  } catch {
    return consumerFailure("invalid_event");
  }
  if (event.routeId) {
    const selected = routeConfig(event.routeId, config);
    if (!selected || event.routeTarget !== routeTarget(selected))
      return json({ action: "ignored", reason: "route_changed" });
    config = selected;
  }
  if (!admitted(event, config))
    return json({ action: "ignored", reason: "policy_changed" });
  if (
    event.routeId !== "bug" && event.taskType !== "release_approval" &&
    isOwnerGroupWithoutSelfMention(
      event.text,
      event.senderUserId,
      config.ownerSlackUserId,
      config.targetSubteamIds,
    )
  )
    return json({ action: "ignored", reason: "self_group_ignored" });
  // 新消息保留入队时的指令分类，配置变更不能把排队中的取消变成启动任务。
  if (event.taskType !== "release_approval")
    event.operation ??= isCancelCommand(
      event.text,
      config.targetUserIds,
      config.targetSubteamIds,
      config.cancelKeywords,
    )
      ? "cancel"
      : "dispatch";
  if (
    event.operation === "cancel" && event.routeId !== "bug" &&
    !canCancelTask(event.senderUserId, config.targetUserIds)
  )
    return json({ action: "ignored", reason: "cancel_not_allowed" });
  const start = Date.now(),
    deadline = AbortSignal.timeout(45000);
  const boundedFetch: typeof fetch = (input, init = {}) => {
    deadline.throwIfAborted();
    return fetchImpl(input, {
      ...init,
      signal: init.signal ? AbortSignal.any([init.signal, deadline]) : deadline,
    });
  };
  try {
    const result = await routeSlackThreadEvent(
      event,
      {
        ...config,
        slackBotUserId: event.routeId === "bug" ? config.bugRoute?.botUserId : undefined,
        store: new UpstashThreadStore(
          config.kvRestApiUrl,
          config.kvRestApiToken,
          boundedFetch,
          // Release remains bounded to 2s even after the dispatch deadline expires.
          fetchImpl,
        ),
      },
      boundedFetch,
    );
    console.info("relay_dispatch", {
      ...result,
      eventId: digest(messageKey(event)),
      durationMs: Date.now() - start,
    });
    return json(result);
  } catch (error) {
    const code = reason(error);
    console.warn("relay_dispatch", {
      eventId: digest(messageKey(event)),
      reason: code,
      durationMs: Date.now() - start,
    });
    // Acknowledged rejection stops retries for input that cannot fit unchanged.
    return consumerFailure(code);
  }
}
