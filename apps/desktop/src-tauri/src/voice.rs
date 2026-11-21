//! 语音会话编排器：热键 → 采集 → IPC 推理 → 后处理 → 注入。
//!
//! 这是 `run_spike.py:ptt_loop` + `hotkey.py:record_ptt` 的等价实现（D16：
//! 壳接管采集与编排，Python 只做 ASR 推理）。时序语义与 Python 版逐条对齐：
//!
//! ```text
//! 单击触发：按下热键即开流（150ms 观察窗后弹浮窗）→ 保持按住说话 → 松开结束
//! 观察窗：按下后 150ms 内出现其它键（Ctrl+C 等）= 组合键，静默丢弃不触发（G40）
//! 热键左右互斥：设置右 Ctrl 时左 Ctrl 不触发（KeyPreset）
//! 按住期间：Esc 取消 / 其它键落下=组合键整段丢弃 / 超时 60s 截断
//! 松开：再收 drain_ms(120ms) 尾音 → 关流 → asr_end → 后处理 → 注入
//! 按住 <250ms：误触，整段丢弃（不进 ASR）
//! 不按键时麦克风完全关闭（按需开麦；cpal 实测 play→首回调 ~20ms）。
//! 设置「常驻麦克风」（resident_mic，默认关，D24）开启后才跨会话保留流。
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
const MIN_HOLD_MS: u64 = 250; // 误触保护：按住不足直接丢弃
const MAX_SECONDS: u64 = 60; // 单次录音上限
const DRAIN_MS: u64 = 120; // 松手后继续收流的时长（G23 尾音）
const COMBO_GUARD_MS: u64 = 150; // 组合键观察窗：按下后这段时间里有别的键 = 组合，不触发（G40）
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

    /// 常驻会话边界：丢干净置期积压的样本，只保留「按下之后」到达的声音。
    /// 下一次会话不把闲置期间的旧环境声送进 ASR。
    /// 只在喂送线程不在跑时调用（它是唯一的消费者）。
    ///
    /// 不动 `dropped`：会话结束时要先把它读进日志，计数在 `arm()` 归零
    /// （按会话统计）。在这里归零会让常驻模式的溢出告警永远是 0。
    fn clear(&self) {
        let head = self.head.load(Ordering::Acquire);
        self.tail.store(head, Ordering::Release);
    }

    /// 会话开始：丢弃计数归零，日志里的「环形缓冲丢弃 N 样本」只算本次会话。
    fn reset_dropped(&self) {
        self.dropped.store(0, Ordering::Relaxed);
    }

    /// 累计丢弃样本数（满则丢新数据，可观测性：会话结束时报进日志）。
    fn dropped_count(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

// ---------------------------------------------------------------- 采集

struct Mic {
    ring: Arc<Ring>,
    stream: Mutex<Option<cpal::Stream>>,
    /// 采集开关：begin() 后回调数据进缓冲，end() 后进"只清不喂"收尾态
    collecting: Arc<AtomicBool>,
    /// 回调写入闸门：按下热键才置真。常驻模式下流一直开着，靠它把闲置期的
    /// 环境声挡在环形缓冲外（不写=不占容量、不会把旧音频喂进下一次识别）。
    /// 原子读，不违反「回调里禁止 IO/加锁」。
    capturing: Arc<AtomicBool>,
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
        let capturing = Arc::clone(&self.capturing);
        let stream_errors = Arc::clone(&self.stream_errors);
        let err = move |_e| {
            // 音频线程只计数；原因由编排线程读日志（回调里禁止 IO/日志）
            stream_errors.fetch_add(1, Ordering::Relaxed);
        };
        // 闸门：常驻模式下流一直开着，闲置期不往环形缓冲写（见 `capturing`）。
        // 按需模式下流本来就只在这次会话期间存在，闸门按下时已置真。
        let stream = match cfg.sample_format() {
            cpal::SampleFormat::I16 => device.build_input_stream(
                &cfg.config(),
                move |d: &[i16], _| {
                    if !capturing.load(Ordering::Relaxed) {
                        return;
                    }
                    for &s in d {
                        ring.push(s)
                    }
                },
                err,
                None,
            ),
            cpal::SampleFormat::F32 => device.build_input_stream(
                &cfg.config(),
                move |d: &[f32], _| {
                    if !capturing.load(Ordering::Relaxed) {
                        return;
                    }
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
                    if !capturing.load(Ordering::Relaxed) {
                        return;
                    }
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
    /// 写入闸门（capturing）在按下热键时就由 `arm()` 打开——观察窗内到达的
    /// 音频已在缓冲里，这里不削句首（G40/G42 口径不变）。
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

    /// 按下热键：清空缓冲并打开写入闸门。
    /// 本次会话只听到「按下之后」到达的声音：观察窗内的音频留在缓冲里
    /// （不削句首，G40/G42 口径不变），常驻模式下闲置期的旧音频被丢掉。
    /// 只在喂送线程不在跑时调用（它是唯一的消费者）。
    fn arm(&mut self) {
        self.ring.clear();
        self.ring.reset_dropped(); // 丢弃计数按会话计（日志在会话结束时读它）
        self.capturing.store(true, Ordering::SeqCst);
    }

    /// 松手/作废：关闭写入闸门（常驻模式下流继续开着，但不再往缓冲写）。
    fn disarm(&mut self) {
        self.capturing.store(false, Ordering::SeqCst);
    }

    /// 结束本次喂送：关闸门，等喂送线程清空缓冲后退出；
    /// 是否释放设备由调用方按模式决定。
    fn end_feed(&mut self) {
        self.disarm();
        self.collecting.store(false, Ordering::SeqCst);
        if let Some(h) = self.feeder.lock().unwrap().take() {
            let _ = h.join();
        }
    }

    /// 按需模式收尾：停止喂送并释放设备（松手就关，不占麦）。
    fn end(&mut self) {
        self.end_feed();
        self.stop_stream();
    }

    /// 常驻模式收尾：保留流（指示灯常亮、下次按下零开流延迟），
    /// 丢掉 drain 之后可能残留的样本，下一次会话从 arm() 之后的新音频开始。
    fn end_resident(&mut self) {
        self.end_feed();
        self.ring.clear();
    }

    /// 流是否在运行（按下即开流后跳过会话处的重复开流）。
    fn running(&self) -> bool {
        self.stream.lock().unwrap().is_some()
    }

    /// 只停流不动喂送（asr_begin 失败等收尾用：立刻释放麦克风）。
    fn stop_stream(&mut self) {
        let stream = self.stream.lock().unwrap().take();
        drop(stream);
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
        capturing: Arc::new(AtomicBool::new(false)),
        feeder: Mutex::new(None),
        native_rate: SAMPLE_RATE,
        native_channels: 1,
        stream_errors: Arc::new(AtomicU64::new(0)),
    }
}

// ---------------------------------------------------------------- 编排器

/// 按住即录（单击触发）：等热键按下 → **开流与弹窗并行** → 过组合键观察窗。
///
/// 返回 `Some(按下时刻)` = 进入会话；`None` = 组合键或观察窗内松手，静默丢弃。
///
/// 并行（G42）：cpal 的 `Stream` 是 `!Send`（不能跨线程建流），所以并行的是
/// **弹窗那一路**——按下时起一个线程，等满 `COMBO_GUARD_MS` 且确认不是组合键就
/// `show()`，完全不等 WASAPI 建流（冷态 450–800ms，见 G41）。
/// 主线程同时 `mic.start()`；观察窗里到达的音频留在环形缓冲，`run()` 开始喂送时
/// 一并送进 ASR——观察窗本身**不丢句首**。
///
/// 代价（如实记账）：冷态下浮窗比"麦克风真正开始采集"早 300–650ms。所以浮窗出现
/// ≠ 已经在采，用户若立刻开口，开头那一小段仍会丢；要彻底消除只能让流常驻
/// （resident_mic 可选项，默认关，D24）。
///
/// 组合键（G40）：按下后 `COMBO_GUARD_MS` 内出现任何其它键（含按下前就按住的
/// Shift/Alt 等，也含窗口内按下又松开的极短敲击）→ 判定为组合键，静默丢弃
/// （不开会话、不弹浮窗、不碰 ASR），等热键松开后重新等下一次按下。设成 150ms
/// 是因为打字时 `Ctrl+C` 的 C 几乎与 Ctrl 同时落下，而人开口前的正常停顿远大于 150ms。
fn wait_for_press(
    hook: &Arc<Hook>,
    vks: &[u32],
    mic: &mut Mic,
    app: &AppHandle,
    theme: &str,
    resident_mic: bool,
) -> Option<Instant> {
    let poll = Duration::from_millis(hotkey::POLL_MS);
    let guard = Duration::from_millis(COMBO_GUARD_MS);
    loop {
        while !hook.pressed(vks) {
            let _ = hook.drain_presses(); // 等待期组合无意义，丢弃
            std::thread::sleep(poll);
        }
        let t_down = Instant::now();

        // 按下瞬间：清空缓冲并打开写入闸门。本次会话从按下之后的新音频开始；
        // 观察窗（150ms）里到达的音频留在缓冲，`run()` 开始喂送时一并送进
        // ASR——G42「观察窗不丢句首」口径不变。常驻模式下闲置期的旧音频
        // 在闸门关闭时根本没进缓冲，这里再清一次是双保险。
        mic.arm();

        // 弹窗线程：只等观察窗，不等开流。与下面的 mic.start() 真并行。
        // 返回 (是否已弹窗, 是否判定为组合键)——`drain_presses` 是取走语义，
        // 它一旦取走，主线程就看不到那些敲击了，所以判定结果必须回传。
        let popup = {
            let hook = Arc::clone(hook);
            let vks = vks.to_vec();
            let app = app.clone();
            let theme = theme.to_string();
            std::thread::spawn(move || -> (bool, bool) {
                while Instant::now() < t_down + guard {
                    std::thread::sleep(poll);
                }
                // 组合键两条通道都看：已按住的其它键 + 窗口内按下又松开的极短敲击
                let combo = !hook.down_except(&vks).is_empty()
                    || hook
                        .drain_presses()
                        .iter()
                        .any(|vk| !vks.contains(vk) && *vk != VK_ESCAPE);
                if combo || !hook.pressed(&vks) {
                    return (false, combo); // 组合键 / 观察窗内就松手：不弹
                }
                if let Some(w) = app.get_webview_window("overlay") {
                    let _ = app.emit("voice://theme", theme.as_str());
                    let _ = w.show();
                    return (true, false);
                }
                (false, false) // 窗口还没建好：交给 run() 兜底
            })
        };

        if !mic.running() {
            if let Err(e) = mic.start() {
                eprintln!("[voice] {e}");
            }
        }
        //   down_except：当前仍按住的其它键（Shift 可能早于 Ctrl 按下）
        //   drain_presses：窗口内按下又松开的其它键（极短敲击不会留在 down 里）
        // 注意先判一次再进循环：`mic.start()` 可能要上百毫秒（设备重开），
        // 若等它返回才开始计时，观察窗会被整段跳过（实测 Shift+Ctrl 漏判）。
        let mut combo = false;
        loop {
            let pressed_other = hook
                .drain_presses()
                .iter()
                .any(|vk| !vks.contains(vk) && *vk != VK_ESCAPE);
            if pressed_other || !hook.down_except(vks).is_empty() {
                combo = true;
                break;
            }
            if t_down.elapsed() >= guard {
                break;
            }
            std::thread::sleep(poll);
        }
        let (popup_shown, popup_combo) = popup.join().unwrap_or((false, true));
        if !combo && !popup_combo && hook.pressed(vks) {
            return Some(t_down);
        }
        // 组合键 / 观察窗内就松手（误触）：静默作废，等热键松开再重来
        while hook.pressed(vks) {
            std::thread::sleep(poll);
        }
        mic.disarm(); // 不进入会话：关掉写入闸门（常驻模式下闲置期不攒音频）
        if !resident_mic {
            mic.stop_stream(); // 按需模式：不进入会话就不占麦；常驻模式保留已打开的流
        }
        // 弹窗线程可能已经弹了（组合键是它之后才被主线程发现的）：收回去
        if popup_shown {
            if let Some(w) = app.get_webview_window("overlay") {
                let _ = w.hide();
            }
        }
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
    /// 常驻麦克风：用户显式开启才跨会话保留设备流；默认 false。
    pub resident_mic: bool,
    /// 浮窗主题（dark | light）：会话开始经 `voice://theme` 广播给浮窗页
    pub theme: String,
}

impl VoiceSettings {
    /// 从 Python 核的 settings 解析。bridge 失败 → 全默认值
    /// （KEY_RCTRL / Auto / PostConfig::default / 纠错关），与旧 run() 同语义。
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
            .unwrap_or(hotkey::KEY_RCTRL);
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
        // 常驻麦克风（默认关）：开着才跨会话保留设备流。
        // 缺失/非法值一律按 false（隐私优先：升级后不会突然常亮指示灯）。
        let resident_mic = settings
            .as_ref()
            .and_then(|s| s.get("resident_mic"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        Self { vks: key.vks().to_vec(), inject_cfg, post_cfg, model_correct, resident_mic, theme }
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
            "[voice] 按住热键说话，松开输入；Esc 取消；{} \
             （热键按键={:?} 注入策略={:?} 填充词={:?} 纠错={}）",
            if s.resident_mic { "常驻麦克风（指示灯常亮，按下零开流延迟）" } else { "按需开麦" },
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
    // 常驻模式：启动时就开流（把 450–800ms 的 WASAPI 建流成本付在启动，
    // 而不是每次按键）。失败不拦启动——会话开始处仍会按需补开（G23 兜底）。
    let mut resident;
    {
        let s = cfg.lock().unwrap();
        resident = s.resident_mic;
    }
    if resident {
        match mic.start() {
            Ok(()) => eprintln!("[mic] 常驻流已就绪（不按键也占用设备，指示灯常亮）"),
            Err(e) => eprintln!("[mic] ⚠ 常驻流启动失败（{e}），退回「按下才开麦」"),
        }
    }
    loop {
        // 每次会话开始取一次设置：热更在会话边界生效
        let (vks, inject_cfg, post_cfg, model_correct, theme, resident_mic) = {
            let s = cfg.lock().unwrap();
            (
                s.vks.clone(),
                s.inject_cfg.clone(),
                s.post_cfg.clone(),
                s.model_correct,
                s.theme.clone(),
                s.resident_mic,
            )
        };
        // 设置热更：从常驻切到按需 → 释放设备；从按需切到常驻 → 补开流。
        // 会话边界做，不会打断正在进行的录音。
        if resident_mic != resident {
            resident = resident_mic;
            if resident {
                if mic.running() {
                    eprintln!("[mic] 已切到常驻（流已在跑）");
                } else {
                    match mic.start() {
                        Ok(()) => eprintln!("[mic] 已切到常驻（指示灯常亮）"),
                        Err(e) => eprintln!("[mic] ⚠ 切常驻失败（{e}），本次仍按需开麦"),
                    }
                }
            } else {
                mic.stop_stream();
                eprintln!("[mic] 已切回按需开麦（设备已释放）");
            }
        }
        let Some(t_press) = wait_for_press(&hook, &vks, &mut mic, &app, &theme, resident) else {
            continue; // 组合键 / 观察窗内松手：已静默丢弃，回到等按键
        };
        // 兜底弹窗：正常路径在观察窗通过那一刻已经由弹窗线程 show 过（G42），
        // 这里覆盖"窗口当时还没建好"的极早期情况；重复 show 无副作用。
        // 浮窗不抢焦点（WS_EX_NOACTIVATE，见 main.rs），不污染稍后的焦点快照。
        if let Some(w) = app.get_webview_window("overlay") {
            let _ = app.emit("voice://theme", theme.as_str());
            let _ = w.show();
        }
        if let Err(e) = bridge.call("asr_begin", serde_json::json!({})) {
            eprintln!("[voice] asr_begin 失败：{e}");
            if let Some(w) = app.get_webview_window("overlay") {
                let _ = w.hide();
            }
            mic.disarm();
            if !resident {
                mic.stop_stream(); // 按需：立刻释放；常驻：保留流，下次按键照旧零延迟
            }
            std::thread::sleep(Duration::from_secs(1)); // 核暂时不可用：缓一拍再等按键
            continue;
        }

        // Python 版先取焦点快照再录音（注入前校验"还是不是同一个窗口"）
        let snap = snapshot_focus();

        // 开流失败的兜底：会话开始处补开一次（正常路径流已在跑，跳过）
        if !mic.running() {
            if let Err(e) = mic.start() {
                eprintln!("[voice] {e}");
                if let Some(w) = app.get_webview_window("overlay") {
                    let _ = w.hide();
                }
                mic.disarm(); // 作废本次：关写入闸门，别让残留标志跨到下一次
                let _ = bridge.call("asr_cancel", serde_json::json!({}));
                std::thread::sleep(Duration::from_millis(500));
                continue;
            }
        }
        let _ = hook.drain_presses(); // combo_base（会话开始前的新按键不算组合）
        mic.begin(&bridge, &app);
        // 按住时长从**按下瞬间**算（含 150ms 观察窗），否则误触判定会少算这段
        let t0 = t_press;

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
            // 组合键两条通道都要看：新按下（极短敲击也在队列里）+ 当前按住
            //（Ctrl+Shift+C 的 Shift 可能早于 Ctrl 按下）
            let others: Vec<u32> = hook
                .drain_presses()
                .into_iter()
                .filter(|vk| !vks.contains(vk) && *vk != VK_ESCAPE)
                .collect();
            if !others.is_empty() || !hook.down_except(&vks).is_empty() {
                stats.combo = true; // 组合键：整段丢弃
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
        if resident {
            mic.end_resident(); // 保留流：下次按下零开流延迟（代价：指示灯常亮）
        } else {
            mic.end(); // 按需：停止喂送并释放设备
        }
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

// ---------------------------------------------------------------- 测试

#[cfg(test)]
mod tests {
    use super::Ring;

    /// 常驻麦克风依赖的缓冲不变量：`clear()` 之后，闲置期写进来的旧样本
    /// 必须全部取不到，且**下一次会话**只读得到 clear 之后到达的音频。
    /// （喂送线程不在跑时调用——它是唯一消费者，与 `arm()` 的约定一致。）
    #[test]
    fn ring_clear_drops_idle_audio_and_keeps_new() {
        let ring = Ring::new(1024);
        // 上一次会话遗留 + 闲置期持续到达的环境声
        for s in 0..600i16 {
            ring.push(s);
        }
        ring.clear();

        let mut out = Vec::new();
        ring.drain(1024, &mut out);
        assert!(out.is_empty(), "clear 之后不该读到旧样本，实际 {} 个", out.len());

        // 下一次会话按下之后到达的音频
        for s in [1i16, 2, 3, 4] {
            ring.push(s);
        }
        let mut out = Vec::new();
        ring.drain(1024, &mut out);
        assert_eq!(out, vec![1, 2, 3, 4]);
    }

    /// `arm()` 的计数口径：`clear()` 只丢样本、不动丢弃计数（会话结束时要先
    /// 把它读进日志），归零是 `reset_dropped()` 的活——所以常驻模式下本次会话
    /// 的溢出告警不会被下一次会话抹掉，也不会把闲置期的计数算进来。
    #[test]
    fn ring_clear_keeps_dropped_until_reset() {
        let ring = Ring::new(4);
        for s in 0..10i16 {
            ring.push(s); // 容量 4：后 6 个被丢弃
        }
        assert!(ring.dropped_count() > 0, "满则丢新数据要留下计数");

        ring.clear();
        assert!(
            ring.dropped_count() > 0,
            "clear 不该清掉计数，否则会话结束时的溢出日志永远是 0"
        );

        ring.reset_dropped();
        assert_eq!(ring.dropped_count(), 0);
    }
}
