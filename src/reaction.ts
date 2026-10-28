export async function clearOwnSlackReactions(
  token: string, channelId: string, messageTs: string, fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const call = async (method: string, body: Record<string, string>) => {
    const reading = method === 'reactions.get';
    const url = new URL(`https://slack.com/api/${method}`);
    if (reading) url.search = new URLSearchParams(body).toString();
    const response = await fetchImpl(url.toString(), {
      method: reading ? 'GET' : 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json; charset=utf-8' },
      body: reading ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) throw new Error('reaction_cleanup_failed');
    const result = await response.json() as Record<string, unknown>;
    if (method === 'reactions.get' && result.error === 'message_not_found') return { message: {} };
    if (result.ok !== true && !(method === 'reactions.remove' && result.error === 'no_reaction'))
      throw new Error('reaction_cleanup_failed');
    return result;
  };
  const identity = await call('auth.test', {});
  if (typeof identity.user_id !== 'string') throw new Error('reaction_cleanup_failed');
  const result = await call('reactions.get', { channel: channelId, timestamp: messageTs, full: 'true' });
  const message = result.message as { reactions?: { name: string; users: string[] }[] } | undefined;
  if (!message || (message.reactions !== undefined && !Array.isArray(message.reactions)))
    throw new Error('reaction_cleanup_failed');
  for (const reaction of message.reactions ?? []) {
    if (typeof reaction.name !== 'string' || !Array.isArray(reaction.users)) throw new Error('reaction_cleanup_failed');
    if (reaction.users.includes(identity.user_id))
      await call('reactions.remove', { channel: channelId, timestamp: messageTs, name: reaction.name });
  }
}
