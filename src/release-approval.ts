import type { SlackMessageEvent } from "./mentions.js";

export interface ReleaseApprovalConfig {
  releaseApprovalChannelId?: string;
  releaseApprovalAppId?: string;
  releaseApprovalBotId?: string;
}

export interface ReleaseApprovalMatch {
  version: string;
  prUrl: string;
  sourceAppId: string;
  sourceBotId: string;
}

const VERSION = /\bVERSION[ \t]+(v\d+\.\d+\.\d+)\b/gu;
const VERSION_PR =
  /^[ \t]*(?:[•*-][ \t]*)?\*?版本[ \t]*PR[：:]\*?[ \t]*<(?<url>https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9]\d*)\|(?<label>[^>\r\n]+)>[ \t]*$/gmu;

export function findReleaseApproval(
  event: SlackMessageEvent,
  config: ReleaseApprovalConfig,
): ReleaseApprovalMatch | undefined {
  const channelId = config.releaseApprovalChannelId;
  const appId = config.releaseApprovalAppId;
  const botId = config.releaseApprovalBotId;
  if (!channelId || !appId || !botId) return undefined;
  if (
    event.type !== "message" ||
    event.channel !== channelId ||
    event.app_id !== appId ||
    event.bot_id !== botId ||
    typeof event.ts !== "string" ||
    typeof event.user !== "string" ||
    typeof event.text !== "string" ||
    (event.thread_ts !== undefined && event.thread_ts !== null) ||
    (event.subtype !== undefined && event.subtype !== null)
  )
    return undefined;

  const text = event.text;
  if (
    !/\bRELEASE APPROVAL REQUIRED\b/iu.test(text) ||
    !/\bSTATUS[ \t]+WAITING FOR APPROVAL\b/iu.test(text)
  )
    return undefined;

  const versions = [...text.matchAll(VERSION)].map((match) => match[1]!);
  if (versions.length !== 1) return undefined;
  const targets = [...text.matchAll(VERSION_PR)];
  if (targets.length !== 1) return undefined;
  const target = targets[0]!;
  const labelVersions = target.groups!.label!.match(/\bv\d+\.\d+\.\d+\b/gu);
  if (labelVersions?.length !== 1 || labelVersions[0] !== versions[0])
    return undefined;

  return {
    version: versions[0]!,
    prUrl: target.groups!.url!,
    sourceAppId: appId,
    sourceBotId: botId,
  };
}
