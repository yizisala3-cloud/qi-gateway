// lib/memo_markdown.js - 备忘录 Markdown 安全渲染（一期，需求 §2.3/M03）
//
// 解析用 marked、净化用 DOMPurify，两个依赖以固定版本 UMD 构建随项目交付
// （admin/assets/vendor/，文件名带版本），经 index.html 的 <script> 挂到
// window。**绝不把未经 DOMPurify 净化的解析结果写入 innerHTML**：依赖缺失
// 时退回纯文本展示（textContent → innerHTML 自动转义），不冒充渲染成功。

export function renderMarkdown(raw) {
  const text = String(raw ?? '');
  const marked = globalThis.marked;
  const purify = globalThis.DOMPurify;
  if (!marked || !purify || typeof marked.parse !== 'function'
      || typeof purify.sanitize !== 'function') {
    const holder = document.createElement('div');
    holder.textContent = text;
    return holder.innerHTML;
  }
  let html = '';
  try {
    html = marked.parse(text, { async: false, gfm: true, breaks: true });
  } catch {
    const holder = document.createElement('div');
    holder.textContent = text;
    return holder.innerHTML;
  }
  if (typeof html !== 'string') {
    const holder = document.createElement('div');
    holder.textContent = text;
    return holder.innerHTML;
  }
  // 默认配置已移除脚本/事件属性/危险协议；显式固定同一配置避免随版本漂移
  return purify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form', 'input', 'button'],
    FORBID_ATTR: ['style'],
  });
}
