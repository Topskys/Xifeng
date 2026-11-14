//! 语音会话编排器：热键 → 采集 → IPC 推理 → 后处理 → 注入。
//!
//! 这是 `run_spike.py:ptt_loop` + `hotkey.py:record_ptt` 的等价实现（D16：
//! 壳接管采集与编排，Python 只做 ASR 推理）。时序语义与 Python 版逐条对齐：
//!
//! ```text
//! 双击触发：单击（≤300ms 即松开）→ 500ms 内再次按下并保持 → 录音开始
//! 按住期间：Esc 取消 / 其它键落下=组合键整段丢弃 / 超时 60s 截断
//! 松开：再收 drain_ms(120ms) 尾音 → 关流 → asr_end → 后处理 → 注入
//! 按住 <250ms：误触，整段丢弃（不进 ASR）
//! 不按键时麦克风完全关闭（按需开麦，G23：开流有 95–376ms 静默期）
//! ```
//!
//! 与 Python 版的结构差异（等价性不受影响）：
//! - Python 先攒 PCM 再整体送 ASR；这里边录边 `asr_feed`（流式追加到
//!   utterance 缓冲），松手后 `asr_end` 一次性裁剪+解码。端到端省一次拷贝。
//! - 采集用 cpal：按设备原生格式开流，在**喂送线程**里做下混+线性重采样
//!   到 16kHz（PortAudio 在 WASAPI 共享模式下也是系统重采样，等价）。
//!   音频回调只做「转 i16 → 拷进无锁环形缓冲」，禁止 IO/加锁（硬约束）。

use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine;
use tauri::{AppHandle, Emitter, Manager};

use crate::hotkey::{self, Hook, VK_ESCAPE};
use crate::inject::{inject_ex, snapshot_focus, InjectConfig, InjectOutcome, InjectStrategy};
use crate::py::PythonBridge;

// 时序常量（与 hotkey.py 的默认值一致）
const TAP_MS: u64 = 300; // 单击判定：按住 ≤300ms 即松开才算单击
const GAP_MS: u64 = 500; // 单击 → 第二击的间隔上限
const MIN_HOLD_MS: u64 = 250; // 误触保护：按住不足直接丢弃
const MAX_SECONDS: u64 = 60; // 单次录音上限
const DRAIN_MS: u64 = 120; // 松手后继续收流的时长（G23 尾音）
const SAMPLE_RATE: u32 = 16000; // ASR 输入采样率
const FEED_INTERVAL_MS: u64 = 40; // 喂送窗口：每窗攒 ≥40ms 音频作为一批（P2-① 节流）

// ---------------------------------------------------------------- 无锁环形缓冲

/// SPSC 环形缓冲（音频回调写、喂送线程读）。
///
/// 硬约束：音频回调里禁止 IO/加锁/推理 —— 这里只有原子游标读写。
/// 满了就丢新数据并计数（与 Python `asr_feed` 60s 上限的"丢弃新数据"同语义，
/// 到 60s 早被状态机的 MAX_SECONDS 截断了，正常到不了这里）。
struct Ring {
    cells: Box<[AtomicU16]>, // i16 的位模式
    cap: usize,
    head: AtomicUsize, // 写游标（生产者独占写）
    tail: AtomicUsize, // 读游标（消费者独占写）
    dropped: AtomicU64,
}

impl Ring {
    fn new(cap: usize) -> Self {
        Self {
            cells: (0..cap).map(|_| AtomicU16::new(0)).collect(),
            cap,
            head: AtomicUsize::new(0),
            tail: AtomicUsize::new(0),
            dropped: AtomicU64::new(0),
        }
    }

    fn push(&self, s: i16) {
        let head = self.head.load(Ordering::Relaxed);
        let tail = self.tail.load(Ordering::Acquire);
        if head - tail >= self.cap {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            return;
        }
        self.cells[head % self.cap].store(s as u16, Ordering::Relaxed);
        self.head.store(head + 1, Ordering::Release);
    }

    /// 取走至多 `max` 个样本。
    fn drain(&self, max: usize, out: &mut Vec<i16>) {
        let head = self.head.load(Ordering::Acquire);
        let mut tail = self.tail.load(Ordering::Relaxed);
        let n = (head - tail).min(max);
        for _ in 0..n {
            out.push(self.cells[tail % self.cap].load(Ordering::Relaxed) as i16);
            tail += 1;
        }
        self.tail.store(tail, Ordering::Release);
    }

    /// 累计丢弃样本数（满则丢新数据，可观测性：会话结束时报进日志）。
    fn dropped_count(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    /// 当前缓冲里的样本数（head-tail；wait_live 用它判断流是否已产出数据）。
    fn len(&self) -> usize {
        self.head.load(Ordering::Acquire) - self.tail.load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------- 采集

struct Mic {
    ring: Arc<Ring>,
    stream: Mutex<Option<cpal::Stream>>,
    /// 采集开关：begin() 后回调数据进缓冲，end() 后进"只清不喂"收尾态
    collecting: Arc<AtomicBool>,
    feeder: Mutex<Option<std::thread::JoinHandle<()>>>,
    native_rate: u32,
    native_channels: usize,
    /// 流错误计数（音频线程只自增，日志由编排线程打——回调里禁止 IO）
    stream_errors: Arc<AtomicU64>,
}

impl Mic {
    /// 按设备**原生格式**开流（重采样在喂送线程做，回调保持最薄）。
    fn start(&mut self) -> Result<(), String> {
        use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

        let device = cpal::default_host()
            .default_input_device()
            .ok_or("找不到默认麦克风设备")?;
        let cfg = device
            .default_input_config()
            .map_err(|e| format!("读麦克风默认格式失败：{e}"))?;
        self.native_rate = cfg.sample_rate().0;
        self.native_channels = cfg.channels() as usize;

        // 环按**原生采样率×声道**分配：回调存的是原生格式样本（65s 富余量）。
        // 新建流还没捕获 ring，此处重建是安全的。
        self.ring = Arc::new(Ring::new(self.native_rate as usize * self.native_channels * 65));

        let ring = Arc::clone(&self.ring);
        let stream_errors = Arc::clone(&self.stream_errors);
        let err = move |_e| {
            // 音频线程只计数；原因由编排线程读日志（回调里禁止 IO/日志）
            stream_errors.fetch_add(1, Ordering::Relaxed);
        };
        let stream = match cfg.sample_format() {
            cpal::SampleFormat::I16 => device.build_input_stream(
                &cfg.config(),
                move |d: &[i16], _| for &s in d { ring.push(s) },
                err,
                None,
            ),
            cpal::SampleFormat::F32 => device.build_input_stream(
                &cfg.config(),
                move |d: &[f32], _| {
                    for &s in d {
                        ring.push((s.clamp(-1.0, 1.0) * 32767.0) as i16);
                    }
                },
                err,
                None,
            ),
            cpal::SampleFormat::U16 => device.build_input_stream(
                &cfg.config(),
                move |d: &[u16], _| {
                    for &s in d {
                        ring.push((s as i32 - 32768) as i16);
                    }
                },
                err,
                None,
            ),
            f => return Err(format!("不支持的麦克风采样格式：{f:?}")),
        }
        .map_err(|e| format!("打开麦克风失败：{e}"))?;

        stream.play().map_err(|e| format!("启动麦克风失败：{e}"))?;
        *self.stream.lock().unwrap() = Some(stream);
        Ok(())
    }

    /// 开始本次录音：打开喂送闸门并起喂送线程（此后到达的样本都送运行时）。
    fn begin(&mut self, bridge: &Arc<PythonBridge>, app: &AppHandle) {
        self.collecting.store(true, Ordering::SeqCst);
        let handle = spawn_feeder(
            Arc::clone(&self.ring),
            Arc::clone(&self.collecting),
            self.native_rate,
            self.native_channels,
            Arc::clone(bridge),
            app.clone(),
        );
        *self.feeder.lock().unwrap() = Some(handle);
    }

    /// 结束：停止喂送，等喂送线程清空缓冲后退出（不留尾音在环形缓冲里）。
    fn end(&mut self) {
        self.collecting.store(false, Ordering::SeqCst);
        let stream = self.stream.lock().unwrap().take();
        drop(stream); // Drop 会停流并释放设备（松手就关，不占麦）
        if let Some(h) = self.feeder.lock().unwrap().take() {
            let _ = h.join();
        }
    }

    /// 流是否在运行（tap1 预热后跳过会话处的重复开流）。
    fn running(&self) -> bool {
        self.stream.lock().unwrap().is_some()
    }

    /// 只停流不动喂送（双击失败的 attempt 收尾用：立刻释放麦克风）。
    fn stop_stream(&mut self) {
        let stream = self.stream.lock().unwrap().take();
        drop(stream);
    }

    /// 等采集流产出第一批样本（G23：开流后 95-376ms 才有回调）。
    /// 超时也返回 false（调用方照常继续，只是句首可能缺）——不能卡死编排。
    fn wait_live(&self, timeout: Duration) -> bool {
        let t0 = Instant::now();
        while t0.elapsed() < timeout {
            if self.ring.len() > 0 {
                return true;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        false
    }
}

/// 逐块把设备原生格式的数据重采样为 16kHz 单声道并喂给运行时。
struct Resampler {
    /// 输出步长 = 原生采样率 / 16000（每个输出样本前进的输入帧数）。
    step: f64,
    /// 下一输出样本在「当前缓冲（含前置 prev 帧）」中的位置。
    t: f64,
    /// 上一缓冲的最后一帧（跨块插值用）。
    prev: f32,
    channels: usize,
}

impl Resampler {
    fn new(rate: u32, channels: usize) -> Self {
        Self { step: rate as f64 / SAMPLE_RATE as f64, t: 0.0, prev: 0.0, channels }
    }

    /// 处理一段交错样本（已转 i16），把重采样后的 16k 单声道追加到 `out`。
    fn feed(&mut self, interleaved: &[i16], out: &mut Vec<i16>) {
        let ch = self.channels.max(1);
        let frames = interleaved.len() / ch;
        if frames == 0 {
            return;
        }
        // 下混：多声道取平均（PortAudio 单声道流的等价行为）
        let mono: Vec<f32> = (0..frames)
            .map(|f| {
                let mut acc = 0i32;
                for c in 0..ch {
                    acc += interleaved[f * ch + c] as i32;
                }
                acc as f32 / ch as f32
            })
            .collect();

        // 虚拟数组 = [prev, mono[0..frames]]；t 从上一块遗留位置继续。
        // 只要 floor(t)+1 仍在虚拟数组内就输出一个样本。
        let n = mono.len();
        while self.t <= n as f64 {
            let j = self.t.floor() as usize; // 0..=n
            let frac = (self.t - j as f64) as f32;
            let a = if j == 0 { self.prev } else { mono[j - 1] };
            let b = if j == n { mono[n - 1] } else { mono[j] };
            let v = a + (b - a) * frac;
            out.push(v.clamp(-32767.0, 32767.0).round() as i16);
            self.t += self.step;
        }
        self.t -= n as f64;
        self.prev = mono[n - 1];
    }
}

/// 起喂送线程：环形缓冲 → 下混+重采样 → base64 → asr_feed。
/// 顺带把每批的音量（RMS 0..1）发给浮窗（`voice://level`）。
///
/// 节流（P2-①）：固定 40ms 窗口——每个窗口攒到的量作为一批送出，
/// 批大小不再随 IPC 速度漂移（旧实现攒到多少送多少，小批多调、
/// 往返开销偏高）；`voice://level` 也随之稳定为约 40ms 一发。
fn spawn_feeder(
    ring: Arc<Ring>,
    collecting: Arc<AtomicBool>,
    rate: u32,
    channels: usize,
    bridge: Arc<PythonBridge>,
    app: AppHandle,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let mut rs = Resampler::new(rate, channels);
        let mut raw: Vec<i16> = Vec::new(); // 一次读出的原生交错样本
        let mut resampled: Vec<i16> = Vec::new();
        let interval = Duration::from_millis(FEED_INTERVAL_MS);
        loop {
            let window = Instant::now();
            // 一次最多取 ~0.5s 原生数据（积压时的上限；正常一个窗口约 40ms 的量）
            raw.clear();
            ring.drain(rate as usize * channels / 2, &mut raw);
            if raw.is_empty() {
                if !collecting.load(Ordering::SeqCst) {
                    break;
                }
                std::thread::sleep(interval);
                continue;
            }
            resampled.clear();
            rs.feed(&raw, &mut resampled);
            if resampled.is_empty() {
                continue;
            }
            // 音量指示：只在录音中发。10ms 子窗 RMS 取最大——旧 popup 的
            // last_chunk 是 ~10ms 回调块，音节尖峰直接顶满；对整批 40ms 求
            // 均值会被音节空隙抹平，柱子动幅明显变小（用户实测反馈）。
            if collecting.load(Ordering::SeqCst) {
                const WIN: usize = 160; // 16k × 10ms
                let n = resampled.len();
                let mut peak = 0.0f64;
                let mut start = 0usize;
                while start < n {
                    let end = (start + WIN).min(n);
                    let acc: f64 = resampled[start..end]
                        .iter()
                        .map(|s| { let v = *s as f64; v * v })
                        .sum();
                    let r = (acc / (end - start) as f64).sqrt() / 32767.0;
                    if r > peak {
                        peak = r;
                    }
                    if end == n {
                        break;
                    }
                    start = end;
                }
                let _ = app.emit("voice://level", peak.min(1.0));
            }
            let mut bytes = Vec::with_capacity(resampled.len() * 2);
            for s in &resampled {
                bytes.extend_from_slice(&s.to_le_bytes());
            }
            let pcm_b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
            if let Err(e) = bridge.call("asr_feed", serde_json::json!({ "pcm_b64": pcm_b64 })) {
                eprintln!("[voice] asr_feed 失败：{e}");
                return; // 核没了：本次会话作废（编排层会感知到后续调用失败）
            }
            // 节流：窗口起点起算 40ms——IPC 快慢只影响批大小，不影响节奏
            let spent = window.elapsed();
            if spent < interval {
                std::thread::sleep(interval - spent);
            }
        }
    })
}

fn new_mic() -> Mic {
    Mic {
        // 占位：真实容量在 start() 里按设备原生格式重建（原生率×声道×65s）
        ring: Arc::new(Ring::new(SAMPLE_RATE as usize * 65)),
        stream: Mutex::new(None),
        collecting: Arc::new(AtomicBool::new(false)),
        feeder: Mutex::new(None),
        native_rate: SAMPLE_RATE,
        native_channels: 1,
        stream_errors: Arc::new(AtomicU64::new(0)),
    }
}

// ---------------------------------------------------------------- 编排器

/// 双击状态机（`hotkey.py:wait_for_double_tap` 的等价移植）。
/// 返回 true = 录音开始；false = 收到退出信号（当前无退出通道，恒 true）。
///
/// G23 句首保护：tap1 按下即开流——开流静默期（95-376ms）与双击 gap 重叠，
/// tap1 起的音频留在环形缓冲里，会话开始后一并喂 ASR，句首不再被割。
/// 非 double（按住太久/组合键/gap 超时）→ 立刻收流，不占麦。
fn wait_for_double_tap(hook: &Hook, vks: &[u32], mic: &mut Mic) -> bool {
    let poll = Duration::from_millis(hotkey::POLL_MS);
    loop {
        // -- 阶段 1：等 tap1 按下
        while !hook.pressed(vks) {
            let _ = hook.drain_presses(); // 等待期组合无意义，丢弃
            std::thread::sleep(poll);
        }
        // tap1 按下即开流预热（失败不阻塞：会话开始处还有兜底）
        if !mic.running() {
            if let Err(e) = mic.start() {
                eprintln!("[voice] {e}");
            }
        }
        let t_down = Instant::now();
        let mut held_long = false;
        // -- 阶段 2：tap1 按住中 —— 必须在 tap_ms 内松开才是单击
        while hook.pressed(vks) {
            let others = hook.drain_presses();
            // 组合键 → 作废（等松开由本循环完成，重开阶段 1）
            if others.iter().any(|vk| !vks.contains(vk) && *vk != VK_ESCAPE) {
                held_long = true;
            }
            if t_down.elapsed().as_millis() as u64 > TAP_MS {
                held_long = true; // 按住太久 → 不是单击
            }
            if held_long {
                mic.stop_stream(); // 已确定不是双击：立刻收流
            }
            std::thread::sleep(poll);
        }
        if held_long {
            continue;
        }
        // -- 阶段 3：gap 内等第二次按下
        let deadline = Instant::now() + Duration::from_millis(GAP_MS);
        let mut combo = false;
        while Instant::now() < deadline {
            let others = hook.drain_presses();
            if others.iter().any(|vk| !vks.contains(vk) && *vk != VK_ESCAPE) {
                combo = true; // 组合 → 作废，重来
                break;
            }
            if hook.pressed(vks) {
                return true; // tap2 按下且保持 → 录音开始（流保持运行，预热样本已在环里）
            }
            std::thread::sleep(poll);
        }
        // gap 超时/组合：attempt 失败，收流再进下一轮
        mic.stop_stream();
        let _ = combo;
    }
}

/// 一次语音会话的统计（只进日志，不含文本内容）。
struct SessionStats {
    hold_ms: u128,
    cancelled: bool,
    combo: bool,
    too_short: bool,
    truncated: bool,
}

/// 一份已解析的用户设置（热键/注入/后处理/纠错）。
///
/// 热更（P2-①）：`voice_reload_settings` 命令重新 load 写进共享
/// `Arc<Mutex<VoiceSettings>>`；编排循环在**每次会话开始**取一次——
/// 会话中段换配置没有意义，改动在下一次会话生效。
#[derive(Clone)]
pub struct VoiceSettings {
    pub vks: Vec<u32>,
    pub inject_cfg: InjectConfig,
    pub post_cfg: ime_post::PostConfig,
    pub model_correct: bool,
    /// 浮窗主题（dark | light）：会话开始经 `voice://theme` 广播给浮窗页
    pub theme: String,
}

impl VoiceSettings {
    /// 从 Python 核的 settings 解析。bridge 失败 → 全默认值
    /// （KEY_CTRL / Auto / PostConfig::default / 纠错关），与旧 run() 同语义。
    pub fn load(bridge: &PythonBridge) -> Self {
        let settings = bridge
            .call("bootstrap", serde_json::json!({}))
            .ok()
            .and_then(|v| v.get("settings").cloned());
        let key = settings
            .as_ref()
            .and_then(|s| s.get("hotkey"))
            .and_then(|v| v.as_str())
            .map(hotkey::KeyPreset::parse)
            .unwrap_or(hotkey::KEY_CTRL);
        // 配置错误显式报出来（inject.py 对未知策略是显式 Failed；壳侧设置是持久化值，
        // 静默兜底会掩盖拼错——至少 ERROR 级别喊出来。宁可功能可用，错误显眼。）
        let inject_strategy_raw = settings
            .as_ref()
            .and_then(|s| s.get("inject_strategy"))
            .and_then(|v| v.as_str())
            .unwrap_or("auto");
        if !matches!(inject_strategy_raw.trim().to_ascii_lowercase().as_str(), "unicode" | "clipboard" | "auto") {
            eprintln!("[voice] ⚠ 设置里的注入策略「{inject_strategy_raw}」不是 unicode/clipboard/auto，按 auto 处理（请检查配置）");
        }
        let inject_cfg = InjectConfig {
            strategy: settings
                .as_ref()
                .and_then(|s| s.get("inject_strategy"))
                .and_then(|v| v.as_str())
                .map(InjectStrategy::parse)
                .unwrap_or(InjectStrategy::Auto),
            restore_focus: settings
                .as_ref()
                .and_then(|s| s.get("restore_focus"))
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            clipboard_restore_ms: settings
                .as_ref()
                .and_then(|s| s.get("clipboard_restore_ms"))
                .and_then(|v| v.as_u64())
                .unwrap_or(500),
            ..InjectConfig::default()
        };
        let post_cfg = ime_post::PostConfig {
            filler_level: settings
                .as_ref()
                .and_then(|s| s.get("filler_level"))
                .and_then(|v| v.as_str())
                .map(ime_post::FillerLevel::parse)
                .unwrap_or_default(),
            final_punct: settings
                .as_ref()
                .and_then(|s| s.get("final_punct"))
                .and_then(|v| v.as_str())
                .map(ime_post::FinalPunct::parse)
                .unwrap_or_default(),
            space_between_cjk_latin: settings
                .as_ref()
                .and_then(|s| s.get("space_between_cjk_latin"))
                .and_then(|v| v.as_bool())
                .unwrap_or(true),
            ..ime_post::PostConfig::default()
        };
        // 本地 LLM 纠错开关（G32 守卫在 Python correction.check 内，applied=false 即拒绝）
        let model_correct = settings
            .as_ref()
            .and_then(|s| s.get("model_correct"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        // 浮窗主题（与主界面同一设置项；非法值显式喊出来并兜底 dark）
        let theme_raw = settings
            .as_ref()
            .and_then(|s| s.get("theme"))
            .and_then(|v| v.as_str())
            .unwrap_or("dark")
            .trim()
            .to_ascii_lowercase();
        let theme = if theme_raw == "light" {
            "light".to_string()
        } else {
            if theme_raw != "dark" {
                eprintln!("[voice] ⚠ 设置里的主题「{theme_raw}」不是 dark/light，按 dark 处理");
            }
            "dark".to_string()
        };
        Self { vks: key.vks().to_vec(), inject_cfg, post_cfg, model_correct, theme }
    }
}

/// 编排主循环：`ptt_loop(while infinite)` 的等价实现。
pub fn run(
    bridge: Arc<PythonBridge>,
    hook: Arc<Hook>,
    app: AppHandle,
    cfg: Arc<Mutex<VoiceSettings>>,
) {
    {
        let mut slot = cfg.lock().unwrap();
        *slot = VoiceSettings::load(&bridge); // 首次装载（命令热更前这里也可能被调）
        let s = &*slot;
        eprintln!(
            "[voice] 双击热键后保持长按说话，松开输入；Esc 取消；按需开麦 \
             （热键按键={:?} 注入策略={:?} 填充词={:?} 纠错={}）",
            s.vks,
            s.inject_cfg.strategy,
            s.post_cfg.filler_level,
            if s.model_correct { "开" } else { "关" }
        );
    }
    if !hook.active() {
        eprintln!("[voice] ⚠ 键盘钩子未就绪，按键采集不可用");
        return;
    }
    let mut mic = new_mic();
    loop {
        // 每次会话开始取一次设置：热更在会话边界生效
        let (vks, inject_cfg, post_cfg, model_correct, theme) = {
            let s = cfg.lock().unwrap();
            (s.vks.clone(), s.inject_cfg.clone(), s.post_cfg.clone(), s.model_correct, s.theme.clone())
        };
        if !wait_for_double_tap(&hook, &vks, &mut mic) {
            mic.stop_stream(); // 编排收尾：不留常驻流
            return;
        }
        // G23：tap1 已开流预热，这里等第一个回调到达（麦克风真正"活"了）再弹
        // 浮窗——浮窗出现 == 可以开口，与旧版「准备提示消失后再说话」同语义。
        // 预热未及（快速双击）时最多等 500ms，超时照常弹。
        mic.wait_live(Duration::from_millis(500));
        // 浮窗弹出不抢焦点（WS_EX_NOACTIVATE，见 main.rs），不污染焦点快照
        if let Some(w) = app.get_webview_window("overlay") {
            let _ = app.emit("voice://theme", theme.as_str());
            let _ = w.show();
        }
        if let Err(e) = bridge.call("asr_begin", serde_json::json!({})) {
            eprintln!("[voice] asr_begin 失败：{e}");
            if let Some(w) = app.get_webview_window("overlay") {
                let _ = w.hide();
            }
            mic.stop_stream();
            std::thread::sleep(Duration::from_secs(1)); // 核暂时不可用：缓一拍再等按键
            continue;
        }

        // Python 版先取焦点快照再录音（注入前校验"还是不是同一个窗口"）
        let snap = snapshot_focus();

        // tap1 预热失败的兜底：会话开始处补开流（正常路径流已在跑，跳过）
        if !mic.running() {
            if let Err(e) = mic.start() {
                eprintln!("[voice] {e}");
                if let Some(w) = app.get_webview_window("overlay") {
                    let _ = w.hide();
                }
                let _ = bridge.call("asr_cancel", serde_json::json!({}));
                std::thread::sleep(Duration::from_millis(500));
                continue;
            }
        }
        let _ = hook.drain_presses(); // combo_base（会话开始前的新按键不算组合）
        mic.begin(&bridge, &app);
        let t0 = Instant::now();

        // 录音循环：Esc 取消 > 组合键 > 松开 > 超时（与 record_ptt 同序）
        let mut stats = SessionStats {
            hold_ms: 0,
            cancelled: false,
            combo: false,
            too_short: false,
            truncated: false,
        };
        loop {
            if hook.pressed(&[VK_ESCAPE]) {
                stats.cancelled = true;
                break;
            }
            let others: Vec<u32> = hook
                .drain_presses()
                .into_iter()
                .filter(|vk| !vks.contains(vk) && *vk != VK_ESCAPE)
                .collect();
            if !others.is_empty() {
                stats.combo = true; // Ctrl+其它键：整段丢弃
                break;
            }
            if !hook.pressed(&vks) {
                break; // 松开
            }
            if t0.elapsed().as_secs() >= MAX_SECONDS {
                stats.truncated = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(hotkey::POLL_MS));
        }
        stats.hold_ms = t0.elapsed().as_millis();

        // 取消时立刻收工；正常结束多收 drain_ms，把宿主缓冲里的尾音捞回来（G23）
        if !stats.cancelled {
            std::thread::sleep(Duration::from_millis(DRAIN_MS));
        }
        mic.end();
        let dropped = mic.ring.dropped_count();
        let stream_errs = mic.stream_errors.load(Ordering::Relaxed);
        if dropped > 0 || stream_errs > 0 {
            eprintln!("[voice] ⚠ 采集异常：环形缓冲丢弃 {dropped} 样本，音频流错误 {stream_errs} 次");
        }
        // 浮窗收起（取消/组合/误触/正常结束统一走这里）
        if let Some(w) = app.get_webview_window("overlay") {
            let _ = w.hide();
        }

        // 误触保护（取消优先：取消与误触互斥，Python 侧同一约定）
        stats.too_short = !stats.cancelled && stats.hold_ms < MIN_HOLD_MS as u128;

        if stats.cancelled {
            eprintln!("[voice] 已取消（Esc），本次不注入");
            let _ = bridge.call("asr_cancel", serde_json::json!({}));
            continue;
        }
        if stats.combo {
            eprintln!("[voice] 检测到组合键（Ctrl+其它键），已忽略本次");
            let _ = bridge.call("asr_cancel", serde_json::json!({}));
            continue;
        }
        if stats.too_short {
            eprintln!("[voice] 按住 {}ms < {MIN_HOLD_MS}ms，判定误触，丢弃", stats.hold_ms);
            let _ = bridge.call("asr_cancel", serde_json::json!({}));
            continue;
        }
        if stats.truncated {
            eprintln!("[voice] 超过 {MAX_SECONDS}s 上限，已截断");
        }

        // 松手 → 裁剪 + 解码（原始文本，壳负责后处理）
        let t_release = Instant::now();
        let (raw, asr_ms) = match bridge.call("asr_end", serde_json::json!({})) {
            Ok(v) => (
                v.get("text")
                    .and_then(|t| t.as_str())
                    .unwrap_or("")
                    .to_string(),
                v.get("ms").and_then(|m| m.as_u64()).unwrap_or(0),
            ),
            Err(e) => {
                eprintln!("[voice] asr_end 失败：{e}");
                continue;
            }
        };
        let t_asr_end = t_release.elapsed();
        if raw.is_empty() {
            eprintln!(
                "[voice] 空结果不注入：hold={}ms 松手→asr_end={}ms",
                stats.hold_ms, t_asr_end.as_millis()
            );
            continue; // 空结果不注入（硬约束）
        }

        // 后处理：幻觉/复读/填充词清理/归一（ime-post，与 Python postprocess 等价）
        let t_post0 = Instant::now();
        let mut cleaned = match ime_post::process(&raw, &post_cfg) {
            Some(c) => c,
            None => {
                eprintln!("[voice] 后处理判为不注入（空/幻觉/纯语气词），len={}", raw.chars().count());
                continue;
            }
        };
        let t_post = t_post0.elapsed();

        // 本地 LLM 纠错（可选，+0.5–1.5s；G32 守卫在 Python correction.check 内，
        // applied=false 即拒绝——壳只在 applied=true 时采用修正文本）
        let cleaned_stage = cleaned.clone(); // 三级对照的中间级（纠错前）
        let mut fixes: u64 = 0;
        let mut ms_correct = Duration::ZERO;
        if model_correct {
            let t_c0 = Instant::now();
            match bridge.call("correct", serde_json::json!({ "text": &cleaned })) {
                Ok(v) => {
                    if v.get("applied").and_then(|a| a.as_bool()).unwrap_or(false) {
                        let fixed = v.get("text").and_then(|t| t.as_str()).unwrap_or("");
                        fixes = v.get("fixes").and_then(|f| f.as_u64()).unwrap_or(0);
                        // 与 Python pipeline 同序：修正文本重新 normalize + final_check
                        match ime_post::final_check(&ime_post::normalize(fixed, &post_cfg)) {
                            Some(c) => cleaned = c,
                            None => {
                                eprintln!("[voice] 纠错后文本为空，不注入（len={})", raw.chars().count());
                                continue;
                            }
                        }
                    }
                }
                Err(e) => eprintln!("[voice] ⚠ correct 调用失败，按原文注入：{e}"),
            }
            ms_correct = t_c0.elapsed();
        }

        // 注入（策略/焦点恢复/剪贴板恢复时延来自用户设置，settings 边界已解析）
        let t_inj0 = Instant::now();
        let res = inject_ex(&cleaned, snap.as_ref(), &inject_cfg);
        let t_inj = t_inj0.elapsed();
        // G7 反馈链：提权（管理员）目标 SendInput 无效且必须显式失败——
        // 光落日志用户看不到「为什么没打上字」，发事件给前端弹提示。
        if res.outcome == InjectOutcome::ElevatedTarget {
            let _ = app.emit("voice://elevated", ());
        }
        let injected = res.outcome == InjectOutcome::Injected;
        eprintln!(
            "[voice] 会话完成：hold={}ms 松手→asr_end={}ms 后处理={}ms 纠错={}ms 注入={}ms \
             松手→完成={}ms chars={} fixes={} outcome={} strategy={}",
            stats.hold_ms,
            t_asr_end.as_millis(),
            t_post.as_millis(),
            ms_correct.as_millis(),
            t_inj.as_millis(),
            t_release.elapsed().as_millis(),
            cleaned.chars().count(),
            fixes,
            res.outcome.as_str(),
            res.strategy
        );

        // 记账：写一条历史（store_text 开关在 Python add_history 内生效；
        // 记账失败只落 id=-1，绝不影响已完成的注入）
        let _ = bridge.call(
            "session_record",
            serde_json::json!({
                "outcome": if injected { "Injected" } else { "Failed" },
                "chars": cleaned.chars().count(),
                "e2e_ms": t_release.elapsed().as_millis() as u64,
                "ms_asr": asr_ms,
                "ms_post": (t_post + ms_correct).as_millis() as u64,
                "ms_inject": t_inj.as_millis() as u64,
                "speech_ms": stats.hold_ms as u64,
                "strategy": res.strategy,
                "fixes": fixes,
                "error": if injected { "" } else { res.outcome.as_str() },
                "text": &cleaned,
                "raw_text": &raw,
                "clean_text": &cleaned_stage,
            }),
        );
    }
}

// ---------------------------------------------------------------- 测试注入（e2e 用）

/// 给真实钩子喂一个合成的 Ctrl 按下/松开（SendInput，不带 MAGIC）。
/// 只用于沙箱 e2e：事件走真实的 WH_KEYBOARD_LL 链路，验得完整。
/// 门控与 main.rs voice_test 一致：debug / `--features e2e` 才编译。
#[cfg(all(windows, any(debug_assertions, feature = "e2e")))]
pub fn send_ctrl(down: bool) {
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP,
        VK_CONTROL,
    };
    let inp = INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: windows::Win32::UI::Input::KeyboardAndMouse::INPUT_0 {
            ki: KEYBDINPUT {
                wVk: VK_CONTROL,
                wScan: 0,
                dwFlags: if down { KEYBD_EVENT_FLAGS(0) } else { KEYEVENTF_KEYUP },
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let sent = unsafe { SendInput([inp].as_slice(), std::mem::size_of::<INPUT>() as i32) };
    if sent != 1 {
        eprintln!("[voice] 合成按键发送失败");
    }
}

/// 注入 smoke：把文本打到当前焦点窗口（e2e 用，不碰真实识别链路）。
#[cfg(all(windows, any(debug_assertions, feature = "e2e")))]
pub fn test_inject(text: &str) -> crate::inject::InjectResult {
    crate::inject::inject_ex(text, None, &crate::inject::InjectConfig::default())
}

#[cfg(not(all(windows, any(debug_assertions, feature = "e2e"))))]
pub fn test_inject(_text: &str) -> crate::inject::InjectResult {
    crate::inject::InjectResult {
        outcome: crate::inject::InjectOutcome::Failed,
        strategy: "none",
    }
}
