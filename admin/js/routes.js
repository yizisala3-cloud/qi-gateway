export const NAV = [
  { title: '', items: [
    { key: 'dashboard', icon: '\u{1F4CA}', label: '\u4EEA\u8868\u76D8' },
  ]},
  { title: '\u8BB0\u5FC6\u7CFB\u7EDF', items: [
    { key: 'memory_requests', icon: '\u{1F4E5}', label: '\u8BB0\u5FC6\u7533\u8BF7' },
    { key: 'memories', icon: '\u{1F9E9}', label: '\u8BB0\u5FC6\u7BA1\u7406' },
    { key: 'digest', icon: '\u{1F9EA}', label: '\u603B\u7ED3\u4EFB\u52A1' },
    { key: 'persona', icon: '\u{1F4DD}', label: '\u4EBA\u8BBE' },
  ]},
  { title: '\u60C5\u7EEA\u5F15\u64CE', items: [
    { key: 'jiwen', icon: '\u{1F321}\uFE0F', label: '\u79EF\u6E29\u72B6\u6001' },
  ]},
  { title: '\u7CFB\u7EDF', items: [
    { key: 'timers', icon: '\u23F0', label: '\u5B9A\u65F6\u5668' },
    { key: 'status', icon: '\u{1F6A6}', label: '\u7F51\u5173\u72B6\u6001' },
  ]},
];

export const ROUTE_INDEX = {};
for (const grp of NAV) for (const it of grp.items) {
  ROUTE_INDEX[it.key] = { ...it, group: grp.title || '' };
}

