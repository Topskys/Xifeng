
## [0.2.0](https://github.com/Topskys/Xifeng/compare/v0.1.0...v0.2.0) (2026-11-18)

### Features

* **pack:** 默认装到 D:\Program Files (x86)\xifeng ([5196327](https://github.com/Topskys/Xifeng/commit/5196327ead382d6a5f1d570b71e4d5c0339ec140))
* **v0.2.0:** 配置外置 JSON + 识别语言切换 + 窗口 1280x820 ([466864d](https://github.com/Topskys/Xifeng/commit/466864dc7193e7304dc32b2f04a2b353c6e1e4e2))

## [0.1.0](https://github.com/Topskys/Xifeng/compare/d592464af368ca0baddfb9a054ef0c35cbb771cc...v0.1.0) (2026-11-17)

### Features

* **api:** 前端加传输层，Tauri 走 IPC 其余走 fetch ([b07a036](https://github.com/Topskys/Xifeng/commit/b07a0367cdab5753c8350342e8d8c34c742dad57))
* **bench:** 加测量环境自检，按档位选预算并标注可信度 ([1910c3e](https://github.com/Topskys/Xifeng/commit/1910c3e2334bf922756b26845b37d23a9f40af74))
* **ipc:** 加 stdio NDJSON 服务供 Tauri 壳拉起 ([54f0fc5](https://github.com/Topskys/Xifeng/commit/54f0fc5af086b5a889d300e687bfd19caac2fdfa))
* **ipc:** D16 P1a——Python 运行时方法 asr_begin/feed/end/cancel ([afc3e2b](https://github.com/Topskys/Xifeng/commit/afc3e2bcf6b32e86b435d182b59db39d796c13d4))
* **p2:** 纠错与统计记账接入壳链路（correct + session_record） ([a1a1848](https://github.com/Topskys/Xifeng/commit/a1a1848a0d45baf17009fa004425f3571cb819ee))
* **p2:** G7 提权反馈链 + 老自启迁移 + voice_test e2e 门控 ([db8b838](https://github.com/Topskys/Xifeng/commit/db8b83809aadcac6dd79f14d47ea264cee01274b))
* **p2:** settings 热更 + 喂送 40ms 节流 + asr_feed 往返成本单测 ([583be9f](https://github.com/Topskys/Xifeng/commit/583be9f96c21500d1d15eb4b807b24e5f6fcd8db))
* **packaging:** 安装器默认不带模型 + NSIS 装不上真根因（G38） ([2632a1f](https://github.com/Topskys/Xifeng/commit/2632a1fd750a09d03a941a69022d7ef9ad1ebe71))
* **paths:** 模型统一根 %LOCALAPPDATA%\VoiceIME\models（D15） ([6eec029](https://github.com/Topskys/Xifeng/commit/6eec02943e7fe342d72c6058953e85ecc49ca7a7))
* **post:** 后处理接入 Rust PyO3 扩展（D13） ([d592464](https://github.com/Topskys/Xifeng/commit/d592464af368ca0baddfb9a054ef0c35cbb771cc))
* **shell:** D16-P1 触发链迁入 Tauri 壳（P1b 热键/采集 + P1c 注入 + P1d 托盘/浮窗/自启） ([97ead82](https://github.com/Topskys/Xifeng/commit/97ead823dce5fb81b585e685eb57caa804ac7191))
* **tauri:** 补 capabilities 启用 IPC，加截图与 CDP 验证工具 ([d40c0f5](https://github.com/Topskys/Xifeng/commit/d40c0f5ef6ddaa40bc1bc43fd0a0da5b683fe3d6))
* **tauri:** 搭 Tauri 壳骨架 + 冻结 IPC 契约（D14） ([1df31fb](https://github.com/Topskys/Xifeng/commit/1df31fba7eca0540ff8303e33a2c1e922822d352))
* **tauri:** Python 核心打包成 sidecar 内嵌安装包 ([0de576b](https://github.com/Topskys/Xifeng/commit/0de576b10332d2e45675dbbfc3da1858d9bbdae9))
* **tauri:** Tauri 壳跑通，窗口+Python 子进程实测可用 ([3b73258](https://github.com/Topskys/Xifeng/commit/3b73258eb57e77f64225edab5245727a4dd36d39))
* **ui:** 品牌块并入顶条左段（zcode 同款布局） ([dd66db8](https://github.com/Topskys/Xifeng/commit/dd66db8c3f7f6e9cc1a58f21f0580e06f1461b80))
* **ui:** 无边框窗口 + 自定义标题栏，出 NSIS 安装包 ([a74ab5b](https://github.com/Topskys/Xifeng/commit/a74ab5b7c112cba8c7ec2548ed8b76b18391dba3))
* **ui:** zcode式全宽顶栏 + 统一圆角应用图标 + ready 超时放宽 ([05c91da](https://github.com/Topskys/Xifeng/commit/05c91dab6f84e85a1cdc971aa17723fc1d9a14d2))
* **update:** 版本号按语义比较并提示无 Release ([6ad57b9](https://github.com/Topskys/Xifeng/commit/6ad57b9e0cd105c50bcbb18b0e8b56059791d802))
* **vad:** VAD 算法搬进 Rust，release 下切分快 6.8 倍 ([05b938d](https://github.com/Topskys/Xifeng/commit/05b938d0a9258d6a82570388f665568156ab4953))
* **voice:** 触发改为单击（按住即录），浮窗随按下零等待弹出（D17） ([ecee588](https://github.com/Topskys/Xifeng/commit/ecee588b9375dedc647b079da5d6aa2b35709839))
* **voice:** 浮窗与开流并行，冷态也不再等 WASAPI 建流（G42/D20） ([9533edc](https://github.com/Topskys/Xifeng/commit/9533edc399bc696a0dc218222d7b573201164ede))
* **voice:** 热键左右互斥 + 组合键不触发（150ms 观察窗，G40） ([4c73088](https://github.com/Topskys/Xifeng/commit/4c73088971340c8a4c66a008913b2a7b5f0c3eaa))

### Bug Fixes

* **capabilities:** 浮窗加入 ACL 授权，修复电平事件被静默拒绝 ([5b83dda](https://github.com/Topskys/Xifeng/commit/5b83ddad9cd31448081c9546cf5b126f96747f0f))
* **pack:** 安装目录避让用户数据目录 ([c261f3b](https://github.com/Topskys/Xifeng/commit/c261f3bb18fa2326421a8b7c6c91e4ac202c4292))
* **packaging:** build_core 移植 G33 CRT 覆写，修复冻结态 LLM 加载崩溃 ([0f158ad](https://github.com/Topskys/Xifeng/commit/0f158ad716baf8d323e3b99d808d7dc9b0ed5ca9))
* **tauri:** 安装/便携态找不到 sidecar——python_exe/core_dir 补 onedir 候选 ([e590957](https://github.com/Topskys/Xifeng/commit/e59095758ee6598b68cd08e6921fe78559754e71))
* **tauri:** 壳拉起语音服务进程——安装版恢复热键/托盘/浮窗（D16 过渡止血） ([7946cf3](https://github.com/Topskys/Xifeng/commit/7946cf308269997e006cdaf756ad0a1c807ece76))
* **ui:** 「VAD 免按键」改名为「录音段切分」，消除误导 ([4f26ae2](https://github.com/Topskys/Xifeng/commit/4f26ae2f030d46ab6be2a05ea7d9c3a62ad3fdce))
* **ui:** 侧栏品牌块贴顶 + favicon 落盘路径修正 ([307a6c3](https://github.com/Topskys/Xifeng/commit/307a6c311253cdd5334546d837b19cef59333060))
* **vad:** 首段/末段额外留白 300ms，修复 Silero 起音延迟吃掉首字（G39） ([a95a028](https://github.com/Topskys/Xifeng/commit/a95a02806e5df74a0b54c11eb1c4c8cc49eb343f))
* **voice:** 浮窗电平改 10ms 子窗取峰值，还原旧版动幅 ([aae6dc3](https://github.com/Topskys/Xifeng/commit/aae6dc3c4d829ea1557cd186f63e007bf34fb069))
* **voice:** tap1 预开流 + 流活了才弹浮窗，修复句首被割 ([6e372af](https://github.com/Topskys/Xifeng/commit/6e372af63ff109aa2bfd3b5f35cfa1e1402a3cfb))
