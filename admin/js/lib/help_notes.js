// Optional explanations use native disclosures; the original node and IDs stay intact.
import { icon } from '../ui.js?v=20261014-admin-layout-help-scrollbars';

const initializers = new WeakMap();

export function initHelpNotes(root = document) {
  if (initializers.has(root)) return initializers.get(root);
  const doc = root.ownerDocument || root;
  const relocated = new Map();
  const prepare = note => {
    if (note.closest('.help-note')) return;
    const label = note.closest('label');
    const target = doc.getElementById(note.getAttribute('data-help-for') || '');
    let field = target && root.contains(target) ? target.closest('.field') : note.closest('.field');
    // Some dialogs place an explanation next to its field instead of inside it.
    if (field === note && !field.querySelector('label')) {
      field = note.previousElementSibling?.matches('.field') ? note.previousElementSibling
        : note.nextElementSibling?.matches('.field') ? note.nextElementSibling : null;
    }
    const fieldLabel = field && [...field.children].find(node => node.tagName === 'LABEL');
    const anchor = fieldLabel || label;
    const existing = anchor?.nextElementSibling?.matches('.help-note') ? anchor.nextElementSibling : null;
    if (target && anchor) {
      // The source renderer still owns this marker, so replacing it also removes old help.
      const origin = doc.createComment('help-note-source');
      note.before(origin);
      relocated.set(note, origin);
    }
    note.classList.add('help-note-content');
    if (existing) {
      existing.append(note);
      return;
    }
    const labelCopy = fieldLabel?.cloneNode(true);
    labelCopy?.querySelectorAll('.req-mark, .char-count, [data-help-note]').forEach(node => node.remove());
    const context = note.getAttribute('data-help-note')
      || labelCopy?.textContent.trim()
      || '补充';
    const disclosure = doc.createElement('details');
    disclosure.className = `help-note${anchor ? ' help-note-inline' : ''}`;
    const toggle = doc.createElement('summary');
    toggle.className = 'help-note-toggle';
    toggle.tabIndex = 0;
    toggle.setAttribute('aria-label', `${context}说明`);
    toggle.innerHTML = icon('info');
    // Keep a summary outside labels so opening help cannot activate a form field.
    if (anchor) anchor.after(disclosure);
    else note.before(disclosure);
    fieldLabel?.querySelectorAll('.char-count').forEach(counter => field.append(counter));
    disclosure.append(toggle, note);
  };
  const scan = node => {
    if (node.nodeType !== 1 && node.nodeType !== 9) return;
    if (node.matches?.('[data-help-note]')) prepare(node);
    node.querySelectorAll('[data-help-note]').forEach(prepare);
  };
  scan(root);
  const observer = new MutationObserver(mutations => {
    for (const [note, origin] of relocated) {
      if (!root.contains(origin)) {
        note.remove();
        relocated.delete(note);
      }
    }
    for (const mutation of mutations) mutation.addedNodes.forEach(scan);
  });
  observer.observe(root, { childList: true, subtree: true });
  const dispose = () => {
    observer.disconnect();
    for (const [note, origin] of relocated) {
      if (root.contains(origin)) origin.replaceWith(note);
      else note.remove();
    }
    relocated.clear();
    initializers.delete(root);
  };
  initializers.set(root, dispose);
  return dispose;
}
