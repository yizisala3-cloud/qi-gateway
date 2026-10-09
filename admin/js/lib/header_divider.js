// Place the petals beside the first row of controls; use the right edge if full.
// PNG coordinates describe the first flower, rather than the long bare stem.
const FLOWERS = {
  wisteria: { width: 716, height: 371, petalStart: 256 },
  lily3: { width: 649, height: 228, petalStart: 438 },
};
const EDGE_PAGES = new Set(['planning', 'emotion', 'config']);
const PETAL_GAP = 12;

function visibleRect(element) {
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 ? rect : null;
}

export function initHeaderDivider() {
  const rule = document.querySelector('.page-head-rule');
  const root = document.getElementById('page-root');
  if (!rule || !root) return;

  let frame = 0;
  const observed = new Set([rule]);
  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(update);
  };
  const resize = new ResizeObserver(schedule);
  resize.observe(rule);

  function update() {
    frame = 0;
    if (rule.dataset.variant === 'straight') {
      rule.style.removeProperty('--phr-right-gap');
      return;
    }
    const page = rule.dataset.page;
    const geometry = FLOWERS[rule.dataset.variant] || FLOWERS.wisteria;
    const bounds = rule.getBoundingClientRect();
    if (!bounds.width) return;

    if (rule.dataset.variant === 'wisteria' || rule.dataset.variant === 'lily3') {
      const imageHeight = parseFloat(getComputedStyle(rule, '::after').height);
      const pixelRatio = window.devicePixelRatio || 1;
      const finialWidth = imageHeight * 117 / geometry.height;
      const edge = Math.round((bounds.left + finialWidth) * pixelRatio) / pixelRatio - bounds.left;
      const value = `${edge}px`;
      if (rule.style.getPropertyValue('--phr-finial-edge') !== value) {
        rule.style.setProperty('--phr-finial-edge', value);
      }
    }

    const tabs = root.querySelector('.tabs');
    const toolbar = root.querySelector('.toolbar');
    const targets = new Set([rule]);
    if (tabs) targets.add(tabs);
    if (toolbar) targets.add(toolbar);
    for (const element of observed) {
      if (!targets.has(element)) {
        resize.unobserve(element);
        observed.delete(element);
      }
    }
    for (const element of targets) {
      if (!observed.has(element)) {
        resize.observe(element);
        observed.add(element);
      }
    }

    let rightGap = 0;
    if (!EDGE_PAGES.has(page)) {
      const controls = tabs
        ? [...tabs.querySelectorAll('button')]
        : [...(toolbar?.querySelectorAll('button, select, input') || [])];
      const rectangles = controls.map(visibleRect).filter(Boolean);
      if (rectangles.length) {
        const firstTop = Math.min(...rectangles.map(rect => rect.top));
        const firstRow = rectangles.filter(rect => Math.abs(rect.top - firstTop) < 8);
        const controlRight = Math.max(...firstRow.map(rect => rect.right));
        const imageHeight = parseFloat(getComputedStyle(rule, '::after').height);
        const scale = imageHeight / geometry.height;
        // Only the flowering end needs the empty slot; the stem stays above tabs.
        const flowerRight = controlRight + PETAL_GAP
          + (geometry.width - geometry.petalStart) * scale;

        // A refresh button can occupy the same toolbar to the right of the tabs.
        const neighbours = tabs && toolbar?.contains(tabs)
          ? [...toolbar.querySelectorAll('button, select, input')]
              .filter(element => !tabs.contains(element))
              .map(visibleRect).filter(Boolean)
              .filter(rect => Math.abs(rect.top - firstTop) < 8 && rect.left >= controlRight)
          : [];
        const slotRight = Math.min(bounds.right, ...neighbours.map(rect => rect.left - 8));
        if (flowerRight <= slotRight) rightGap = bounds.right - flowerRight;
      }
    }

    const value = `${Math.max(0, rightGap).toFixed(2)}px`;
    if (rule.style.getPropertyValue('--phr-right-gap') !== value) {
      rule.style.setProperty('--phr-right-gap', value);
    }
  }

  new MutationObserver(schedule).observe(root, {
    childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'],
  });
  new MutationObserver(schedule).observe(rule, {
    attributes: true, attributeFilter: ['data-page', 'data-variant'],
  });
  window.addEventListener('resize', schedule);
  document.fonts.ready.then(schedule);
  schedule();
}
