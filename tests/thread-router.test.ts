import { afterEach, describe, expect, it, vi } from "vitest";
import {
  routeSlackThreadEvent,
  type SlackThreadEvent,
  type ThreadRouterConfig,
} from "../src/thread-router.js";
import { MemoryThreadStore } from "../src/thread-store.js";
const root: SlackThreadEvent = {
  teamId: "T1",
  channelId: "C1",
  messageTs: "100.000001",
  threadTs: "100.000001",
  senderUserId: "U2",
  text: "<@U1> test",
  mention: { type: "user", id: "U1" },
};
const releaseRoot: SlackThreadEvent = {
  taskType: "release_approval",
  teamId: "T1",
  channelId: "C08HV4GTJ2D",
  messageTs: "1789637508.526609",
  threadTs: "1789637508.526609",
  senderUserId: "U081137G64T",
  text: "RELEASE APPROVAL REQUIRED VERSION v1.0.117 STATUS WAITING FOR APPROVAL",
  operation: "dispatch",
  releaseApproval: {
    version: "v1.0.117",
    prUrl: "https://github.com/MoeGolibrary/moego-finance-kit/pull/354",
    sourceAppId: "A080YA0KTJ9",
    sourceBotId: "B081PVALKC0",
  },
};
function fixture() {
  const issues: {
    id: string;
    title: string;
    description: string;
    project_id: string;
    assignee_type: string;
    assignee_id: string;
  }[] = [];
  const comments: { id: string; content: string }[] = [];
  const slackReactionCalls: string[] = [];
  let issuePosts = 0,
    agentGets = 0,
    commentPosts = 0,
    failIssue = false,
    failComment = false;
  const agentConfig = { model: "gpt-6-astra", service_tier: "priority" };
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("/api/agents/")) {
      agentGets++;
      return Response.json({
        id: decodeURIComponent(url.split("/").at(-1)!),
        workspace_id: "ws",
        ...agentConfig,
      });
    }
    if (url.includes("/api/issues/search?")) return Response.json({ issues });
    if (url.endsWith("/api/issues")) {
      issuePosts++;
      const data = JSON.parse(String(init?.body));
      const row = {
        id: "issue-" + issuePosts,
        title: data.title,
        description: data.description,
        project_id: "project",
        assignee_type: data.assignee_type,
        assignee_id: data.assignee_id,
      };
      issues.push(row);
      if (failIssue) throw new DOMException("lost response", "TimeoutError");
      return Response.json(row, { status: 201 });
    }
    if (url.includes("/comments")) {
      if (init?.method === "POST") {
        commentPosts++;
        const row = {
          id: "comment-" + commentPosts,
          content: JSON.parse(String(init.body)).content,
        };
        comments.push(row);
        if (failComment)
          throw new DOMException("lost response", "TimeoutError");
        return Response.json(row, { status: 201 });
      }
      return Response.json(comments);
    }
    const mapped = issues.find((row) => url.endsWith("/api/issues/" + row.id));
    if (mapped) return Response.json(mapped);
    if (url === "https://slack.com/api/reactions.add") {
      slackReactionCalls.push(String(init?.body));
      return Response.json({ ok: true });
    }
    throw new Error("unexpected endpoint");
  };
  const config: ThreadRouterConfig = {
    multicaApiBaseUrl: "https://multica.test",
    multicaApiToken: "test",
    multicaWorkspaceId: "ws",
    multicaProjectId: "project",
    multicaAgentId: "agent",
    store: new MemoryThreadStore(),
  };
  return {
    config,
    fetcher,
    issues,
    comments,
    slackReactionCalls,
    agentConfig,
    get agentGets() {
      return agentGets;
    },
    get issuePosts() {
      return issuePosts;
    },
    get commentPosts() {
      return commentPosts;
    },
    loseIssueResponse() {
      failIssue = true;
    },
    loseCommentResponse() {
      failComment = true;
    },
  };
}
afterEach(() => vi.restoreAllMocks());
describe("direct Issue routing", () => {
  function payload(description: string) {
    return JSON.parse(description.match(/```json\n([\s\S]*?)\n```/)![1]!);
  }

  it.each([false, true])("reuses the event snapshot after a definite write rejection (followup=%s)", async (followup) => {
    const f = fixture();
    if (followup) await routeSlackThreadEvent(root, f.config, f.fetcher);
    const event = followup ? { ...root, messageTs: "102.000001", text: "<@U1> next" } : root;
    let reject = true;
    const fetcher: typeof fetch = async (input, init) => {
      if (reject && init?.method === "POST" && String(input).endsWith(followup ? "/comments" : "/api/issues")) {
        reject = false;
        return Response.json({}, { status: 429 });
      }
      return f.fetcher(input, init);
    };
    await expect(routeSlackThreadEvent(event, f.config, fetcher)).rejects.toThrow();
    f.agentConfig.model = "new-configuration";
    await routeSlackThreadEvent({ ...event, text: "changed retry input" }, f.config, fetcher);
    const saved = payload(followup ? f.comments[0]!.content : f.issues[0]!.description);
    expect(saved.eventPayload.text).toBe(event.text);
    expect(saved.replyContext.model).toBe("gpt-6-astra");
  });

  it("attaches a fresh configuration snapshot to each new message, not each duplicate", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    expect(payload(f.issues[0]!.description).replyContext).toMatchObject({
      type: "slack_reply_context",
      source: "agent_config",
      status: "available",
      agentId: "agent",
      model: "gpt-6-astra",
      serviceTier: "priority",
    });
    f.agentConfig.model = "gpt-5.6-sol";
    f.agentConfig.service_tier = "default";
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    const next = { ...root, messageTs: "102.000001" };
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(payload(f.comments[0]!.content)).toMatchObject({
      eventPayload: { messageTs: next.messageTs },
      replyContext: { model: "gpt-5.6-sol", serviceTier: "default" },
    });
    expect(payload(f.issues[0]!.description).replyContext.model).toBe("gpt-6-astra");
    expect(f.agentGets).toBe(2);
  });

  it("continues both initial and followup writes when Agent configuration times out", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    const fetcher: typeof fetch = async (input, init) => {
      if (String(input).includes("/api/agents/"))
        throw new DOMException("timeout", "TimeoutError");
      return f.fetcher(input, init);
    };
    expect((await routeSlackThreadEvent(root, f.config, fetcher)).action).toBe("created");
    const next = { ...root, messageTs: "102.000001" };
    expect((await routeSlackThreadEvent(next, f.config, fetcher)).action).toBe("comment_persisted");
    for (const text of [f.issues[0]!.description, f.comments[0]!.content]) {
      expect(payload(text).replyContext).toMatchObject({
        status: "unavailable", model: null, serviceTier: null,
      });
      expect(payload(text).eventPayload.text).toBe(root.text);
    }
  });
  it("creates distinct issues for two simultaneous different Slack threads", async () => {
    const f = fixture();
    await Promise.all([
      routeSlackThreadEvent(root, f.config, f.fetcher),
      routeSlackThreadEvent(
        { ...root, messageTs: "101.000001", threadTs: "101.000001" },
        f.config,
        f.fetcher,
      ),
    ]);
    expect(f.issuePosts).toBe(2);
    expect(f.issues[0]!.title).not.toBe(f.issues[1]!.title);
  });
  it("routes separate notifications for the same version PR to one Issue", async () => {
    const f = fixture();
    await routeSlackThreadEvent(releaseRoot, f.config, f.fetcher);
    const repeated = {
      ...releaseRoot,
      messageTs: "1789637600.000001",
      threadTs: "1789637600.000001",
    };
    expect(
      (await routeSlackThreadEvent(repeated, f.config, f.fetcher)).action,
    ).toBe("comment_persisted");
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("recovers the same version PR Issue after its KV mapping expires", async () => {
    const f = fixture();
    await routeSlackThreadEvent(releaseRoot, f.config, f.fetcher);
    f.config.store = new MemoryThreadStore();
    const repeated = {
      ...releaseRoot,
      messageTs: "1789637600.000001",
      threadTs: "1789637600.000001",
    };
    expect(
      (await routeSlackThreadEvent(repeated, f.config, f.fetcher)).action,
    ).toBe("comment_persisted");
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("routes different version PRs to distinct Issues", async () => {
    const f = fixture();
    await routeSlackThreadEvent(releaseRoot, f.config, f.fetcher);
    await routeSlackThreadEvent(
      {
        ...releaseRoot,
        messageTs: "1789637600.000001",
        threadTs: "1789637600.000001",
        releaseApproval: {
          ...releaseRoot.releaseApproval,
          version: "v1.0.118",
          prUrl: "https://github.com/MoeGolibrary/moego-finance-kit/pull/355",
        },
      },
      f.config,
      f.fetcher,
    );
    expect(f.issuePosts).toBe(2);
    expect(f.issues[0]!.title).not.toBe(f.issues[1]!.title);
  });
  it("one thread creates once and appends once per followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).action,
    ).toBe("duplicate");
    const next = { ...root, messageTs: "102.000001" };
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.agentGets).toBe(2);
    expect(f.commentPosts).toBe(1);
  });
  it("never adds Slack reactions for created, duplicate, or followup delivery", async () => {
    const f = fixture();
    const legacyReactionConfig = Object.assign(f.config, {
      slackReactionToken: "xoxp-owner",
      slackReactionName: "eyes",
    });
    expect(
      (await routeSlackThreadEvent(root, legacyReactionConfig, f.fetcher))
        .action,
    ).toBe("created");
    expect(
      (await routeSlackThreadEvent(root, legacyReactionConfig, f.fetcher))
        .action,
    ).toBe("duplicate");
    expect(
      (
        await routeSlackThreadEvent(
          { ...root, messageTs: "102.000001" },
          legacyReactionConfig,
          f.fetcher,
        )
      ).action,
    ).toBe("comment_persisted");
    expect(f.slackReactionCalls).toEqual([]);
  });
  it("recovers a committed Issue after lost response without second POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).issueId,
    ).toBe("issue-1");
    expect(f.issuePosts).toBe(1);
    expect(f.agentGets).toBe(1);
  });
  it("does not blindly replay an ambiguous unconfirmed Issue POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    f.issues.splice(0);
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("ambiguous_issue_create");
    expect(f.issuePosts).toBe(1);
  });
  it("recovers a committed comment after lost response without duplicate followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.loseCommentResponse();
    const next = { ...root, messageTs: "102.000001" };
    await expect(
      routeSlackThreadEvent(next, f.config, f.fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.commentPosts).toBe(1);
    expect(f.agentGets).toBe(2);
  });
  it("recovers original root identity after KV mapping expires", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.config.store = new MemoryThreadStore();
    const result = await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001" },
      f.config,
      f.fetcher,
    );
    expect(result.action).toBe("comment_persisted");
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("does not POST while another worker owns the lock", async () => {
    const f = fixture();
    f.config.store.setIfAbsent = async () => false;
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("thread_lock_busy");
    expect(f.issuePosts).toBe(0);
    expect(f.agentGets).toBe(0);
  });
  it("recovers a legacy description after KV expiry without creating a second Issue", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    const row = f.issues[0]!;
    row.description =
      row.description.split("\n")[0] +
      "\n" +
      JSON.stringify({ eventPayload: root });
    f.config.store = new MemoryThreadStore();
    await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001" },
      f.config,
      f.fetcher,
    );
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it.each(["damaged", "mismatched"])(
    "does not recreate an Issue with %s recovery data",
    async (mode) => {
      const f = fixture();
      await routeSlackThreadEvent(root, f.config, f.fetcher);
      f.issues[0]!.description =
        mode === "damaged"
          ? f.issues[0]!.description.replace("<!-- /relay-payload -->", "")
          : f.issues[0]!.description.replace(
              '"channelId": "C1"',
              '"channelId": "C2"',
            );
      f.config.store = new MemoryThreadStore();
      await expect(
        routeSlackThreadEvent(root, f.config, f.fetcher),
      ).rejects.toThrow("invalid_thread_state");
      expect(f.issuePosts).toBe(1);
      expect(f.commentPosts).toBe(0);
    },
  );
  it("retains both requests when a rejected first create is overtaken by a followup", async () => {
    const f = fixture();
    let reject = true;
    const fetcher: typeof fetch = async (input, init) => {
      if (reject && String(input).endsWith("/api/issues")) {
        reject = false;
        return Response.json({ error: "rate_limited" }, { status: 429 });
      }
      return f.fetcher(input, init);
    };
    await expect(
      routeSlackThreadEvent(root, f.config, fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001", text: "<@U1> B" },
      f.config,
      fetcher,
    );
    await routeSlackThreadEvent(root, f.config, fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
    expect(payload(f.issues[0]!.description).eventPayload.text).toBe("<@U1> B");
    expect(payload(f.comments[0]!.content).eventPayload.text).toBe("<@U1> test");
  });
  it("does not adopt a different configured Agent scope", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    await routeSlackThreadEvent(
      root,
      { ...f.config, multicaAgentId: "another-agent" },
      f.fetcher,
    );
    expect(f.issuePosts).toBe(2);
  });
  it("does not recover an Issue assigned to another Agent", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.issues[0]!.assignee_id = "another-agent";
    f.config.store = new MemoryThreadStore();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("invalid_issue_scope");
    expect(f.issuePosts).toBe(1);
  });
});

describe("Team migration", () => {
  const team = { multicaAssigneeType: "squad" as const, multicaAssigneeId: "team", multicaThreadScopeId: "agent" };
  it("keeps root and followup deduplication after reassignment", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.issues[0]!.assignee_type = "squad";
    f.issues[0]!.assignee_id = "team";
    const config = { ...f.config, ...team };
    expect((await routeSlackThreadEvent(root, config, f.fetcher)).action).toBe("duplicate");
    const next = { ...root, messageTs: "102.000001" };
    await routeSlackThreadEvent(next, config, f.fetcher);
    await routeSlackThreadEvent(next, config, f.fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
    config.store = new MemoryThreadStore();
    await routeSlackThreadEvent(next, config, f.fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("accepts only explicitly configured legacy ownership during migration", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    const config = { ...f.config, ...team };
    await expect(routeSlackThreadEvent(root, config, f.fetcher)).rejects.toThrow("invalid_issue_scope");
    await routeSlackThreadEvent(root, { ...config, multicaLegacyAgentId: "agent" }, f.fetcher);
    expect(f.issuePosts).toBe(1);
  });
  it("preserves ambiguous create intent across target changes", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(routeSlackThreadEvent(root, f.config, f.fetcher)).rejects.toThrow();
    f.issues.splice(0);
    await expect(routeSlackThreadEvent(root, { ...f.config, ...team }, f.fetcher)).rejects.toThrow("ambiguous_issue_create");
    expect(f.issuePosts).toBe(1);
  });
  it("creates new tasks for the Team and isolates fresh Team scopes", async () => {
    const f = fixture();
    const config = { ...f.config, multicaAssigneeType: "squad" as const, multicaAssigneeId: "team" };
    await routeSlackThreadEvent(root, config, f.fetcher);
    expect(f.issues[0]).toMatchObject({ assignee_type: "squad", assignee_id: "team" });
    await routeSlackThreadEvent(root, { ...config, multicaAssigneeId: "another" }, f.fetcher);
    expect(f.issuePosts).toBe(2);
  });
});
