/* ============================================================
   终端用户端（H5 小程序模拟）启动入口
   ------------------------------------------------------------
   屏幕状态机：

     扫码 → 登录 → 配网/激活 → 激活完成 → 首页 ⇄ 对话 / 充值 / 设置

   本文件只做两件事：
     1. 按顺序 import 9 个屏幕模块——它们在被 import 时调用
        registerScreen() 自注册（因此顺序不影响结果，写成固定顺序只为可读）；
     2. 调 startApp()：挂载手机壳并按登录态进入首屏。

   外壳（手机壳、navbar、hero、tabbar、navigate、state）在 shell.js；
   每屏一个模块，各自对接真实接口。这样做的好处是：
     * 单屏改动能单独 review，不再在一个 700+ 行的文件里翻找；
     * 屏幕之间没有隐式依赖，只共享 shell 导出的 navigate / state。
   ============================================================ */

import './scan.js';
import './login.js';
import './setup_4g.js';
import './setup_wifi.js';
import './activate_done.js';
import './home.js';
import './chat.js';
import './recharge.js';
import './settings.js';

import { startApp } from './shell.js';

startApp();
