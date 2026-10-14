// Measure native scrollbar tracks without changing scroll containers or scroll behavior.
const initializers = new WeakMap();
const trackProperties = ['--scrollbar-track-top', '--scrollbar-track-bottom', '--scrollbar-track-left', '--scrollbar-track-right'];

function trackMargins(size, viewportSize) {
  const edge = Math.min(8, size / 2);
  const inset = Math.min(96, Math.max(24, viewportSize * .12));
  const length = Math.min(Math.max(0, size - 2 * edge), Math.max(28, (size - 2 * inset) * 2 / 15));
  return [Math.max(edge, size - length - edge), edge];
}

export function initScrollbars(root = document) {
  if (initializers.has(root)) return initializers.get(root);
  const doc = root.ownerDocument || root;
  const win = doc.defaultView;
  const scrollers = new Set();
  const pending = new Set();
  let frame = null;

  const measure = el => {
    const [top, bottom] = trackMargins(el.clientHeight, win.innerHeight);
    const [left, right] = trackMargins(el.clientWidth, win.innerWidth);
    [top, bottom, left, right].forEach((value, index) => {
      const pixels = `${value}px`;
      if (el.style.getPropertyValue(trackProperties[index]) !== pixels) {
        el.style.setProperty(trackProperties[index], pixels);
      }
    });
  };
  const resize = new ResizeObserver(entries => entries.forEach(({ target }) => measure(target)));
  const consider = el => {
    const style = win.getComputedStyle(el);
    if (!/auto|scroll/.test(`${style.overflowX} ${style.overflowY}`)) return;
    if (!scrollers.has(el)) {
      scrollers.add(el);
      el.setAttribute('data-unified-scrollbar', '');
      resize.observe(el);
    }
    measure(el);
  };
  const scan = node => {
    if (node.nodeType !== 1 && node.nodeType !== 9) return;
    if (node.nodeType === 1) consider(node);
    node.querySelectorAll('*').forEach(consider);
  };
  const flush = () => {
    frame = null;
    for (const el of scrollers) {
      if (!root.contains(el)) {
        resize.unobserve(el);
        scrollers.delete(el);
      }
    }
    const nodes = [...pending];
    pending.clear();
    nodes.filter(node => node === root || root.contains(node)).forEach(scan);
  };
  const queue = node => {
    pending.add(node);
    if (frame === null) frame = win.requestAnimationFrame(flush);
  };
  const mutations = new MutationObserver(entries => {
    for (const entry of entries) queue(entry.target);
  });
  scan(root);
  mutations.observe(root, { childList: true, subtree: true, attributes: true,
    attributeFilter: ['class', 'style', 'hidden', 'open'] });
  const viewportChanged = () => queue(root);
  win.addEventListener('resize', viewportChanged);
  win.visualViewport?.addEventListener('resize', viewportChanged);

  const dispose = () => {
    mutations.disconnect();
    resize.disconnect();
    if (frame !== null) win.cancelAnimationFrame(frame);
    win.removeEventListener('resize', viewportChanged);
    win.visualViewport?.removeEventListener('resize', viewportChanged);
    for (const el of scrollers) {
      el.removeAttribute('data-unified-scrollbar');
      trackProperties.forEach(property => el.style.removeProperty(property));
    }
    scrollers.clear();
    pending.clear();
    initializers.delete(root);
  };
  initializers.set(root, dispose);
  return dispose;
}
