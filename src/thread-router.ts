import { createHash, randomUUID } from "node:crypto";
import {
  ApiError,
  threadScopeId,
  validateMappedIssue,
  createIssue,
  findIssue,
  createComment,
  findComment,
  getSlackReplyContext,
  type ApiConfig,
  type SlackReplyContext,
} from "./multica-api.js";
import type { MentionMatch } from "./mentions.js";
import type { ReleaseApprovalMatch } from "./release-approval.js";
import {
  cancelThread,
  compareTimestamp,
  maxTimestamp,
  type CancellationState,
} from "./cancellation.js";
import { type ThreadStore } from "./thread-store.js";
import {
  formatTaskTitle,
  formatTaskDescription,
  readTaskMessage,
  compactEvent,
} from "./task-presentation.js";

interface SlackEventBase {
  teamId: string;
  channelId: string;
  messageTs: string;
  threadTs: string;
  senderUserId: string;
  text: string;
  files?: unknown;
  filesTruncated?: boolean;
}
export interface SlackMentionEvent extends SlackEventBase {
  taskType?: "mention_review";
  mention: MentionMatch;
  operation?: "dispatch" | "cancel";
}
export interface SlackReleaseApprovalEvent extends SlackEventBase {
  taskType: "release_approval";
  releaseApproval: ReleaseApprovalMatch;
  operation: "dispatch";
}
export type SlackThreadEvent = SlackMentionEvent | SlackReleaseApprovalEvent;
export interface ThreadRouterConfig extends ApiConfig {
  store: ThreadStore;
  slackReactionToken?: string;
}
export interface ThreadState {
  version: 2;
  rootMessageKey: string;
  issueId?: string;
  creating: boolean;
  lastMessageTs?: string;
  ignoredThrough?: string;
  reactionMessages?: string[];
  reactionHistoryKnown?: boolean;
  cancellation?: CancellationState;
}
interface MessageState {
  phase: "writing" | "done" | "rejected";
}
export interface ThreadRouteResult {
  action:
    | "created"
    | "comment_persisted"
    | "duplicate"
    | "cancelled"
    | "no_active_run"
    | "ignored";
  issueId?: string;
}
export const STATE_TTL_SECONDS = 90 * 24 * 60 * 60;
export function threadKey(event: {
  teamId: string;
  channelId: string;
  threadTs: string;
  taskType?: string;
  releaseApproval?: { prUrl: string };
}): string {
  if (event.taskType === "release_approval") {
    if (!event.releaseApproval?.prUrl) throw new Error("invalid_thread_state");
    return `${event.teamId}:${event.channelId}:release_approval:${event.releaseApproval.prUrl}`;
  }
  return `${event.teamId}:${event.channelId}:${event.threadTs}`;
}
export function messageKey(event: SlackThreadEvent): string {
  return `${event.teamId}:${event.channelId}:${event.messageTs}`;
}
export function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

export async function routeSlackThreadEvent(
  event: SlackThreadEvent,
  config: ThreadRouterConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<ThreadRouteResult> {
  const scope = digest(
    config.multicaWorkspaceId +
      ":" +
      config.multicaProjectId +
      ":" +
      threadScopeId(config),
  );
  const key = `relay:${scope}:thread:${digest(threadKey(event))}`;
  const msgKey = `relay:${scope}:message:${digest(messageKey(event))}`;
  const lockKey = key + ":lock",
    owner = randomUUID();
  if (!(await config.store.setIfAbsent(lockKey, owner, 120)))
    throw new Error("thread_lock_busy");
  try {
    const raw = await config.store.get(key);
    let state: ThreadState;
    if (raw) {
      state = JSON.parse(raw) as ThreadState;
      if (
        state.version !== 2 ||
        typeof state.rootMessageKey !== "string" ||
        typeof state.creating !== "boolean"
      )
        throw new Error("invalid_thread_state");
    } else
      state = {
        version: 2,
        rootMessageKey: messageKey(event),
        creating: false,
      };
    const marker = `<!-- relay-thread:${scope}:${digest(threadKey(event))} -->`;
    const save = () =>
      config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
    if (event.operation === "cancel")
      return await cancelThread(event, state, marker, config, save, fetchImpl);
    if (state.cancellation && state.cancellation.phase !== "done") {
      state.ignoredThrough = maxTimestamp(
        state.ignoredThrough,
        event.messageTs,
      );
      await save();
      return { action: "ignored", issueId: state.issueId };
    }
    if (
      state.ignoredThrough &&
      compareTimestamp(event.messageTs, state.ignoredThrough) <= 0
    )
      return { action: "ignored", issueId: state.issueId };
    if (state.issueId) await validateMappedIssue(config, state.issueId, marker, fetchImpl);
    state.lastMessageTs = maxTimestamp(state.lastMessageTs, event.messageTs);
    // 保存触发消息，再尝试外部写入，取消恢复才能找到响应丢失的消息。
    state.reactionMessages ??= raw
      ? [state.rootMessageKey.split(":").at(-1)!]
      : [];
    if (!state.reactionMessages.includes(event.messageTs))
      state.reactionMessages.push(event.messageTs);
    await save();
    const finish = (
      action: "created" | "comment_persisted" | "duplicate",
    ): ThreadRouteResult => ({ action, issueId: state.issueId });
    const prepare = async (descriptionMarker: string, followup: boolean): Promise<string> => {
      const preparedKey = msgKey + ":envelope";
      const previous = await config.store.get(preparedKey);
      const envelope = previous
        ? JSON.parse(previous) as { eventPayload: SlackThreadEvent; replyContext: SlackReplyContext }
        : { eventPayload: compactEvent(event), replyContext: await getSlackReplyContext(config, fetchImpl) };
      // Freeze the event, not its role as an Issue or comment: a rejected first
      // create can be overtaken by a follow-up before it is retried.
      const description = formatTaskDescription(envelope.eventPayload, descriptionMarker, followup, envelope.replyContext);
      if (!previous)
        await config.store.set(preparedKey, JSON.stringify(envelope), 24 * 60 * 60);
      return description;
    };
    if (!state.issueId) {
      // Recover by immutable description marker before any write. A POST whose
      // result is unknown must never be repeated blindly.
      const existing = await findIssue(config, marker, fetchImpl);
      if (existing) {
        state.issueId = existing.id;
        if (!raw) {
          const original = readTaskMessage(existing.description!);
          if (threadKey(original) !== threadKey(event))
            throw new Error("invalid_thread_state");
          state.rootMessageKey = `${original.teamId}:${original.channelId}:${original.messageTs}`;
          if (!state.reactionMessages!.includes(original.messageTs))
            state.reactionMessages!.push(original.messageTs);
        }
      } else {
        if (state.creating) throw new Error("ambiguous_issue_create");
        const description = await prepare(marker, false);
        state.rootMessageKey = messageKey(event);
        state.creating = true;
        await config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
        try {
          const created = await createIssue(
            config,
            formatTaskTitle(event, scope),
            description,
            fetchImpl,
          );
          state.issueId = created.id;
          state.reactionHistoryKnown = true;
        } catch (error) {
          // Definite request rejection permits a later retry; 5xx/transport or
          // malformed success may have committed, so retain the write intent.
          if (
            error instanceof ApiError &&
            error.status >= 400 &&
            error.status < 500
          ) {
            state.creating = false;
            await config.store.set(
              key,
              JSON.stringify(state),
              STATE_TTL_SECONDS,
            );
          }
          throw error;
        }
      }
      state.creating = false;
      await config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
    }
    const previous = await config.store.get(msgKey);
    if (previous && (JSON.parse(previous) as MessageState).phase === "done")
      return finish("duplicate");
    if (messageKey(event) === state.rootMessageKey) {
      await config.store.set(
        msgKey,
        JSON.stringify({ phase: "done" }),
        STATE_TTL_SECONDS,
      );
      return finish("created");
    }
    const messageMarker = `<!-- relay-message:${digest(messageKey(event))} -->`;
    const existingComment = await findComment(
      config,
      state.issueId,
      messageMarker,
      fetchImpl,
    );
    if (!existingComment) {
      if (
        previous &&
        (JSON.parse(previous) as MessageState).phase === "writing"
      )
        throw new Error("ambiguous_comment_create");
      const description = await prepare(messageMarker, true);
      await config.store.set(
        msgKey,
        JSON.stringify({ phase: "writing" }),
        STATE_TTL_SECONDS,
      );
      try {
        await createComment(
          config,
          state.issueId,
          description,
          fetchImpl,
        );
      } catch (error) {
        // Do not clear ambiguous writes. Explicit rejections are retried by
        // the queue after storing a non-writing phase.
        if (
          error instanceof ApiError &&
          error.status >= 400 &&
          error.status < 500
        )
          await config.store.set(
            msgKey,
            JSON.stringify({ phase: "rejected" }),
            STATE_TTL_SECONDS,
          );
        throw error;
      }
    }
    await config.store.set(
      msgKey,
      JSON.stringify({ phase: "done" }),
      STATE_TTL_SECONDS,
    );
    return finish("comment_persisted");
  } finally {
    await config.store.releaseIfOwner(lockKey, owner);
  }
}
