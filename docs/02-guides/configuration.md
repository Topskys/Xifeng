# 配置、纠错与模型准备

本文件描述当前源码实现。设置保存在外置 JSON，不再写入 SQLite `settings` 表。

## config.json

默认位置：`%LOCALAPPDATA%\Xifeng\config.json`；配置页「配置文件 → 打开」可定位。
开发环境在 `%LOCALAPPDATA%` 不可写时回退到 `core/.data/config.json`。

除 `schema_version` 等元数据外，设置值使用字符串编码：布尔值是 `"0"` / `"1"`，
数字也写成字符串。非法值逐项回退默认值；保存采用原子替换；坏 JSON 会备份后重建。

```json
{
  "language": "zh",
  "use_itn": "1",
  "resident_mic": "0",
  "model_correct": "0",
  "correction_history_limit": "10",
  "correction_prompt": "项目名写作 Xifeng；技术术语保留英文原文。",
  "store_text": "1",
  "streaming_enabled": "1",
  "schema_version": 2
}
```

保存后，语言、ASR 线程数、模型目录和 ITN 等**构造参数**会立即重建 recognizer；
其余开关在下一次语音会话生效。纠错提示词和历史轮数无需重启。

### 常用项

| 键 | 默认 | 行为 |
|---|---|---|
| `language` | `zh` | 显式指定 zh/en/ja/ko/yue；不要使用 auto |
| `use_itn` | `1` | SenseVoice 标点与数字 ITN 打包开关，修改后重建 recognizer |
| `resident_mic` | `0` | 默认按需开麦；开启后跨会话占用麦克风以消除冷建流等待 |
| `model_correct` | `0` | 本地 Qwen 纠错默认关；开启会增加每句延迟 |
| `correction_history_limit` | `10` | 最近成功注入文本的上下文轮数，允许 0–50；0 表示不带历史 |
| `correction_prompt` | 空 | 用户自定义领域/风格要求，最长 2000 字；不新增界面编辑器 |
| `store_text` | `1` | 关闭后只记录长度与结果，不保存三级文本，也不给纠错提供历史 |
| `streaming_enabled` | `1` | **流式识别开关**：开启后每会话按 VAD 切段、逐段识别并逐段注入；默认关，下一次说话生效 |

### ITN 的取舍

ITN 默认开启是用户明确决定，不代表它在所有文本上更准确。固定五句实测中，
ITN 开会把中文数字改成阿拉伯数字，并可能改变英文混排结果；中英术语较多时可关闭。
ITN 不是独立的数字开关，它与标点处理一起由 sherpa recognizer 在构造时决定。

## 本地模型纠错

纠错模型为 Qwen2.5-0.5B-Instruct Q4_K_M GGUF，经 llama-cpp-python 在本机 CPU 推理。
模型不随安装器默认分发；缺失或未开启时不影响语音输入。

调用顺序：读取最近 N 条**成功注入且非空**的历史（旧→新，不含当前句）
→ 拼接默认规则、用户自定义提示词和不可覆盖的安全底线 → 当前句独立包装
→ 小模型生成 → 删词/续写/解释/多行/大改守卫 → 采纳或原样回退。

- 历史和当前句都只是待处理文本，不是指令；提示词不能授权删词或绕过守卫。
- 历史每条最多 200 字，只用于本地推理，不写日志、不联网。
- `store_text=0` 时不读取历史，因为数据库中本来就没有可用于上下文的文本。
- 局部 ASR 口吃允许有限折叠（例如「就交就」），普通删词仍受原守卫限制。
- 纠错是可选增强，**不承诺开启后仍满足 P95 ≤1500ms**。带 10 轮历史的真实探测曾达到约 8 秒，不能拿短句耗时代表所有场景。

## 模型准备

统一根：`%LOCALAPPDATA%\Xifeng\models\{asr,vad,llm}`。不要把模型提交进仓库。

### ASR（必需）

文件：`models\asr\sensevoice-small-int8\model.int8.onnx` 与 `tokens.txt`。
在 `core/` 中执行（PowerShell）：

```powershell
uv run python fetch_model.py --dir "$env:LOCALAPPDATA\Xifeng\models\asr\sensevoice-small-int8"
```

`fetch_model.py` 默认使用 HuggingFace 镜像，支持断点续传。也可以在「模型」页
选择已有的完整目录。模型目录不能只放压缩包或 WAV/MP3 文件。

### VAD（可选）

文件：`models\vad\silero_vad.onnx`。只有启用「录音段切分」才需要；
PTT 默认的能量静音裁剪不依赖该模型。

### 纠错 LLM（可选）

文件：`models\llm\qwen2.5-0.5b-instruct-q4_k_m.gguf`。
可手动下载到该位置；`core/fetch_llm.py` 的默认目录是旧开发布局，不能直接当作
当前统一根的 GUI 下载实现。

## 模型下载界面

**已实现基础功能，但仍在开发中。** 当前已实现：
- `models_download_start/status/cancel`：支持 HuggingFace 镜像、ModelScope、GitHub Release、本地源；后台异步下载、进度轮询、支持取消、断点续传、校验
- `models_add`：选择本地目录自动扫描、写入 config.json 的 models.list 并自动保存
- `models_find`：按目录名+文件清单在本机候选位置匹配模型目录

仍在完善：镜像映射（当前仅 hf-mirror 可用，ModelScope 暂映射到 hf-mirror）、冻结包脚本定位、页面恢复后状态恢复、专项测试与安装版冒烟通过、VAD/LLM 下载支持。

验收要求：真实 ModelScope/HuggingFace 源、正确统一根、断点续传、校验、明确失败、
进度/取消、页面恢复后状态一致、sidecar 内可运行、专项测试与安装版冒烟通过。
在此之前，安装文档以本页的手动准备步骤为准。

## 流式识别

**后端已实现，前端仅保留开关。**

后端能力（Python `AsrRuntime` + IPC）：
- `asr_stream_begin/feed/end/cancel` 四个方法完整实现
- 按 `FEED_INTERVAL_MS=40ms` 批次喂送、累积 `STREAM_EVAL_EVERY_FRAMES=4000` 帧（≈250ms）触发 VAD 切分
- VAD 切分后，对每个**已闭合**语音段复用 `prepare_pcm` + `asr.transcribe` 并逐段下发
- `segments` 在 `asr_stream_feed` 的响应里同步返回；`on_segment` 回调供进程内调试
- `asr_stream_end` 把未闭合尾音转写作为最后一片段
- `asr_stream_cancel` 丢弃缓冲并关闭会话
- 缓冲上限 60s（约 960000 帧），超出丢弃新数据并计一次警告日志

前端现状：
- 只在配置页保留 `streaming_enabled` 开关（`config.json` 的 `streaming_enabled`，默认开）
- `Streaming.tsx` 页面已删除，不提供逐字预览 UI
- `voice://stream/segment`、`voice://stream/end`、`voice://stream/cancelled` 事件照常发送，供未来扩展

**行为差异：**
- **逐段注入**：说话中途切窗口，后续段会跟着切注入目标（逐段重取焦点快照）
- **分段独立**：后处理/纠错按段独立执行，跨段上下文纠错覆盖不到
- **首段延迟**：≈ 说完停顿（VAD 尾点 240ms）+ 250ms 评估粒度 + 单段解码 ~350ms
- **默认开启**：配置默认 `"streaming_enabled": "1"`，下一次说话生效

## 流式输出边界

SenseVoice 当前走 offline recognizer，不能把一次完整 decode 包装成"逐字流式"。后续若增加中间预览，必须保持：
最终句仍经 postprocess、纠错守卫、焦点校验；不得反复向目标应用注入/删除未确认文本。