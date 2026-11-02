import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  routeSlackThreadEvent,
  digest,
  threadKey,
  type SlackThreadEvent,
  type ThreadRouterConfig,
} from "../src/thread-router.js";
import { MemoryThreadStore } from "../src/thread-store.js";
import type { IssueRun } from "../src/multica-api.js";

const root: SlackThreadEvent = {
  teamId: "T1",
  channelId: "C1",
  senderUserId: "U1",
  messageTs: "100.000001",
  threadTs: "100.000001",
  text: "<@U1> 工作",
  mention: { type: "user", id: "U1" },
  operation: "dispatch",
};
const cancel = {
  ...root,
  messageTs: "102.000001",
  text: "<@U1> 取消",
  operation: "cancel" as const,
};
const ownerSlackUserId = "U05GLCUAUTU";
function fixture() {
  const store = new MemoryThreadStore();
  const config: ThreadRouterConfig = {
    multicaApiBaseUrl: "https://multica.test",
    multicaApiToken: "test",
    multicaWorkspaceId: "ws",
    multicaProjectId: "project",
    multicaAgentId: "agent",
    slackReactionToken: "test",
    store,
  };
  const issues: Record<string, unknown>[] = [];
  const comments: Record<string, unknown>[] = [];
  const runs: IssueRun[] = [];
  const calls: string[] = [];
  const reactions = new Map<string, { name: string; users: string[] }[]>();
  let lostCreate = false,
    lostCancel = false,
    refuseCancel = false,
    delayedCancel = false,
    naturalCompletion = false,
    failCleanup = false;
  const newRun = (id: string, status = "running") => {
    const run = {
      id,
      status,
      issue_id: "issue",
      workspace_id: "ws",
      agent_id: "agent",
    };
    runs.push(run);
    return run;
  };
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url.includes("/api/agents/"))
      return Response.json({ id: "agent", workspace_id: "ws" });
    if (url.includes("/api/issues/search?")) return Response.json({ issues });
    if (url.endsWith("/api/issues")) {
      issues.push({ ...body, id: "issue" });
      newRun("run-1");
      if (lostCreate) throw new Error("lost create");
      return Response.json(issues[0]);
    }
    if (url.endsWith("/api/issues/issue")) return Response.json(issues[0]);
    if (url.endsWith("/task-runs")) return Response.json(runs);
    if (url.endsWith("/cancel")) {
      if (refuseCancel) return Response.json({}, { status: 403 });
      const id = url.split("/").at(-2);
      if (!delayedCancel)
        runs.find((run) => run.id === id)!.status = naturalCompletion
          ? "completed"
          : "cancelled";
      if (lostCancel) throw new Error("lost cancel");
      return Response.json({});
    }
    if (url.includes("/comments")) {
      if (init?.method === "POST") {
        const row = { ...body, id: `comment-${comments.length}` };
        comments.push(row);
        newRun(`run-${runs.length + 1}`);
        return Response.json(row);
      }
      return Response.json(comments);
    }
    if (url.endsWith("/reactions.add")) {
      const rows = reactions.get(body.timestamp) ?? [];
      if (
        !rows.some(
          (row) =>
            row.name === body.name && row.users.includes(ownerSlackUserId),
        )
      )
        rows.push({ name: body.name, users: [ownerSlackUserId] });
      reactions.set(body.timestamp, rows);
      return Response.json({ ok: true });
    }
    if (url.endsWith("/auth.test"))
      return Response.json({ ok: true, user_id: ownerSlackUserId });
    if (new URL(url).pathname.endsWith("/reactions.get"))
      return Response.json({
        ok: true,
        message: { reactions: reactions.get(new URL(url).searchParams.get("timestamp")!) ?? [] },
      });
    if (url.endsWith("/reactions.remove")) {
      if (failCleanup)
        return Response.json({ ok: false, error: "missing_scope" });
      for (const row of reactions.get(body.timestamp) ?? [])
        if (row.name === body.name)
          row.users = row.users.filter((user) => user !== ownerSlackUserId);
      return Response.json({ ok: true });
    }
    throw new Error(`unexpected endpoint ${url}`);
  };
  const seedAgentReaction = (messageTs: string) => {
    const rows = reactions.get(messageTs) ?? [];
    if (
      !rows.some(
        (row) => row.name === "eyes" && row.users.includes(ownerSlackUserId),
      )
    )
      rows.push({ name: "eyes", users: [ownerSlackUserId] });
    reactions.set(messageTs, rows);
  };
  const route = async (event = root) => {
    const result = await routeSlackThreadEvent(event, config, fetcher);
    if (
      event.operation !== "cancel" &&
      (result.action === "created" || result.action === "comment_persisted")
    )
      seedAgentReaction(event.messageTs);
    return result;
  };
  return {
    config,
    route,
    runs,
    reactions,
    issues,
    calls,
    comments,
    newRun,
    loseCreate: () => {
      lostCreate = true;
    },
    loseCancel: () => {
      lostCancel = true;
    },
    refuseCancel: () => {
      refuseCancel = true;
    },
    delayCancel: () => {
      delayedCancel = true;
    },
    finishNaturally: () => {
      naturalCompletion = true;
    },
    failCleanup: (fail: boolean) => {
      failCleanup = fail;
    },
    cancelPosts: () =>
      calls.filter(
        (call) => call.startsWith("POST") && call.endsWith("/cancel"),
      ),
  };
}
beforeEach(() => vi.spyOn(Date, "now").mockReturnValue(103000));
afterEach(() => vi.restoreAllMocks());

describe("取消和恢复", () => {
  it.each([
    "queued",
    "dispatched",
    "running",
    "waiting_local_directory",
    "deferred",
  ])("取消 %s 并清理所有本身份表情", async (status) => {
    const f = fixture();
    await f.route();
    f.runs[0]!.status = status;
    f.reactions
      .get(root.messageTs)!
      .push(
        { name: "thinking_face", users: [ownerSlackUserId, "U2"] },
        { name: "heart", users: ["U2"] },
      );
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.runs[0]!.status).toBe("cancelled");
    expect(f.reactions.get(root.messageTs)!.flatMap((r) => r.users)).toEqual([
      "U2",
      "U2",
    ]);
    expect(f.comments).toHaveLength(0);
    expect(f.reactions.has(cancel.messageTs)).toBe(false);
    const lastRead = f.calls.lastIndexOf(
      "GET https://multica.test/api/issues/issue/task-runs",
    );
    expect(
      f.calls.findIndex((call) => call.endsWith("/reactions.remove")),
    ).toBeGreaterThan(lastRead);
  });
  it("取消多个运行且同时清理多个触发消息", async () => {
    const f = fixture();
    await f.route();
    await f.route({ ...root, messageTs: "101.000001" });
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.cancelPosts()).toHaveLength(2);
    expect(
      [...f.reactions.values()]
        .flat()
        .every((row) => !row.users.includes(ownerSlackUserId)),
    ).toBe(true);
  });
  it.each(["completed", "failed", "cancelled"])(
    "无活动运行且已 %s 时保留已有表情",
    async (status) => {
      const f = fixture();
      await f.route();
      f.runs[0]!.status = status;
      expect((await f.route(cancel)).action).toBe("no_active_run");
      expect(f.cancelPosts()).toHaveLength(0);
      expect(f.reactions.get(root.messageTs)![0]!.users).toContain(
        ownerSlackUserId,
      );
    },
  );
  it("取消时自然完成不伪装取消成功，也不清理完成标记", async () => {
    const f = fixture();
    await f.route();
    f.finishNaturally();
    expect((await f.route(cancel)).action).toBe("no_active_run");
    expect(f.reactions.get(root.messageTs)![0]!.users).toContain(
      ownerSlackUserId,
    );
  });
  it("取消响应丢失后先回读，不重复提交取消", async () => {
    const f = fixture();
    await f.route();
    f.loseCancel();
    await expect(f.route(cancel)).rejects.toThrow("lost cancel");
    expect(f.reactions.get(root.messageTs)![0]!.users).toContain(
      ownerSlackUserId,
    );
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.cancelPosts()).toHaveLength(1);
  });
  it("取消返回成功但仍活动时保留标记并等待队列重试", async () => {
    const f = fixture();
    await f.route();
    f.delayCancel();
    await expect(f.route(cancel)).rejects.toThrow("cancellation_pending");
    expect(f.reactions.get(root.messageTs)![0]!.users).toContain(
      ownerSlackUserId,
    );
    f.runs[0]!.status = "cancelled";
    expect((await f.route(cancel)).action).toBe("cancelled");
  });
  it("明确拒绝取消时不清理", async () => {
    const f = fixture();
    await f.route();
    f.refuseCancel();
    await expect(f.route(cancel)).rejects.toThrow("multica_http_error");
    expect(f.calls.some((call) => call.endsWith("/reactions.remove"))).toBe(
      false,
    );
  });
  it("清理失败重试只补偿清理，不重复取消", async () => {
    const f = fixture();
    await f.route();
    f.failCleanup(true);
    await expect(f.route(cancel)).rejects.toThrow("reaction_cleanup_failed");
    expect(f.runs[0]!.status).toBe("cancelled");
    f.failCleanup(false);
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.cancelPosts()).toHaveLength(1);
  });
  it("重复取消不会重新清理，旧启动重放也不会重新加表情", async () => {
    const f = fixture();
    await f.route();
    await f.route(cancel);
    const count = f.calls.length;
    await f.route(cancel);
    expect(f.calls).toHaveLength(count);
    expect((await f.route()).action).toBe("ignored");
    expect(f.calls).toHaveLength(count);
  });
  it("无关联任务不建卡，并阻止被取消的旧启动事件晚到", async () => {
    const f = fixture();
    expect((await f.route(cancel)).action).toBe("ignored");
    expect((await f.route()).action).toBe("ignored");
    expect(f.issues).toHaveLength(0);
  });
  it("建卡响应丢失仍能恢复并取消，没有第二次建卡", async () => {
    const f = fixture();
    f.loseCreate();
    await expect(f.route()).rejects.toThrow("lost create");
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.issues).toHaveLength(1);
  });
  it("无法确认建卡时保留取消意图，不创建另一张卡", async () => {
    const f = fixture();
    f.loseCreate();
    await expect(f.route()).rejects.toThrow();
    const row = f.issues.pop()!;
    await expect(f.route(cancel)).rejects.toThrow("cancellation_pending");
    expect((await f.route({ ...root, messageTs: "102.500000" })).action).toBe(
      "ignored",
    );
    f.issues.push(row);
    expect((await f.route(cancel)).action).toBe("cancelled");
  });
  it("只有 issue 没有 run 时等待后续读取", async () => {
    const f = fixture();
    await f.route();
    f.runs.splice(0);
    await expect(f.route(cancel)).rejects.toThrow("cancellation_pending");
    f.newRun("run-later", "queued");
    expect((await f.route(cancel)).action).toBe("cancelled");
  });
  it("取消期间不接受后续任务，结束后明确的新消息可继续原卡", async () => {
    const f = fixture();
    await f.route();
    f.failCleanup(true);
    await expect(f.route(cancel)).rejects.toThrow();
    const blocked = { ...root, messageTs: "102.500000" };
    expect((await f.route(blocked)).action).toBe("ignored");
    f.failCleanup(false);
    await f.route(cancel);
    expect((await f.route(blocked)).action).toBe("ignored");
    expect((await f.route({ ...root, messageTs: "104.000001" })).action).toBe(
      "comment_persisted",
    );
    const count = f.cancelPosts().length;
    await f.route(cancel);
    expect(f.cancelPosts()).toHaveLength(count);
    expect(f.runs.at(-1)!.status).toBe("running");
    expect(f.issues).toHaveLength(1);
  });
  it("晚到的旧取消不得停止已经处理的较新请求", async () => {
    const f = fixture();
    await f.route();
    await f.route({ ...root, messageTs: "104.000001" });
    expect((await f.route(cancel)).action).toBe("ignored");
    expect(f.cancelPosts()).toHaveLength(0);
  });
  it("重试只绑定原运行，外部新增运行会阻止清理", async () => {
    const f = fixture();
    await f.route();
    f.loseCancel();
    await expect(f.route(cancel)).rejects.toThrow();
    f.newRun("external");
    await expect(f.route(cancel)).rejects.toThrow("cancellation_new_run");
    expect(f.cancelPosts()).toHaveLength(1);
    expect(f.runs.at(-1)!.status).toBe("running");
  });
  it("映射恢复核对原消息来源并清理原触发消息", async () => {
    const f = fixture();
    await f.route();
    await f.route({ ...root, messageTs: "101.000001" });
    f.config.store = new MemoryThreadStore();
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.reactions.get(root.messageTs)![0]!.users).not.toContain(
      ownerSlackUserId,
    );
    expect(f.reactions.get("101.000001")![0]!.users).not.toContain(
      ownerSlackUserId,
    );
  });
  it("issue 指派改变后不取消其他 Agent 的任务", async () => {
    const f = fixture();
    await f.route();
    f.issues[0]!.assignee_id = "another";
    await expect(f.route(cancel)).rejects.toThrow("invalid_issue_scope");
    expect(f.cancelPosts()).toHaveLength(0);
  });
  it("并发取消不越过同一个线程锁", async () => {
    const f = fixture();
    await f.route();
    const key = `relay:${digest("ws:project:agent")}:thread:${digest(threadKey(root))}:lock`;
    await f.config.store.setIfAbsent(key, "another-worker", 120);
    await expect(f.route(cancel)).rejects.toThrow("thread_lock_busy");
    expect(f.cancelPosts()).toHaveLength(0);
  });
  it("清理补偿遇到外部新运行时暂停，不清除新运行期间的标记", async () => {
    const f = fixture();
    await f.route();
    f.failCleanup(true);
    await expect(f.route(cancel)).rejects.toThrow("reaction_cleanup_failed");
    f.newRun("external");
    f.failCleanup(false);
    await expect(f.route(cancel)).rejects.toThrow("cancellation_new_run");
    expect(f.cancelPosts()).toHaveLength(1);
    expect(f.reactions.get(root.messageTs)![0]!.users).toContain(
      ownerSlackUserId,
    );
  });
  it("未知运行状态保留错误，不能推断为已完成", async () => {
    const f = fixture();
    await f.route();
    f.runs[0]!.status = "unknown-status";
    await expect(f.route(cancel)).rejects.toThrow("invalid_multica_response");
    expect(f.cancelPosts()).toHaveLength(0);
  });
  it("快照中的运行消失后不清理或取消其他运行", async () => {
    const f = fixture();
    await f.route();
    f.loseCancel();
    await expect(f.route(cancel)).rejects.toThrow();
    f.runs.splice(0);
    await expect(f.route(cancel)).rejects.toThrow("cancellation_run_missing");
    expect(f.reactions.get(root.messageTs)![0]!.users).toContain(
      ownerSlackUserId,
    );
  });
});


describe("Agent 与 Team 归属的取消兼容", () => {
  it("使用新 Agent 配置取消，不依赖旧 multicaAgentId", async () => {
    const f = fixture();
    f.config.multicaAssigneeType = "agent";
    f.config.multicaAssigneeId = "new-agent";
    delete f.config.multicaAgentId;
    await f.route();
    f.runs[0]!.agent_id = "new-agent";
    expect(f.issues[0]!.assignee_id).toBe("new-agent");
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.cancelPosts()).toHaveLength(1);
  });

  it("Agent 模式仍拒绝同一卡片中其他执行者的活动运行", async () => {
    const f = fixture();
    await f.route();
    f.runs[0]!.agent_id = "another-agent";
    await expect(f.route(cancel)).rejects.toThrow("invalid_issue_scope");
    expect(f.cancelPosts()).toHaveLength(0);
  });

  it("Team 取消绑定同一 Issue 的多个 Agent 运行，并保留后续新增运行", async () => {
    const f = fixture();
    f.config.multicaAssigneeType = "squad";
    f.config.multicaAssigneeId = "team";
    delete f.config.multicaAgentId;
    await f.route();
    f.runs[0]!.agent_id = "leader";
    f.newRun("member-run").agent_id = "member";
    f.loseCancel();
    await expect(f.route(cancel)).rejects.toThrow("lost cancel");
    f.newRun("later-run").agent_id = "later-member";
    await expect(f.route(cancel)).rejects.toThrow("lost cancel");
    await expect(f.route(cancel)).rejects.toThrow("cancellation_new_run");
    expect(f.cancelPosts()).toHaveLength(2);
    expect(f.runs.map((run) => run.status)).toEqual(["cancelled", "cancelled", "running"]);
    expect(f.reactions.get(root.messageTs)![0]!.users).toContain(ownerSlackUserId);
  });

  it("Team 正常取消后清理标记，重复取消不重复发送", async () => {
    const f = fixture();
    f.config.multicaAssigneeType = "squad";
    f.config.multicaAssigneeId = "team";
    delete f.config.multicaAgentId;
    await f.route();
    f.runs[0]!.agent_id = "leader";
    f.newRun("member-run").agent_id = "member";
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.cancelPosts()).toHaveLength(2);
    expect(f.reactions.get(root.messageTs)![0]!.users).not.toContain(ownerSlackUserId);
    const count = f.calls.length;
    expect((await f.route(cancel)).action).toBe("cancelled");
    expect(f.calls).toHaveLength(count);
  });

  it.each([
    { issue_id: "another-issue" },
    { workspace_id: "another-workspace" },
  ])("Team 也拒绝越界运行：%j", async (change) => {
    const f = fixture();
    f.config.multicaAssigneeType = "squad";
    f.config.multicaAssigneeId = "team";
    await f.route();
    Object.assign(f.runs[0]!, change);
    await expect(f.route(cancel)).rejects.toThrow("invalid_multica_response");
    expect(f.cancelPosts()).toHaveLength(0);
  });

  it.each([true, false])("迁移期旧 Agent 的取消仅在显式兼容时放行：%s", async (allowLegacy) => {
    const f = fixture();
    await f.route();
    f.config.multicaAssigneeType = "squad";
    f.config.multicaAssigneeId = "team";
    f.config.multicaThreadScopeId = "agent";
    if (allowLegacy) f.config.multicaLegacyAgentId = "agent";
    if (allowLegacy) {
      expect((await f.route(cancel)).action).toBe("cancelled");
      expect(f.cancelPosts()).toHaveLength(1);
    } else {
      await expect(f.route(cancel)).rejects.toThrow("invalid_issue_scope");
      expect(f.cancelPosts()).toHaveLength(0);
    }
  });
});
