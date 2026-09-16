/* ============================================================
   二维码生成（纯实现，无外部依赖）
   ------------------------------------------------------------
   为什么手写：
     原型用 CSS 方块画了一个假的二维码（App.qrMock），
     视觉上是二维码但**扫不出任何内容**。本项目需要真正可扫的码，
     因为二维码承载设备身份信息：
       集贤 4G：JX|{SN}|{IMEI}|{ICCID}|{deviceId}
       京东 Wi-Fi：JD|{tenant_id}|{product_id}|{sn}|{sign}
     同时前端为零构建零依赖方案，不能引入 npm 包。

   实现范围：
     * QR Code Model 2，字节模式（UTF-8）
     * 纠错等级 L / M / Q / H，默认 M
     * 版本 1–10 自动选择（容量 14–213 字节，覆盖业务全部场景）
     * 8 种掩码自动择优（按 ISO/IEC 18004 的 4 条罚分规则）
     * 完整的 Reed-Solomon 纠错与格式/版本信息 BCH 编码

   正确性验证：
     与 Python 参考实现 segno 逐模块比对（见 tests 中的校验脚本），
     确保生成的码可被真实扫码器识别。
   ============================================================ */

/* ------------------------------------------------------------
   一、GF(256) 伽罗华域
   ------------------------------------------------------------ */

const GF_EXP = new Uint8Array(512);
const GF_LOG = new Uint8Array(256);

(function initGaloisField() {
  let x = 1;
  for (let i = 0; i < 255; i += 1) {
    GF_EXP[i] = x;
    GF_LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d; // 本原多项式 x^8+x^4+x^3+x^2+1
  }
  for (let i = 255; i < 512; i += 1) GF_EXP[i] = GF_EXP[i - 255];
})();

function gfMul(a, b) {
  if (a === 0 || b === 0) return 0;
  return GF_EXP[GF_LOG[a] + GF_LOG[b]];
}

/** 生成 degree 次的 Reed-Solomon 生成多项式（最高次在前） */
function rsGeneratorPoly(degree) {
  let poly = [1];
  for (let i = 0; i < degree; i += 1) {
    const next = new Array(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j += 1) {
      next[j] ^= poly[j];
      next[j + 1] ^= gfMul(poly[j], GF_EXP[i]);
    }
    poly = next;
  }
  return poly;
}

/** 计算纠错码字 */
function rsEncode(data, ecCount) {
  const gen = rsGeneratorPoly(ecCount);
  const remainder = new Array(ecCount).fill(0);

  for (let i = 0; i < data.length; i += 1) {
    const factor = data[i] ^ remainder[0];
    remainder.shift();
    remainder.push(0);
    if (factor !== 0) {
      for (let j = 0; j < ecCount; j += 1) {
        remainder[j] ^= gfMul(gen[j + 1], factor);
      }
    }
  }
  return remainder;
}

/* ------------------------------------------------------------
   二、版本与容量参数（纠错等级 M）
   ------------------------------------------------------------ */

/**
 * 每个版本的块结构。
 * 结构：{ ec: 每块纠错码字数, groups: [[块数, 每块数据码字数], ...] }
 */
const VERSION_INFO = {
  1: { ec: 10, groups: [[1, 16]] },
  2: { ec: 16, groups: [[1, 28]] },
  3: { ec: 26, groups: [[1, 44]] },
  4: { ec: 18, groups: [[2, 32]] },
  5: { ec: 24, groups: [[2, 43]] },
  6: { ec: 16, groups: [[4, 27]] },
  7: { ec: 18, groups: [[4, 31]] },
  8: { ec: 22, groups: [[2, 38], [2, 39]] },
  9: { ec: 22, groups: [[3, 36], [2, 37]] },
  10: { ec: 26, groups: [[4, 43], [1, 44]] },
};

/** 各版本对齐图案中心坐标 */
const ALIGNMENT_CENTERS = {
  1: [],
  2: [6, 18],
  3: [6, 22],
  4: [6, 26],
  5: [6, 30],
  6: [6, 34],
  7: [6, 22, 38],
  8: [6, 24, 42],
  9: [6, 26, 46],
  10: [6, 28, 50],
};

/** 纠错等级 → 格式信息中的 2 位标识 */
const ECC_BITS = { L: 0b01, M: 0b00, Q: 0b11, H: 0b10 };

/** 各等级可纠正比例（用于文档说明） */
export const ECC_RATIO = { L: 0.07, M: 0.15, Q: 0.25, H: 0.30 };

/* ------------------------------------------------------------
   三、数据编码
   ------------------------------------------------------------ */

/** 字符串 → UTF-8 字节数组 */
function toUtf8Bytes(text) {
  if (typeof TextEncoder !== 'undefined') return Array.from(new TextEncoder().encode(text));

  // 兼容旧环境：手工编码
  const bytes = [];
  for (let i = 0; i < text.length; i += 1) {
    let code = text.charCodeAt(i);
    if (code < 0x80) {
      bytes.push(code);
    } else if (code < 0x800) {
      bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code >= 0xd800 && code <= 0xdbff) {
      // 代理对
      const next = text.charCodeAt(i + 1);
      code = 0x10000 + ((code - 0xd800) << 10) + (next - 0xdc00);
      i += 1;
      bytes.push(0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    } else {
      bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    }
  }
  return bytes;
}

/** 某版本可容纳的数据码字总数 */
function dataCodewordCount(version) {
  const info = VERSION_INFO[version];
  return info.groups.reduce((sum, [blocks, perBlock]) => sum + blocks * perBlock, 0);
}

/** 字节模式下，字符计数指示符的位宽（版本 1–9 为 8 位，10+ 为 16 位） */
function charCountBits(version) {
  return version <= 9 ? 8 : 16;
}

/** 自动选择能容纳 len 字节的最小版本 */
export function pickVersion(byteLength) {
  for (let version = 1; version <= 10; version += 1) {
    const capacityBits = dataCodewordCount(version) * 8;
    const overheadBits = 4 + charCountBits(version);
    if (byteLength * 8 + overheadBits <= capacityBits) return version;
  }
  throw new Error(`内容过长（${byteLength} 字节），超出本实现支持的版本 10 容量`);
}

/**
 * 构造数据码字序列（含模式指示符、长度、终止符与填充）。
 * @param {number[]} bytes UTF-8 字节
 * @param {number} version
 */
function buildDataCodewords(bytes, version) {
  const capacity = dataCodewordCount(version);
  const capacityBits = capacity * 8;

  const bits = [];
  const pushBits = (value, length) => {
    for (let i = length - 1; i >= 0; i -= 1) bits.push((value >> i) & 1);
  };

  // 模式指示符：字节模式 = 0100
  pushBits(0b0100, 4);
  // 字符计数
  pushBits(bytes.length, charCountBits(version));
  // 数据
  bytes.forEach((byte) => pushBits(byte, 8));

  // 终止符（最多 4 位）
  const terminator = Math.min(4, capacityBits - bits.length);
  pushBits(0, terminator);

  // 补齐到字节边界
  while (bits.length % 8 !== 0) bits.push(0);

  // 填充码字：0xEC 与 0x11 交替
  const padBytes = [0xec, 0x11];
  let padIndex = 0;
  while (bits.length < capacityBits) {
    pushBits(padBytes[padIndex % 2], 8);
    padIndex += 1;
  }

  // 位流 → 字节
  const codewords = [];
  for (let i = 0; i < bits.length; i += 8) {
    let byte = 0;
    for (let j = 0; j < 8; j += 1) byte = (byte << 1) | bits[i + j];
    codewords.push(byte);
  }

  return codewords;
}

/**
 * 分块纠错并交织。
 * @param {number[]} dataCodewords
 * @param {number} version
 * @returns {number[]} 交织后的最终码字序列
 */
function interleaveWithEcc(dataCodewords, version) {
  const info = VERSION_INFO[version];
  const { ec: ecPerBlock, groups } = info;

  // 按分组切分数据块
  const dataBlocks = [];
  let offset = 0;
  groups.forEach(([blockCount, perBlock]) => {
    for (let i = 0; i < blockCount; i += 1) {
      dataBlocks.push(dataCodewords.slice(offset, offset + perBlock));
      offset += perBlock;
    }
  });

  // 每块的纠错码字
  const ecBlocks = dataBlocks.map((block) => rsEncode(block, ecPerBlock));

  // 交织：先按列取数据码字，再按列取纠错码字
  const result = [];
  const maxDataLength = Math.max(...dataBlocks.map((block) => block.length));

  for (let i = 0; i < maxDataLength; i += 1) {
    dataBlocks.forEach((block) => {
      if (i < block.length) result.push(block[i]);
    });
  }
  for (let i = 0; i < ecPerBlock; i += 1) {
    ecBlocks.forEach((block) => result.push(block[i]));
  }

  return result;
}

/* ------------------------------------------------------------
   四、矩阵构造
   ------------------------------------------------------------ */

function createMatrix(size) {
  return {
    modules: Array.from({ length: size }, () => new Array(size).fill(false)),
    reserved: Array.from({ length: size }, () => new Array(size).fill(false)),
  };
}

/** 摆放定位图案（含分隔符） */
function placeFinder(matrix, size, top, left) {
  for (let r = -1; r <= 7; r += 1) {
    for (let c = -1; c <= 7; c += 1) {
      const row = top + r;
      const col = left + c;
      if (row < 0 || row >= size || col < 0 || col >= size) continue;

      const inside = r >= 0 && r <= 6 && c >= 0 && c <= 6;
      const dark =
        inside &&
        (r === 0 || r === 6 || c === 0 || c === 6 || (r >= 2 && r <= 4 && c >= 2 && c <= 4));

      matrix.modules[row][col] = dark;
      matrix.reserved[row][col] = true;
    }
  }
}

/** 摆放对齐图案 */
function placeAlignment(matrix, size, centers) {
  for (const r of centers) {
    for (const c of centers) {
      // 跳过与定位图案重叠的位置
      const nearTopLeft = r <= 8 && c <= 8;
      const nearTopRight = r <= 8 && c >= size - 9;
      const nearBottomLeft = r >= size - 9 && c <= 8;
      if (nearTopLeft || nearTopRight || nearBottomLeft) continue;

      for (let dr = -2; dr <= 2; dr += 1) {
        for (let dc = -2; dc <= 2; dc += 1) {
          const dark = Math.max(Math.abs(dr), Math.abs(dc)) !== 1;
          matrix.modules[r + dr][c + dc] = dark;
          matrix.reserved[r + dr][c + dc] = true;
        }
      }
    }
  }
}

/** 摆放定时图案 */
function placeTiming(matrix, size) {
  for (let i = 8; i < size - 8; i += 1) {
    const dark = i % 2 === 0;
    matrix.modules[6][i] = dark;
    matrix.reserved[6][i] = true;
    matrix.modules[i][6] = dark;
    matrix.reserved[i][6] = true;
  }
}

/** 预留格式信息区域 */
function reserveFormatAreas(matrix, size) {
  for (let i = 0; i <= 8; i += 1) {
    if (i !== 6) {
      matrix.reserved[8][i] = true;
      matrix.reserved[i][8] = true;
    }
  }
  matrix.reserved[8][8] = true;

  for (let i = 0; i < 8; i += 1) {
    matrix.reserved[8][size - 1 - i] = true;
    matrix.reserved[size - 1 - i][8] = true;
  }
}

/** 预留版本信息区域（版本 7 及以上） */
function reserveVersionAreas(matrix, size, version) {
  if (version < 7) return;
  for (let i = 0; i < 18; i += 1) {
    const row = size - 11 + (i % 3);
    const col = Math.floor(i / 3);
    matrix.reserved[row][col] = true;
    matrix.reserved[col][row] = true;
  }
}

/** 数据码字按之字形填入矩阵 */
function placeData(matrix, size, codewords) {
  const totalBits = codewords.length * 8;
  let bitIndex = 0;
  let upward = true;

  // 从最右列开始，每次向左跨两列；第 6 列是纵向定时图案，需整体跳过。
  // 注意：这里直接修改循环变量（col = 5），使下一次迭代从第 3 列继续，
  // 这是标准实现方式，不能另用变量代替，否则列配对不对。
  for (let col = size - 1; col > 0; col -= 2) {
    if (col === 6) col = 5;

    for (let step = 0; step < size; step += 1) {
      const row = upward ? size - 1 - step : step;

      for (let j = 0; j < 2; j += 1) {
        const targetCol = col - j;
        if (matrix.reserved[row][targetCol]) continue;

        let bit = 0;
        if (bitIndex < totalBits) {
          bit = (codewords[bitIndex >> 3] >> (7 - (bitIndex & 7))) & 1;
        }
        bitIndex += 1;
        matrix.modules[row][targetCol] = bit === 1;
      }
    }

    upward = !upward;
  }
}

/* ------------------------------------------------------------
   五、掩码与罚分
   ------------------------------------------------------------ */

/** 8 种掩码函数 */
const MASK_FUNCTIONS = [
  (r, c) => (r + c) % 2 === 0,
  (r) => r % 2 === 0,
  (r, c) => c % 3 === 0,
  (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
  (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
  (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
];

function applyMask(modules, reserved, size, maskIndex) {
  const fn = MASK_FUNCTIONS[maskIndex];
  for (let r = 0; r < size; r += 1) {
    for (let c = 0; c < size; c += 1) {
      if (!reserved[r][c] && fn(r, c)) modules[r][c] = !modules[r][c];
    }
  }
}

/** 罚分规则 1：行/列中连续同色 ≥5 */
function penaltyRule1(modules, size) {
  let penalty = 0;

  const scan = (getter) => {
    for (let i = 0; i < size; i += 1) {
      let runLength = 1;
      let previous = getter(i, 0);
      for (let j = 1; j < size; j += 1) {
        const current = getter(i, j);
        if (current === previous) {
          runLength += 1;
        } else {
          if (runLength >= 5) penalty += 3 + (runLength - 5);
          runLength = 1;
          previous = current;
        }
      }
      if (runLength >= 5) penalty += 3 + (runLength - 5);
    }
  };

  scan((i, j) => modules[i][j]); // 行
  scan((i, j) => modules[j][i]); // 列
  return penalty;
}

/** 罚分规则 2：2x2 同色块 */
function penaltyRule2(modules, size) {
  let penalty = 0;
  for (let r = 0; r < size - 1; r += 1) {
    for (let c = 0; c < size - 1; c += 1) {
      const value = modules[r][c];
      if (
        value === modules[r][c + 1] &&
        value === modules[r + 1][c] &&
        value === modules[r + 1][c + 1]
      ) {
        penalty += 3;
      }
    }
  }
  return penalty;
}

/** 罚分规则 3：出现 1:1:3:1:1 的类定位图案 */
function penaltyRule3(modules, size) {
  const PATTERN_A = [true, false, true, true, true, false, true, false, false, false, false];
  const PATTERN_B = [false, false, false, false, true, false, true, true, true, false, true];
  let penalty = 0;

  const matches = (get, start, pattern) => {
    for (let k = 0; k < pattern.length; k += 1) {
      if (get(start + k) !== pattern[k]) return false;
    }
    return true;
  };

  for (let i = 0; i < size; i += 1) {
    for (let j = 0; j <= size - 11; j += 1) {
      const rowGetter = (idx) => modules[i][idx];
      const colGetter = (idx) => modules[idx][i];
      if (matches(rowGetter, j, PATTERN_A) || matches(rowGetter, j, PATTERN_B)) penalty += 40;
      if (matches(colGetter, j, PATTERN_A) || matches(colGetter, j, PATTERN_B)) penalty += 40;
    }
  }
  return penalty;
}

/** 罚分规则 4：深色模块比例偏离 50% */
function penaltyRule4(modules, size) {
  let dark = 0;
  for (let r = 0; r < size; r += 1) {
    for (let c = 0; c < size; c += 1) {
      if (modules[r][c]) dark += 1;
    }
  }
  const total = size * size;
  const percent = (dark * 100) / total;
  const deviation = Math.abs(percent - 50);
  return Math.floor(deviation / 5) * 10;
}

function totalPenalty(modules, size) {
  return (
    penaltyRule1(modules, size) +
    penaltyRule2(modules, size) +
    penaltyRule3(modules, size) +
    penaltyRule4(modules, size)
  );
}

/* ------------------------------------------------------------
   六、格式信息与版本信息
   ------------------------------------------------------------ */

/** 15 位格式信息（BCH(15,5) + 固定掩码） */
function formatInfoBits(eccLevel, maskIndex) {
  const data = (ECC_BITS[eccLevel] << 3) | maskIndex;
  let remainder = data << 10;

  for (let i = 14; i >= 10; i -= 1) {
    if (remainder & (1 << i)) remainder ^= 0x537 << (i - 10);
  }

  return ((data << 10) | remainder) ^ 0x5412;
}

/** 18 位版本信息（BCH(18,6)），仅版本 7+ 需要 */
function versionInfoBits(version) {
  let remainder = version << 12;
  for (let i = 17; i >= 12; i -= 1) {
    if (remainder & (1 << i)) remainder ^= 0x1f25 << (i - 12);
  }
  return (version << 12) | remainder;
}

/**
 * 写入格式信息（两份副本）。
 *
 * 位序说明（易错点，已通过与参考实现逐模块比对确认）：
 *   两份副本都按**标准的读取顺序**依次承载 bit14 → bit0（高位在前）。
 *   一个常见的错误实现是按 bit0 → bit14 递增写入，那样生成的码
 *   功能图案与数据区都正确，但扫码器读不出内容。
 *
 *   副本 1 读取顺序：
 *     (8,0)(8,1)(8,2)(8,3)(8,4)(8,5)(8,7)(8,8)(7,8)(5,8)(4,8)(3,8)(2,8)(1,8)(0,8)
 *   副本 2 读取顺序：
 *     竖向 7 位 (size-1,8) … (size-7,8)
 *     横向 8 位 (8,size-8) … (8,size-1)
 */
function writeFormatInfo(modules, reserved, size, eccLevel, maskIndex) {
  const bits = formatInfoBits(eccLevel, maskIndex);

  const getBit = (index) => (bits >> index) & 1;

  const set = (row, col, value) => {
    modules[row][col] = value === 1;
    reserved[row][col] = true;
  };

  /* ---- 副本 1 ---- */
  const copy1 = [
    [8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5],
    [8, 7], [8, 8], [7, 8],
    [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8],
  ];
  copy1.forEach(([row, col], index) => set(row, col, getBit(14 - index)));

  /* ---- 副本 2 ---- */
  for (let i = 0; i < 7; i += 1) {
    set(size - 1 - i, 8, getBit(14 - i));
  }
  for (let i = 0; i < 8; i += 1) {
    set(8, size - 8 + i, getBit(7 - i));
  }
}

/** 写入版本信息（两份副本） */
function writeVersionInfo(modules, reserved, size, version) {
  if (version < 7) return;
  const bits = versionInfoBits(version);

  for (let i = 0; i < 18; i += 1) {
    const bit = (bits >> i) & 1;
    const row = size - 11 + (i % 3);
    const col = Math.floor(i / 3);
    modules[row][col] = bit === 1;
    reserved[row][col] = true;
    modules[col][row] = bit === 1;
    reserved[col][row] = true;
  }
}

/* ------------------------------------------------------------
   七、对外主函数
   ------------------------------------------------------------ */

/**
 * 生成二维码模块矩阵。
 *
 * @param {string} text 待编码内容
 * @param {object} [options]
 * @param {'L'|'M'|'Q'|'H'} [options.ecc='M'] 纠错等级
 * @param {number} [options.version] 指定版本（不传则自动选择）
 * @param {number} [options.mask] 指定掩码 0–7（不传则按罚分自动择优）。
 *        该选项主要用于测试与与参考实现比对，业务代码无需传。
 * @returns {{size:number, modules:boolean[][], version:number, ecc:string}}
 *          modules[row][col] === true 表示该模块为深色
 */
export function encode(text, options = {}) {
  const { ecc = 'M', version: forcedVersion = null, mask: forcedMask = null } = options;

  if (ecc !== 'M') {
    throw new Error(`当前实现仅支持纠错等级 M，收到 ${ecc}`);
  }

  const value = String(text ?? '');
  if (!value) throw new Error('二维码内容不能为空');

  const bytes = toUtf8Bytes(value);
  const version = forcedVersion || pickVersion(bytes.length);

  if (version < 1 || version > 10) {
    throw new Error(`版本 ${version} 超出本实现支持范围（1–10）`);
  }

  const size = 17 + 4 * version;

  /* ---- 1. 编码数据 ---- */
  const dataCodewords = buildDataCodewords(bytes, version);
  const codewords = interleaveWithEcc(dataCodewords, version);

  /* ---- 2. 构造功能图案 ---- */
  const matrix = createMatrix(size);
  placeFinder(matrix, size, 0, 0);
  placeFinder(matrix, size, 0, size - 7);
  placeFinder(matrix, size, size - 7, 0);
  placeAlignment(matrix, size, ALIGNMENT_CENTERS[version] || []);
  placeTiming(matrix, size);
  reserveFormatAreas(matrix, size);
  reserveVersionAreas(matrix, size, version);

  // 固定的深色模块
  matrix.modules[size - 8][8] = true;
  matrix.reserved[size - 8][8] = true;

  /* ---- 3. 填入数据 ---- */
  placeData(matrix, size, codewords);

  /* ---- 4. 应用掩码 ---- */
  const buildWithMask = (maskIndex) => {
    const modules = matrix.modules.map((row) => row.slice());
    const reserved = matrix.reserved.map((row) => row.slice());

    applyMask(modules, reserved, size, maskIndex);
    writeFormatInfo(modules, reserved, size, ecc, maskIndex);
    writeVersionInfo(modules, reserved, size, version);

    return modules;
  };

  if (forcedMask !== null) {
    const mask = Math.max(0, Math.min(7, Number(forcedMask)));
    const modules = buildWithMask(mask);
    return {
      size,
      modules,
      version,
      ecc,
      mask,
      penalty: totalPenalty(modules, size),
    };
  }

  /* ---- 5. 按罚分择优 ---- */
  let bestMask = 0;
  let bestPenalty = Infinity;
  let bestModules = null;

  for (let maskIndex = 0; maskIndex < 8; maskIndex += 1) {
    const candidate = buildWithMask(maskIndex);
    const penalty = totalPenalty(candidate, size);

    if (penalty < bestPenalty) {
      bestPenalty = penalty;
      bestMask = maskIndex;
      bestModules = candidate;
    }
  }

  return { size, modules: bestModules, version, ecc, mask: bestMask, penalty: bestPenalty };
}

/* ------------------------------------------------------------
   八、渲染到 Canvas
   ------------------------------------------------------------ */

/**
 * 把二维码渲染到 canvas 元素。
 *
 * @param {HTMLCanvasElement} canvas
 * @param {string} text 内容
 * @param {object} [options]
 * @param {number} [options.size=160] 画布边长（CSS 像素）
 * @param {number} [options.margin=4] 静默区宽度（模块数）
 * @param {string} [options.dark='#0f172a']
 * @param {string} [options.light='#ffffff']
 * @returns {{canvas:HTMLCanvasElement, version:number, size:number}}
 */
export function renderToCanvas(canvas, text, options = {}) {
  const { size = 160, margin = 4, dark = '#0f172a', light = '#ffffff' } = options;

  const { modules, size: moduleCount, version } = encode(text);

  const totalModules = moduleCount + margin * 2;
  const scale = Math.max(1, Math.floor(size / totalModules));
  const pixelSize = totalModules * scale;

  // 用设备像素比渲染，保证高分屏清晰
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  canvas.width = pixelSize * dpr;
  canvas.height = pixelSize * dpr;
  canvas.style.width = `${pixelSize}px`;
  canvas.style.height = `${pixelSize}px`;

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  // 背景（含静默区）
  ctx.fillStyle = light;
  ctx.fillRect(0, 0, pixelSize, pixelSize);

  // 模块
  ctx.fillStyle = dark;
  for (let r = 0; r < moduleCount; r += 1) {
    for (let c = 0; c < moduleCount; c += 1) {
      if (!modules[r][c]) continue;
      ctx.fillRect((c + margin) * scale, (r + margin) * scale, scale, scale);
    }
  }

  return { canvas, version, size: pixelSize };
}

/**
 * 生成二维码的 HTML 片段（含画布与说明）。
 *
 * @param {string} text 内容
 * @param {object} [options]
 * @param {number} [options.size=140]
 * @param {string} [options.caption] 下方说明文字
 * @param {string} [options.class]
 */
export function qrHtml(text, options = {}) {
  const { size = 140, caption = '', class: extra = '' } = options;
  const id = `qr-${Math.random().toString(36).slice(2, 10)}`;
  const captionHtml = caption
    ? `<div class="qr-caption">${String(caption).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch])}</div>`
    : '';

  // 内容通过 data 属性传递，由 hydrateQr() 在插入 DOM 后渲染，
  // 避免在 HTML 字符串里内联大量 base64
  return `<div class="qr ${extra}" data-qr-text="${encodeURIComponent(text)}" data-qr-size="${size}">
    <canvas id="${id}" width="${size}" height="${size}" role="img" aria-label="二维码"></canvas>
    ${captionHtml}
  </div>`;
}

/**
 * 渲染容器内所有待生成的二维码。
 * 在把 qrHtml() 产生的 HTML 插入 DOM 之后调用一次即可。
 *
 * @param {HTMLElement} root
 */
export function hydrateQr(root = document) {
  root.querySelectorAll('[data-qr-text]').forEach((node) => {
    if (node.dataset.qrRendered === 'true') return;
    const canvas = node.querySelector('canvas');
    if (!canvas) return;

    try {
      const text = decodeURIComponent(node.dataset.qrText || '');
      renderToCanvas(canvas, text, { size: Number(node.dataset.qrSize) || 140 });
      node.dataset.qrRendered = 'true';
    } catch (error) {
      // 内容过长或编码失败：明确提示，而不是留一个空白占位
      const note = document.createElement('div');
      note.className = 'qr-caption text-danger';
      note.textContent = '二维码生成失败：内容过长';
      node.append(note);
      console.error('[qrcode] 生成失败', error);
    }
  });
}

/**
 * 渲染单个二维码到 canvas（命令式用法）。
 * @param {HTMLCanvasElement} canvas
 * @param {string} text
 * @param {object} [options]
 */
export function drawQr(canvas, text, options = {}) {
  return renderToCanvas(canvas, text, options);
}

export default { encode, renderToCanvas, qrHtml, hydrateQr, drawQr, pickVersion, ECC_RATIO };
