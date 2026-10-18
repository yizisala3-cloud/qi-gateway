export function createPlanningActions() {
  const pending = new Map();
  return {
    run(key, { button = null, label = '正在处理…' } = {}, operation) {
      if (pending.has(key)) return pending.get(key);
      const previous = button ? { html: button.innerHTML, disabled: button.disabled,
        busy: button.getAttribute?.('aria-busy') ?? null } : null;
      if (button) {
        button.disabled = true;
        button.textContent = label;
        button.setAttribute?.('aria-busy', 'true');
      }
      let resolveOperation, rejectOperation;
      const execution = new Promise((resolve, reject) => { resolveOperation = resolve; rejectOperation = reject; });
      const promise = execution.finally(() => {
        pending.delete(key);
        if (button && button.isConnected !== false) {
          button.innerHTML = previous.html;
          button.disabled = previous.disabled;
          if (previous.busy === null) button.removeAttribute?.('aria-busy');
          else button.setAttribute?.('aria-busy', previous.busy);
        }
      });
      pending.set(key, promise);
      try { Promise.resolve(operation()).then(resolveOperation, rejectOperation); }
      catch (error) { rejectOperation(error); }
      return promise;
    },
  };
}
