/* ============================================================
   纯 SVG 图表
   ------------------------------------------------------------
   为什么手写而不引入图表库：
     前端为零构建、零依赖方案，引入 ECharts/Chart.js 会破坏
     「克隆即可运行」的目标。这里用原生 SVG 实现常用图表，
     体积为零且风格与设计系统完全一致。

   已实现：柱状图、折线图、环形图、热力图、迷你趋势线
   ============================================================ */

import { esc, formatNumber, html, raw } from './dom.js';

const DEFAULT_HEIGHT = 220;
/** 坐标轴与标签留白 */
const PADDING = { top: 16, right: 16, bottom: 28, left: 40 };

/** 生成 SVG 的 viewBox 宽度基准（实际宽度由 CSS 拉伸） */
const VIEW_WIDTH = 640;

function niceMax(value) {
  const num = Number(value) || 0;
  if (num <= 0) return 10;
  const magnitude = 10 ** Math.floor(Math.log10(num));
  const normalized = num / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

/* ------------------------------------------------------------
   一、柱状图
   ------------------------------------------------------------ */

/**
 * @param {object} o
 * @param {Array<{label:string,value:number,color?:string}>} o.data
 * @param {number} [o.height]
 * @param {string} [o.color]
 * @param {(v:number)=>string} [o.formatter]
 * @param {boolean}[o.showValues]
 */
export function barChart(o = {}) {
  const { data = [], height = DEFAULT_HEIGHT, color = '', formatter = formatNumber, showValues = false } = o;

  if (!data.length) return emptyChart(height);

  const width = VIEW_WIDTH;
  const innerWidth = width - PADDING.left - PADDING.right;
  const innerHeight = height - PADDING.top - PADDING.bottom;
  const max = niceMax(Math.max(...data.map((d) => Number(d.value) || 0)));
  const slot = innerWidth / data.length;
  const barWidth = Math.max(6, Math.min(slot * 0.6, 48));

  const gridLines = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const y = PADDING.top + innerHeight * (1 - ratio);
      const value = max * ratio;
      return `
        <line x1="${PADDING.left}" y1="${y}" x2="${width - PADDING.right}" y2="${y}"
              stroke="var(--chart-grid)" stroke-width="1" />
        <text x="${PADDING.left - 8}" y="${y + 4}" text-anchor="end"
              font-size="10" fill="var(--chart-axis)">${esc(formatter(value))}</text>`;
    })
    .join('');

  const bars = data
    .map((item, index) => {
      const value = Number(item.value) || 0;
      const barHeight = max > 0 ? (value / max) * innerHeight : 0;
      const x = PADDING.left + slot * index + (slot - barWidth) / 2;
      const y = PADDING.top + innerHeight - barHeight;
      const fill = item.color || color || 'var(--chart-1)';

      const valueText = showValues
        ? `<text x="${x + barWidth / 2}" y="${y - 6}" text-anchor="middle"
                font-size="10" fill="var(--ink-600)">${esc(formatter(value))}</text>`
        : '';

      return `
        <g class="bar-group" data-index="${index}">
          <rect x="${x}" y="${y}" width="${barWidth}" height="${Math.max(barHeight, 0)}"
                rx="4" fill="${fill}" opacity="0.9" />
          <rect x="${x}" y="${PADDING.top}" width="${barWidth}" height="${innerHeight}"
                fill="transparent" data-hit="true" />
          <text x="${x + barWidth / 2}" y="${height - 8}" text-anchor="middle"
                font-size="10" fill="var(--chart-axis)">${esc(item.label)}</text>
          ${valueText}
        </g>`;
    })
    .join('');

  return html`<div class="chart">
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="柱状图"
         xmlns="http://www.w3.org/2000/svg">
      ${raw(gridLines)}
      ${raw(bars)}
      <line x1="${PADDING.left}" y1="${PADDING.top + innerHeight}" x2="${width - PADDING.right}"
            y2="${PADDING.top + innerHeight}" stroke="var(--line)" stroke-width="1" />
    </svg>
  </div>`;
}

/* ------------------------------------------------------------
   二、折线图
   ------------------------------------------------------------ */

/**
 * @param {object} o
 * @param {Array<{label:string,value:number}>} o.data
 * @param {number} [o.height]
 * @param {string} [o.color]
 * @param {boolean}[o.area=true] 是否填充面积
 * @param {(v:number)=>string} [o.formatter]
 */
export function lineChart(o = {}) {
  const { data = [], height = DEFAULT_HEIGHT, color = '', area = true, formatter = formatNumber } = o;

  if (data.length < 2) return emptyChart(height, '数据不足，无法绘制趋势');

  const width = VIEW_WIDTH;
  const innerWidth = width - PADDING.left - PADDING.right;
  const innerHeight = height - PADDING.top - PADDING.bottom;
  const max = niceMax(Math.max(...data.map((d) => Number(d.value) || 0)));
  const stepX = innerWidth / (data.length - 1);
  const stroke = color || 'var(--chart-1)';

  const points = data.map((item, index) => {
    const value = Number(item.value) || 0;
    return {
      x: PADDING.left + stepX * index,
      y: PADDING.top + innerHeight - (max > 0 ? (value / max) * innerHeight : 0),
      value,
      label: item.label,
    };
  });

  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  const areaD = `${pathD} L${points[points.length - 1].x.toFixed(1)},${PADDING.top + innerHeight} L${points[0].x.toFixed(1)},${PADDING.top + innerHeight} Z`;

  const gridLines = [0, 0.5, 1]
    .map((ratio) => {
      const y = PADDING.top + innerHeight * (1 - ratio);
      return `
        <line x1="${PADDING.left}" y1="${y}" x2="${width - PADDING.right}" y2="${y}"
              stroke="var(--chart-grid)" stroke-width="1" />
        <text x="${PADDING.left - 8}" y="${y + 4}" text-anchor="end"
              font-size="10" fill="var(--chart-axis)">${esc(formatter(max * ratio))}</text>`;
    })
    .join('');

  // x 轴标签：点太多时隔位显示，避免重叠
  const labelStep = Math.ceil(data.length / 8);
  const xLabels = points
    .map((p, index) =>
      index % labelStep === 0
        ? `<text x="${p.x}" y="${height - 8}" text-anchor="middle" font-size="10"
                 fill="var(--chart-axis)">${esc(p.label)}</text>`
        : '',
    )
    .join('');

  const dots = points
    .map(
      (p, index) => `
      <circle cx="${p.x}" cy="${p.y}" r="8" fill="transparent" data-point-index="${index}" />
      <circle cx="${p.x}" cy="${p.y}" r="2.5" fill="${stroke}" />`,
    )
    .join('');

  return html`<div class="chart">
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="折线图"
         xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="lineArea" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${stroke}" stop-opacity="0.22" />
          <stop offset="100%" stop-color="${stroke}" stop-opacity="0" />
        </linearGradient>
      </defs>
      ${raw(gridLines)}
      ${raw(area ? `<path d="${areaD}" fill="url(#lineArea)" />` : '')}
      <path d="${pathD}" fill="none" stroke="${stroke}" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round" />
      ${raw(dots)}
      ${raw(xLabels)}
      <line x1="${PADDING.left}" y1="${PADDING.top + innerHeight}" x2="${width - PADDING.right}"
            y2="${PADDING.top + innerHeight}" stroke="var(--line)" stroke-width="1" />
    </svg>
  </div>`;
}

/* ------------------------------------------------------------
   三、环形图
   ------------------------------------------------------------ */

/**
 * @param {object} o
 * @param {number} o.value
 * @param {number} [o.max=100]
 * @param {string} [o.label]
 * @param {string} [o.color]
 * @param {number} [o.size=140]
 * @param {string} [o.suffix='%']
 */
export function ringChart(o = {}) {
  const { value = 0, max = 100, label = '', color = '', size = 140, suffix = '%' } = o;

  const radius = 54;
  const strokeWidth = 12;
  const circumference = 2 * Math.PI * radius;
  const ratio = Math.max(0, Math.min(1, max > 0 ? Number(value) / max : 0));
  const dash = circumference * ratio;
  const stroke = color || 'var(--chart-1)';

  const display = suffix === '%' ? `${(ratio * 100).toFixed(1)}${suffix}` : `${formatNumber(value)}${suffix}`;

  return html`<div class="chart" style="width:${size}px;position:relative">
    <svg viewBox="0 0 140 140" role="img" aria-label="${label || '占比'}">
      <circle cx="70" cy="70" r="${radius}" fill="none"
              stroke="var(--surface-sunken)" stroke-width="${strokeWidth}" />
      <circle cx="70" cy="70" r="${radius}" fill="none"
              stroke="${stroke}" stroke-width="${strokeWidth}"
              stroke-linecap="round"
              stroke-dasharray="${dash.toFixed(2)} ${(circumference - dash).toFixed(2)}"
              transform="rotate(-90 70 70)" />
    </svg>
    <div class="chart-ring-center">
      <div class="chart-ring-value">${display}</div>
      ${raw(label ? html`<div class="chart-ring-label">${label}</div>` : '')}
    </div>
  </div>`;
}

/* ------------------------------------------------------------
   四、热力图（24 小时交互分布）
   ------------------------------------------------------------ */

/** 热力色阶（由浅到深，取品牌色系） */
const HEAT_SCALE = ['#eef2ff', '#c7d2fe', '#a5b4fc', '#818cf8', '#6366f1', '#4f46e5', '#4338ca'];

function heatColor(value, max) {
  if (!value) return HEAT_SCALE[0];
  const ratio = max > 0 ? value / max : 0;
  const index = Math.min(HEAT_SCALE.length - 1, Math.floor(ratio * HEAT_SCALE.length));
  return HEAT_SCALE[index];
}

/**
 * @param {object} o
 * @param {number[]} o.values 24 个数值（按小时）
 * @param {number} [o.cellSize=20]
 * @param {number} [o.gap=4]
 */
export function heatmap(o = {}) {
  const { values = [], cellSize = 26, gap = 4, label = '交互次数' } = o;
  if (!values.length) return emptyChart(120, '暂无数据');

  const max = Math.max(...values.map((v) => Number(v) || 0));

  // 按 24 小时排成 2 行 × 12 列，便于阅读
  const cols = 12;
  const rows = Math.ceil(values.length / cols);
  const width = cols * (cellSize + gap) - gap;
  const height = rows * (cellSize + gap) - gap + 18;

  const cells = values
    .map((value, index) => {
      const col = index % cols;
      const row = Math.floor(index / cols);
      const x = col * (cellSize + gap);
      const y = row * (cellSize + gap) + 18;
      const num = Number(value) || 0;
      return `
        <g>
          <rect x="${x}" y="${y}" width="${cellSize}" height="${cellSize}" rx="4"
                fill="${heatColor(num, max)}" data-heat-index="${index}">
            <title>${index}:00 · ${esc(label)} ${formatNumber(num)}</title>
          </rect>
          <text x="${x + cellSize / 2}" y="${y + 14}" text-anchor="middle"
                font-size="9" fill="var(--chart-axis)">${index}</text>
        </g>`;
    })
    .join('');

  const legend = HEAT_SCALE.map(
    (c) => `<span style="background:${c}"></span>`,
  ).join('');

  return html`<div class="chart">
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="热力图"
         style="max-width:${width}px" xmlns="http://www.w3.org/2000/svg">
      ${raw(cells)}
    </svg>
    <div class="heatmap-legend">
      <span>低</span>
      <div class="heatmap-scale">${raw(legend)}</div>
      <span>高</span>
      <span class="ml-auto">峰值 ${formatNumber(max)} 次</span>
    </div>
  </div>`;
}

/* ------------------------------------------------------------
   五、迷你趋势线（表格内联）
   ------------------------------------------------------------ */

/**
 * @param {number[]} values
 * @param {object} [o] width / height / color
 */
export function sparkline(values = [], o = {}) {
  const { width = 96, height = 28, color = '' } = o;
  if (values.length < 2) return html`<span class="text-disabled">-</span>`;

  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 1;
  const stepX = width / (values.length - 1);

  const points = values.map((value, index) => {
    const x = stepX * index;
    const y = height - ((value - min) / range) * (height - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  return html`<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"
    fill="none" stroke="${color || 'var(--chart-1)'}" stroke-width="1.6"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <polyline points="${raw(points.join(' '))}" />
  </svg>`;
}

/* ------------------------------------------------------------
   六、堆叠条（占比条）
   ------------------------------------------------------------ */

/**
 * @param {Array<{label:string,value:number,color:string}>} segments
 */
export function stackedBar(segments = []) {
  const total = segments.reduce((sum, item) => sum + (Number(item.value) || 0), 0);
  if (!total) return emptyChart(60, '暂无数据');

  const bars = segments
    .map((item) => {
      const percent = ((Number(item.value) || 0) / total) * 100;
      return `<div style="width:${percent}%;background:${item.color || 'var(--chart-1)'}"
                   title="${esc(item.label)} ${formatNumber(item.value)}"></div>`;
    })
    .join('');

  const legend = segments
    .map(
      (item, index) => `
      <span class="chart-legend-item">
        <span class="chart-legend-swatch"
              style="background:${item.color || `var(--chart-${(index % 6) + 1})`}"></span>
        <span>${esc(item.label)}</span>
        <span class="num text-secondary">${formatNumber(item.value)}</span>
      </span>`,
    )
    .join('');

  return html`<div class="chart">
    <div style="display:flex;height:10px;border-radius:999px;overflow:hidden;gap:2px">
      ${raw(bars)}
    </div>
    <div class="chart-legend">${raw(legend)}</div>
  </div>`;
}

/* ------------------------------------------------------------
   七、空图表占位
   ------------------------------------------------------------ */

function emptyChart(height = DEFAULT_HEIGHT, text = '暂无数据') {
  return html`<div class="chart flex-center" style="height:${height}px">
    <span class="text-sm text-secondary">${text}</span>
  </div>`;
}

/* ------------------------------------------------------------
   八、悬浮提示（配合图表容器使用）
   ------------------------------------------------------------ */

/**
 * 绑定图表悬浮提示。
 * @param {HTMLElement} container 图表容器
 * @param {(index:number)=>string} formatter 返回提示 HTML
 */
export function bindChartTooltip(container, formatter) {
  const svg = container.querySelector('svg');
  if (!svg) return () => {};

  let tooltip = container.querySelector('.chart-tooltip');

  const onMove = (event) => {
    const target = event.target.closest('[data-hit], [data-point-index], [data-heat-index]');
    if (!target) {
      tooltip?.remove();
      tooltip = null;
      return;
    }

    const indexAttr =
      target.dataset.pointIndex ?? target.dataset.heatIndex ?? target.closest('[data-index]')?.dataset.index;
    if (indexAttr === undefined) return;

    if (!tooltip) {
      tooltip = document.createElement('div');
      tooltip.className = 'chart-tooltip';
      container.append(tooltip);
    }

    tooltip.innerHTML = formatter(Number(indexAttr));

    const rect = container.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    tooltip.style.left = `${targetRect.left - rect.left + targetRect.width / 2}px`;
    tooltip.style.top = `${targetRect.top - rect.top}px`;
  };

  const onLeave = () => {
    tooltip?.remove();
    tooltip = null;
  };

  container.addEventListener('mousemove', onMove);
  container.addEventListener('mouseleave', onLeave);

  return () => {
    container.removeEventListener('mousemove', onMove);
    container.removeEventListener('mouseleave', onLeave);
    tooltip?.remove();
  };
}

export default { barChart, lineChart, ringChart, heatmap, sparkline, stackedBar, bindChartTooltip };
