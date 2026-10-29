import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import type { SlackThreadEvent } from "./thread-router.js";
import type { SlackReplyContext } from "./multica-api.js";

const PAYLOAD_START = "<!-- relay-payload:v1 -->";
const PAYLOAD_END = "<!-- /relay-payload -->";
function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function decodeSlack(text: string): string {
  return text
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

export function formatTaskTitle(
  event: SlackThreadEvent,
  scope: string,
): string {
  if (event.taskType === "release_approval") {
    const match = event.releaseApproval.prUrl.match(
      /^https:\/\/github\.com\/[^/]+\/([^/]+)\/pull\/([1-9]\d*)$/u,
    );
    if (!match) throw new Error("invalid_event");
    const identity = createHash("sha256")
      .update(
        `${scope}:${event.teamId}:${event.channelId}:release_approval:${event.releaseApproval.prUrl}`,
      )
      .digest("hex")
      .slice(0, 16);
    return `发布候选自动批准 · ${match[1]} #${match[2]} · ${event.releaseApproval.version} · [${identity}]`;
  }
  const pr = event.text.match(
    /https:\/\/github\.com\/[^/\s<>|]+\/([^/\s<>|]+)\/pull\/(\d+)(?=[/?#\s<>|]|$)/,
  );
  const summary = decodeSlack(
    event.text
      .replace(/<@[^>]+>|<!subteam\^[^>]+>/g, "")
      .replace(/<https?:\/\/[^>]+>|https?:\/\/\S+/g, ""),
  )
    .replace(/^\s*cc\b\s*/i, "")
    .replace(/\s+/g, " ")
    .trim();
  const label = summary || (pr ? `${pr[1]} #${pr[2]}` : "自动任务");
  // 同一正文的不同线程或 Agent scope 仍须有不同标题，避免服务端同标题去重。
  const identity = createHash("sha256")
    .update(`${scope}:${event.teamId}:${event.channelId}:${event.threadTs}`)
    .digest("hex")
    .slice(0, 16);
  const chars = Array.from(label);
  return `Slack mention · ${chars.slice(0, 80).join("")}${chars.length > 80 ? "..." : ""} · [${identity}]`;
}

function clip(text: string, bytes: number): string {
  let result = "", used = 0;
  for (const char of text) {
    const size = Buffer.byteLength(char);
    if (used + size > bytes) break;
    result += char;
    used += size;
  }
  return result;
}

export function compactEvent(event: SlackThreadEvent): SlackThreadEvent {
  // Slack 入站字段不具有 Relay 元数据权限，只复制事件白名单。
  const base = {
    teamId: event.teamId,
    channelId: event.channelId,
    messageTs: event.messageTs,
    threadTs: event.threadTs,
    senderUserId: event.senderUserId,
    text: event.text,
    ...(event.routeId ? { routeId: event.routeId, routeTarget: event.routeTarget } : {}),
  };
  const rest: SlackThreadEvent =
    event.taskType === "release_approval"
      ? {
          ...base,
          taskType: "release_approval",
          operation: "dispatch",
          releaseApproval: {
            version: event.releaseApproval.version,
            prUrl: event.releaseApproval.prUrl,
            sourceAppId: event.releaseApproval.sourceAppId,
            sourceBotId: event.releaseApproval.sourceBotId,
          },
        }
      : {
          ...base,
          ...(event.taskType ? { taskType: event.taskType } : {}),
          ...(event.operation ? { operation: event.operation } : {}),
          mention: { type: event.mention.type, id: event.mention.id },
        };
  if (event.files === undefined) return rest;
  const files = Array.isArray(event.files) ? event.files : [];
  return {
    ...rest,
    files: files.filter(object).filter(file => typeof file.id === "string").slice(0, 5).map(file => ({
      id: clip(file.id as string, 128),
      name: clip(typeof file.name === "string" ? file.name : "", 256),
      mime: clip(typeof file.mime === "string" ? file.mime : typeof file.mimetype === "string" ? file.mimetype : "", 128),
      ...(typeof file.size === "number" && Number.isFinite(file.size) ? { size: file.size } : {}),
      contentStatus: "not_loaded",
    })),
    ...(event.filesTruncated || files.length > 5 ? { filesTruncated: true } : {}),
  };
}

function quoteMessage(text: string): string {
  // 原文只作为引用展示，不让消息里的 HTML、围栏或链接语法改写描述结构。
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/[\\`*_[\]{}()!#|~]/g, "\\$&")
    .split(/\r?\n/)
    .map((line) => `> ${line}`)
    .join("\n");
}

export function formatTaskDescription(
  event: SlackThreadEvent,
  marker: string,
  followup = false,
  replyContext?: SlackReplyContext,
): string {
  const payload = compactEvent(event);
  const timestamp = Number(event.messageTs) * 1000;
  const date = new Date(timestamp);
  const time = Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("sv-SE", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      }).format(date) + "（Asia/Shanghai）"
    : "未知";
  const channelUrl =
    "https://slack.com/app_redirect?" +
    new URLSearchParams({
      team: event.teamId,
      channel: event.channelId,
    });
  const envelope = { eventPayload: payload, ...(replyContext ? { replyContext } : {}) };
  const serialize = (indent?: number) => JSON.stringify(envelope, null, indent)
    .replace(/@/g, "\\u0040").replace(/</g, "\\u003c").replace(/>/g, "\\u003e");
  const compact = serialize();
  if (Buffer.byteLength(compact) > 48 * 1024) throw new Error("relay_payload_too_large");
  const json = serialize(2);
  const fence = "`".repeat(
    (json.match(/`+/g) ?? []).reduce(
      (length, run) => Math.max(length, run.length + 1),
      3,
    ),
  );
  const render = (value: string) => [
    marker,
    followup ? "## Slack thread 后续消息" : "## Slack 原始消息",
    quoteMessage(decodeSlack(clip(event.text, 4096))) + (Buffer.byteLength(event.text) > 4096 ? "\n> （展示已截断，完整请求见数据区）" : ""),
    "## 来源",
    `- Slack 频道：[打开频道](${channelUrl})`,
    `- 触发时间：${time}`,
    `- 附件：${Array.isArray(payload.files) ? payload.files.length : 0}${payload.filesTruncated ? "+" : ""} 个`,
    "## 事件上下文",
    PAYLOAD_START,
    `${fence}json\n${value}\n${fence}`,
    PAYLOAD_END,
  ]
    .join("\n\n")
    .replace(marker + "\n\n", marker + "\n");
  let description = render(json);
  if (Buffer.byteLength(description) > 64 * 1024) description = render(compact);
  if (Buffer.byteLength(description) > 64 * 1024) throw new Error("task_presentation_too_large");
  return description;
}

export interface RecoveredMessage {
  senderUserId?: string;
  teamId: string;
  channelId: string;
  threadTs: string;
  messageTs: string;
  taskType?: "mention_review" | "bug_resolver" | "release_approval";
  releaseApproval?: {
    version: string;
    prUrl: string;
    sourceAppId: string;
    sourceBotId: string;
  };
}

export function readTaskMessage(description: string): RecoveredMessage {
  const body = description
    .slice(description.indexOf("\n") + 1)
    .replace(/\r\n/g, "\n");
  let json = body;
  const lines = body.split("\n");
  const starts = lines.filter((line) => line === PAYLOAD_START);
  const ends = lines.filter((line) => line === PAYLOAD_END);
  if (starts.length || ends.length) {
    if (starts.length !== 1 || ends.length !== 1)
      throw new Error("invalid_thread_state");
    const start = lines.indexOf(PAYLOAD_START);
    const end = lines.indexOf(PAYLOAD_END);
    const block = lines
      .slice(start + 1, end)
      .join("\n")
      .trim();
    const match = block.match(/^(`{3,})json\n([\s\S]*)\n\1$/);
    if (end < start || !match) throw new Error("invalid_thread_state");
    json = match[2];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    throw new Error("invalid_thread_state");
  }
  const event = object(parsed) ? parsed.eventPayload : undefined;
  if (
    !object(event) ||
    !["teamId", "channelId", "threadTs", "messageTs"].every(
      (key) => typeof event[key] === "string" && event[key].length > 0,
    ) ||
    !/^\d+\.\d+$/.test(event.threadTs as string) ||
    !/^\d+\.\d+$/.test(event.messageTs as string)
  )
    throw new Error("invalid_thread_state");
  if (event.taskType === "release_approval") {
    const approval = event.releaseApproval;
    if (
      !object(approval) ||
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
      throw new Error("invalid_thread_state");
  } else if (
    event.taskType !== undefined &&
    event.taskType !== "mention_review" && event.taskType !== "bug_resolver"
  )
    throw new Error("invalid_thread_state");
  return event as unknown as RecoveredMessage;
}
