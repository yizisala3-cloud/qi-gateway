// One controller per mounted planning page. Invalidations separate reads begun
// before a write from the fresh read after it; identical in-flight reads share work.
export function createPlanningReads() {
  const lists = new Map(['today', 'tasks', 'occurrences'].map((name) => [name, {
    revision: 0, status: 'unloaded', hasData: false, key: null, pending: null,
  }]));
  let disposed = false;

  const invalidate = (names = [...lists.keys()]) => {
    for (const name of names) {
      const list = lists.get(name);
      list.revision += 1;
      list.status = list.hasData ? 'invalid' : 'unloaded';
    }
  };

  return {
    invalidate,
    needsRead(name) { return lists.get(name).status !== 'loaded'; },
    hasData(name) { return lists.get(name).hasData; },
    dispose() { disposed = true; },
    read(name, { key = '', request, apply }) {
      if (disposed) return Promise.resolve({ ignored: true });
      const list = lists.get(name);
      if (list.key !== null && list.key !== key) invalidate([name]);
      list.key = key;
      if (list.pending?.revision === list.revision && list.pending.key === key) {
        return list.pending.promise;
      }
      const pending = { revision: list.revision, key, promise: null };
      list.pending = pending;
      const current = () => !disposed && list.revision === pending.revision && list.key === key;
      pending.promise = (async () => {
        try {
          const data = await request();
          if (!current()) return { ignored: true };
          apply(data);
          list.hasData = true;
          list.status = 'loaded';
          return { ok: true };
        } catch (error) {
          if (!current()) return { ignored: true };
          list.status = list.hasData ? 'invalid' : 'unloaded';
          return { error };
        } finally {
          if (list.pending === pending) list.pending = null;
        }
      })();
      return pending.promise;
    },
  };
}
