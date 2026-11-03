import fs from 'node:fs/promises';

export function topDocumentUrl(current, params, context) {
  return params?.context === context && typeof params.url === 'string' ? params.url : current;
}

export function createOwnedLifecycle({call, sid, context, file}) {
  const owned = new Set(), blocked = new Set(), subscriptions = new Set();
  let closing = false, cleanupPromise;
  const persist = async () => {
    const temporary = `${file}.${process.pid}.tmp`;
    await fs.writeFile(temporary, JSON.stringify({sid, context, intercepts: [...owned], blocked_requests: [...blocked]}), 'utf8');
    await fs.rename(temporary, file);
  };
  const remove = async id => {
    try { await call('network.removeIntercept', {intercept: id}); owned.delete(id); }
    catch (error) { if (/no such intercept/i.test(String(error))) owned.delete(id); }
  };
  const release = async request => {
    try { await call('network.continueRequest', {request}); blocked.delete(request); }
    catch (error) { if (/no such request/i.test(String(error))) blocked.delete(request); }
  };
  return {
    get closing() { return closing; },
    owns(params) { return (params?.intercepts || []).some(id => owned.has(id)); },
    track(id) { if (id) blocked.add(id); },
    released(id) { blocked.delete(id); },
    async recover() {
      let prior;
      try { prior = JSON.parse(await fs.readFile(file, 'utf8')); } catch { return; }
      if (prior.sid !== sid || prior.context !== context || !Array.isArray(prior.intercepts)) return;
      for (const id of prior.intercepts) if (typeof id === 'string') owned.add(id);
      for (const id of Array.isArray(prior.blocked_requests) ? prior.blocked_requests : []) if (typeof id === 'string') blocked.add(id);
      await Promise.all([...owned].map(remove));
      await Promise.all([...blocked].map(release));
      await persist();
      if (owned.size || blocked.size) throw Object.assign(new Error('owned BiDi intercept cleanup incomplete'),
        {code:'BIDI_CLEANUP_INCOMPLETE',remaining_intercepts:owned.size,remaining_requests:blocked.size});
    },
    async added(result) {
      if (typeof result?.intercept !== 'string') throw new Error('missing BiDi intercept id');
      owned.add(result.intercept); await persist();
    },
    subscribed(result) { if (typeof result?.subscription === 'string') subscriptions.add(result.subscription); },
    cleanup() {
      if (cleanupPromise) return cleanupPromise;
      closing = true;
      cleanupPromise = (async () => {
        await persist();
        // Remove intercepts before releasing in-flight requests so no new ones block.
        await Promise.all([...owned].map(remove));
        await Promise.all([...blocked].map(release));
        if (subscriptions.size) {
          try { await call('session.unsubscribe', {subscriptions: [...subscriptions]}); } catch {}
        }
        await persist();
        if (owned.size || blocked.size) throw Object.assign(new Error('owned BiDi intercept cleanup incomplete'),
          {code:'BIDI_CLEANUP_INCOMPLETE',remaining_intercepts:owned.size,remaining_requests:blocked.size});
      })();
      return cleanupPromise;
    },
  };
}
