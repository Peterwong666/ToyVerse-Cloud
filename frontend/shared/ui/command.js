/* ============================================================
   命令面板（⌘K / Ctrl+K）
   ------------------------------------------------------------
   用途：快速跳转页面、执行常用操作、搜索资源。
   设计要点：
     * 全局快捷键唤起（Mac 用 ⌘K，其他平台用 Ctrl+K）
     * 支持分组、模糊匹配、键盘上下键选择、回车执行
     * 可动态注册命令（各端在启动时注入自己的菜单命令）
   ============================================================ */

import { clear, esc, h, html, raw } from './dom.js';
import { iconNode } from './icons.js';

/* ------------------------------------------------------------
   一、模糊匹配
   ------------------------------------------------------------ */

/**
 * 简单模糊匹配：字符按顺序出现即命中，并给出评分。
 * 支持中文直接子串匹配与拼音首字母（若命令提供 keywords）。
 *
 * @returns {number} 分数，0 表示不匹配（分数越高越靠前）
 */
function score(query, text, keywords = []) {
  const q = query.toLowerCase().trim();
  if (!q) return 1;

  const target = String(text || '').toLowerCase();

  // 直接子串：最高分
  const directIndex = target.indexOf(q);
  if (directIndex >= 0) return 1000 - directIndex;

  // 关键词命中
  for (const keyword of keywords) {
    const kw = String(keyword || '').toLowerCase();
    if (kw.includes(q)) return 800;
  }

  // 顺序字符匹配
  let cursor = 0;
  let matched = 0;
  for (const ch of q) {
    const found = target.indexOf(ch, cursor);
    if (found < 0) return 0;
    cursor = found + 1;
    matched += 1;
  }
  return matched === q.length ? 400 - (cursor - matched) : 0;
}

/* ------------------------------------------------------------
   二、命令面板
   ------------------------------------------------------------ */

export class CommandPalette {
  constructor({ placeholder = '搜索页面、操作或资源…', emptyText = '没有匹配的结果' } = {}) {
    this.placeholder = placeholder;
    this.emptyText = emptyText;
    /** @type {Array<{id:string,label:string,group?:string,icon?:string,hint?:string,keywords?:string[],run:Function}>} */
    this.commands = [];
    this.filtered = [];
    this.activeIndex = 0;
    this.visible = false;
    this.el = null;
    this._onKeyDown = this._onKeyDown.bind(this);
  }

  /* ---- 命令注册 ---- */

  /**
   * 注册命令。
   * @param {object|Array} command
   */
  register(command) {
    const list = Array.isArray(command) ? command : [command];
    list.forEach((item) => {
      if (!item?.id || typeof item.run !== 'function') {
        throw new Error(`命令定义不合法：${JSON.stringify(item)}`);
      }
      const index = this.commands.findIndex((existing) => existing.id === item.id);
      if (index >= 0) this.commands[index] = item;
      else this.commands.push(item);
    });
    return this;
  }

  /** 清空命令（切换端时使用） */
  clearCommands() {
    this.commands = [];
    return this;
  }

  /* ---- 显隐 ---- */

  open() {
    if (this.visible) return;
    this._mount();
    this.visible = true;
    document.addEventListener('keydown', this._onKeyDown, true);
    requestAnimationFrame(() => {
      const input = this.el.querySelector('.command-input');
      input?.focus();
      input?.select();
    });
  }

  close() {
    if (!this.visible) return;
    this.visible = false;
    document.removeEventListener('keydown', this._onKeyDown, true);
    this.el?.remove();
    this.el = null;
  }

  toggle() {
    if (this.visible) this.close();
    else this.open();
  }

  /* ---- 渲染 ---- */

  _mount() {
    const input = h('input', {
      class: 'command-input',
      type: 'text',
      placeholder: this.placeholder,
      'aria-label': '命令搜索',
      autocomplete: 'off',
      spellcheck: 'false',
    });

    const listEl = h('div', { class: 'command-list', role: 'listbox' });

    const panel = h(
      'div',
      { class: 'command', role: 'dialog', 'aria-modal': 'true', 'aria-label': '命令面板' },
      h(
        'div',
        { class: 'command-input-wrap' },
        h('span', { style: { color: 'var(--text-disabled)' } }).append(
          iconNode('search', { size: 18 }),
        ),
        input,
      ),
      listEl,
      h(
        'div',
        { class: 'command-foot' },
        h('span', {}, h('kbd', { text: '↑' }), ' ', h('kbd', { text: '↓' }), ' 选择'),
        h('span', {}, h('kbd', { text: '↵' }), ' 执行'),
        h('span', {}, h('kbd', { text: 'Esc' }), ' 关闭'),
      ),
    );

    const mask = h('div', { class: 'command-mask show' }, panel);
    document.body.append(mask);
    this.el = mask;
    this.listEl = listEl;
    this.inputEl = input;

    input.addEventListener('input', () => this._applyFilter(input.value));
    mask.addEventListener('mousedown', (event) => {
      if (event.target === mask) this.close();
    });
    listEl.addEventListener('click', (event) => {
      const item = event.target.closest('.command-item');
      if (!item) return;
      this._run(Number(item.dataset.index));
    });

    this._applyFilter('');
  }

  _applyFilter(query) {
    const scored = this.commands
      .map((command) => ({
        command,
        score: score(query, command.label, [command.hint, command.group, ...(command.keywords || [])]),
      }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score);

    this.filtered = scored.map((item) => item.command);
    this.activeIndex = 0;
    this._renderList();
  }

  _renderList() {
    clear(this.listEl);

    if (!this.filtered.length) {
      this.listEl.append(h('div', { class: 'command-empty', text: this.emptyText }));
      return;
    }

    // 按分组聚合，保持组内顺序
    const groups = new Map();
    this.filtered.forEach((command, index) => {
      const key = command.group || '通用';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({ command, index });
    });

    for (const [groupName, items] of groups) {
      this.listEl.append(h('div', { class: 'command-group-title', text: groupName }));

      items.forEach(({ command, index }) => {
        const itemIcon = iconNode(command.icon || 'arrowRight', { size: 16 });

        const node = h(
          'div',
          {
            class: `command-item ${index === this.activeIndex ? 'active' : ''}`,
            'data-index': String(index),
            role: 'option',
            'aria-selected': index === this.activeIndex ? 'true' : 'false',
          },
          h('span', { class: 'command-item-icon' }, itemIcon),
          h('span', { class: 'command-item-label', text: command.label }),
          command.hint ? h('span', { class: 'command-item-hint', text: command.hint }) : null,
        );

        this.listEl.append(node);
      });
    }

    this._scrollActiveIntoView();
  }

  _scrollActiveIntoView() {
    const active = this.listEl.querySelector('.command-item.active');
    active?.scrollIntoView({ block: 'nearest' });
  }

  _move(delta) {
    if (!this.filtered.length) return;
    this.activeIndex = (this.activeIndex + delta + this.filtered.length) % this.filtered.length;
    this._renderList();
  }

  _run(index) {
    const command = this.filtered[index];
    if (!command) return;

    this.close();
    // 让命令中的导航先于面板关闭后的重绘执行
    setTimeout(() => {
      try {
        command.run();
      } catch (error) {
        console.error('[command] 执行失败', error);
      }
    }, 0);
  }

  _onKeyDown(event) {
    if (!this.visible) return;

    switch (event.key) {
      case 'Escape':
        event.preventDefault();
        event.stopPropagation();
        this.close();
        break;
      case 'ArrowDown':
        event.preventDefault();
        this._move(1);
        break;
      case 'ArrowUp':
        event.preventDefault();
        this._move(-1);
        break;
      case 'Enter':
        event.preventDefault();
        this._run(this.activeIndex);
        break;
      case 'Home':
        event.preventDefault();
        this.activeIndex = 0;
        this._renderList();
        break;
      case 'End':
        event.preventDefault();
        this.activeIndex = this.filtered.length - 1;
        this._renderList();
        break;
      default:
        break;
    }
  }
}

/* ------------------------------------------------------------
   三、全局快捷键绑定
   ------------------------------------------------------------ */

/**
 * 绑定 ⌘K / Ctrl+K 唤起命令面板。
 * @param {CommandPalette} palette
 * @returns {Function} 解绑
 */
export function bindCommandShortcut(palette) {
  const handler = (event) => {
    const isMac = navigator.platform.toLowerCase().includes('mac');
    const modifier = isMac ? event.metaKey : event.ctrlKey;

    if (modifier && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      palette.toggle();
    }
  };

  document.addEventListener('keydown', handler);
  return () => document.removeEventListener('keydown', handler);
}

/**
 * 由菜单定义生成导航类命令。
 * @param {Array} menus 菜单分组
 * @param {(path:string)=>void} navigate
 */
export function commandsFromMenus(menus = [], navigate) {
  const commands = [];
  menus.forEach((group) => {
    (group.items || []).forEach((item) => {
      if (!item.path || item.hidden) return;
      commands.push({
        id: `nav:${item.path}`,
        label: item.label,
        group: group.groupTitle || '导航',
        icon: item.icon,
        hint: '跳转',
        keywords: [item.path],
        run: () => navigate(item.path),
      });
    });
  });
  return commands;
}

export default { CommandPalette, bindCommandShortcut, commandsFromMenus };
