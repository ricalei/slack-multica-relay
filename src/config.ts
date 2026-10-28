import type { ApiConfig } from "./multica-api.js";
export interface RelayConfig extends ApiConfig {
  signingSecret: string;
  teamId: string;
  ownerSlackUserId: string;
  targetUserIds: Set<string>;
  targetSubteamIds: Set<string>;
  cancelKeywords: Set<string>;
  allowedChannelIds: Set<string>;
  allowAllChannels: boolean;
  blockedChannelIds: Set<string>;
  allowedSenderIds: Set<string>;
  allowAllSenders: boolean;
  blockedSenderIds: Set<string>;
  releaseApprovalChannelId?: string;
  releaseApprovalAppId?: string;
  releaseApprovalBotId?: string;
  slackReactionToken: string;
  kvRestApiUrl: string;
  kvRestApiToken: string;
  queueUrl: string;
  queueToken: string;
  queueCurrentSigningKey: string;
  queueNextSigningKey: string;
  consumerUrl: string;
}
export function loadRelayConfig(
  env: NodeJS.ProcessEnv = process.env,
): RelayConfig {
  const allowedChannels = policyIds(required(env, "SLACK_ALLOWED_CHANNEL_IDS"));
  const blockedChannelIds = ids(env.SLACK_BLOCKED_CHANNEL_IDS);
  const allowedSenders = policyIds(env.SLACK_ALLOWED_SENDER_IDS || "all");
  const blockedSenderIds = ids(env.SLACK_BLOCKED_SENDER_IDS);
  const targetUserIds = ids(env.SLACK_TARGET_USER_IDS);
  const targetSubteamIds = ids(env.SLACK_TARGET_SUBTEAM_IDS);
  const releaseApproval = releaseApprovalConfig(env);
  const ownerSlackUserId = required(env, "RELAY_OWNER_SLACK_USER_ID");
  if (!/^[UW][A-Z0-9]+$/u.test(ownerSlackUserId))
    throw new Error("invalid_identifier");
  if (!targetUserIds.size && !targetSubteamIds.size)
    throw new Error("missing_mention_target");
  const type = env.MULTICA_ASSIGNEE_TYPE?.trim() || "agent";
  if (type !== "agent" && type !== "squad") throw new Error("invalid_assignee_type");
  const targetId = env.MULTICA_ASSIGNEE_ID?.trim() || (type === "agent" ? env.MULTICA_AGENT_ID?.trim() : undefined);
  if (!targetId) throw new Error("relay_not_configured");
  const legacy = env.MULTICA_LEGACY_AGENT_ID?.trim() || undefined;
  const scope = env.MULTICA_THREAD_SCOPE_ID?.trim() || undefined;
  if (legacy && (type !== "squad" || scope !== legacy)) throw new Error("invalid_migration_scope");
  return {
    signingSecret: required(env, "SLACK_SIGNING_SECRET"),
    teamId: required(env, "SLACK_TEAM_ID"),
    ownerSlackUserId,
    allowedChannelIds: allowedChannels.ids,
    allowAllChannels: allowedChannels.all,
    blockedChannelIds,
    allowedSenderIds: allowedSenders.ids,
    allowAllSenders: allowedSenders.all,
    blockedSenderIds,
    ...releaseApproval,
    targetUserIds,
    targetSubteamIds,
    cancelKeywords: cancelKeywords(env.SLACK_CANCEL_KEYWORDS),
    multicaApiBaseUrl: https(required(env, "MULTICA_API_BASE_URL")),
    multicaApiToken: required(env, "MULTICA_API_TOKEN"),
    multicaWorkspaceId: required(env, "MULTICA_WORKSPACE_ID"),
    multicaProjectId: required(env, "MULTICA_PROJECT_ID"),
    multicaAssigneeType: type,
    multicaAssigneeId: targetId,
    multicaThreadScopeId: scope,
    multicaLegacyAgentId: legacy,
    slackReactionToken: required(env, "SLACK_USER_TOKEN"),
    kvRestApiUrl: https(required(env, "KV_REST_API_URL")),
    kvRestApiToken: required(env, "KV_REST_API_TOKEN"),
    queueUrl: https(env.QSTASH_URL?.trim() || "https://qstash.upstash.io"),
    queueToken: required(env, "QSTASH_TOKEN"),
    queueCurrentSigningKey: required(env, "QSTASH_CURRENT_SIGNING_KEY"),
    queueNextSigningKey: required(env, "QSTASH_NEXT_SIGNING_KEY"),
    consumerUrl: https(required(env, "RELAY_CONSUMER_URL")),
  };
}
function required(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key]?.trim();
  if (!value) throw new Error("relay_not_configured");
  return value;
}
function ids(value: string | undefined): Set<string> {
  const values = (value ?? "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
  if (values.some((x) => !/^[A-Z][A-Z0-9]+$/u.test(x)))
    throw new Error("invalid_identifier");
  return new Set(values);
}

function releaseApprovalConfig(env: NodeJS.ProcessEnv): {
  releaseApprovalChannelId?: string;
  releaseApprovalAppId?: string;
  releaseApprovalBotId?: string;
} {
  const values = [
    env.SLACK_RELEASE_APPROVAL_CHANNEL_ID?.trim(),
    env.SLACK_RELEASE_APPROVAL_APP_ID?.trim(),
    env.SLACK_RELEASE_APPROVAL_BOT_ID?.trim(),
  ];
  const configured = values.filter(Boolean);
  if (!configured.length) return {};
  if (
    configured.length !== values.length ||
    values.some((value) => !value || !/^[A-Z][A-Z0-9]+$/u.test(value))
  )
    throw new Error("invalid_release_approval_config");
  return {
    releaseApprovalChannelId: values[0],
    releaseApprovalAppId: values[1],
    releaseApprovalBotId: values[2],
  };
}
function policyIds(value: string): { ids: Set<string>; all: boolean } {
  const normalized = value.trim();
  if (normalized.toLowerCase() === "all") return { ids: new Set(), all: true };
  const parsed = ids(normalized);
  if (!parsed.size) throw new Error("invalid_allowlist");
  return { ids: parsed, all: false };
}
function https(value: string): string {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.hash ||
    url.search
  )
    throw new Error("invalid_service_url");
  return value.replace(/\/+$/u, "");
}

function cancelKeywords(value: string | undefined): Set<string> {
  const keywords = (value ?? "")
    .split(",")
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
  return new Set(keywords.length ? keywords : ["cancel", "取消"]);
}
