// routes.js - seven fixed top-level pages
export const NAV = [
  {
    title: '',
    items: [
      { key: 'planning', icon: 'calendar', label: '规划管理', desc: '待办排程与时间管理' },
      { key: 'memories', icon: 'book', label: '记忆管理', desc: '记忆库、审核申请与召回记录' },
      { key: 'digest', icon: 'scroll', label: '记忆总结', desc: '连续感总结的执行与运行记录' },
      { key: 'emotion', icon: 'heart', label: '情感', desc: 'Eventide 身体状态' },
    ],
  },
  {
    title: '设定',
    items: [
      { key: 'persona', icon: 'feather', label: '人设与规则', desc: '人设、用户资料与互动规则' },
    ],
  },
  {
    title: '系统',
    items: [
      { key: 'config', icon: 'gear', label: '配置', desc: '网关与依赖组件的真实状态' },
      { key: 'logs', icon: 'journal', label: '日志', desc: '全网关报错日志查询' },
    ],
  },
];

export const ROUTE_INDEX = {};
for (const grp of NAV) for (const it of grp.items) {
  ROUTE_INDEX[it.key] = { ...it, group: grp.title || '' };
}
