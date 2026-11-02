import { describe, expect, it } from 'vitest';
import { clearOwnSlackReactions } from '../src/reaction.js';

describe('取消后的 reaction 清理', () => {
  it('所有清理请求使用同一个 user token，取消时保留其他身份表情', async () => {
    const token = 'user';
    const reactions = new Map<string, Set<string>>([
      ['eyes', new Set(['OTHER', 'CURRENT'])], ['heart', new Set(['OTHER'])],
    ]);
    const fetcher: typeof fetch = async (input, init) => {
      const url = new URL(String(input));
      const authorization = new Headers(init?.headers).get('authorization');
      expect(authorization).toBe(`Bearer ${token}`);
      if (url.pathname.endsWith('reactions.get')) {
        expect(init?.method).toBe('GET');
        expect(init?.body).toBeUndefined();
        expect(Object.fromEntries(url.searchParams)).toEqual({ channel: 'C1', timestamp: '1.000001', full: 'true' });
        return Response.json({ ok: true, message: { reactions: [...reactions].map(([name, users]) => ({ name, users: [...users] })) } });
      }
      if (url.pathname.endsWith('auth.test')) return Response.json({ ok: true, user_id: 'CURRENT' });
      const name = JSON.parse(String(init?.body)).name;
      expect(url.pathname).toBe('/api/reactions.remove');
      return Response.json(reactions.get(name)!.delete('CURRENT') ? { ok: true } : { ok: false, error: 'no_reaction' });
    };
    expect([...reactions.get('eyes')!]).toEqual(['OTHER', 'CURRENT']);
    await clearOwnSlackReactions(token, 'C1', '1.000001', fetcher);
    expect([...reactions.get('eyes')!]).toEqual(['OTHER']);
    expect([...reactions.get('heart')!]).toEqual(['OTHER']);
  });
  it('仅移除 token 身份自己的表情', async () => {
    const removed: string[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      if (String(input).endsWith('auth.test')) return Response.json({ ok: true, user_id: 'U1' });
      if (new URL(String(input)).pathname.endsWith('reactions.get')) return Response.json({ ok: true, message: { reactions: [
        { name: 'eyes', users: ['U1', 'U2'] }, { name: 'heart', users: ['U2'] },
      ] } });
      removed.push(JSON.parse(String(init?.body)).name);
      return Response.json({ ok: true });
    };
    await clearOwnSlackReactions('test', 'C1', '1.000001', fetcher);
    expect(removed).toEqual(['eyes']);
  });
  it('已删除消息不阻断取消收尾', async () => {
    const fetcher: typeof fetch = async (input) => Response.json(String(input).endsWith('auth.test')
      ? { ok: true, user_id: 'U1' } : { ok: false, error: 'message_not_found' });
    await expect(clearOwnSlackReactions('test', 'C1', '1.000001', fetcher)).resolves.toBeUndefined();
  });
  it('权限不足保留失败，不把未知数据当作已清理', async () => {
    const fetcher: typeof fetch = async (input) => Response.json(String(input).endsWith('auth.test')
      ? { ok: true, user_id: 'U1' } : { ok: false, error: 'missing_scope' });
    await expect(clearOwnSlackReactions('test', 'C1', '1.000001', fetcher)).rejects.toThrow('reaction_cleanup_failed');
  });
});
