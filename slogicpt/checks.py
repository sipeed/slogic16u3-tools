"""Measurement and checks for captured logic-analyser data.

Every channel is measured from its raw edges -- no glitch filtering: a narrow
pulse on a board output is exactly what a production test has to catch, so the
judgement never decides it was "only ringing".  Frequency and duty come from
the per-period values (see measure_channel), and the structured checks (M0
rotation, SPI/I2C/UART decoding, pulse widths, burst shape) are added on top.

It works on the numpy arrays produced by waveform.extract_channels().
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .patterns import Expect, Kind
from . import deep_checks


@dataclass
class Measurement:
    """What one channel actually did."""

    channel: int
    freq_hz: float = 0.0
    duty_pct: float | None = None
    static: int | None = None          # set when fewer than 2 rising edges
    rises: int = 0
    first_rise: int | None = None      # sample index of the first kept rising edge

    @property
    def toggling(self) -> bool:
        return self.rises >= 2


def measure_channel(samples: np.ndarray, channel: int) -> Measurement:
    """Measure one channel's 1-D 0/1 array.

    **不滤任何毛刺** —— 产测的职责是把采集到的信号如实读出来。任何"把窄脉冲当振铃丢掉"的处理都可能同时丢掉真实的东西：

      * 被测板输出上的窄毛刺（焊接/器件问题）；
      * 固件产生的伪边沿（比如状态机跑飞、时钟域出问题）。

    这两类正是产测要抓的，所以判定层不替它们做决定。代价是探头振铃也会被
    计入读数（实测飞线台架上 M5 的 CH2 会从 100 kHz 读成 27 MHz）—— 那要靠
    短粗地线、直连主机口解决，而不是在判定里滤掉。
    """
    if samples.size < 2:
        return Measurement(channel, static=int(samples[0]) if samples.size else 0)

    # indices where the level changes
    idx = np.flatnonzero(samples[1:] != samples[:-1]) + 1
    if idx.size == 0:
        return Measurement(channel, static=int(samples[0]))

    level = int(samples[idx[-1]])
    kept_t = np.concatenate(([0], idx, [samples.size]))
    kept_lvl = np.concatenate(([samples[0]], samples[idx], [level]))

    # A rising edge is where the level goes 0 -> 1, so `rise_at` indexes the LOW
    # segment that PRECEDES each rise. The edge itself is at kept_t[i + 1]; using
    # kept_t[i] reports the start of the preceding low period instead, which
    # shifted every channel's first edge back by a whole low period (CH0's first
    # rise came out as sample 0 instead of 40000).
    lv = np.asarray(kept_lvl, dtype=np.int8)
    rise_at = np.flatnonzero((lv[:-1] == 0) & (lv[1:] == 1))

    m = Measurement(channel, rises=int(rise_at.size))
    if rise_at.size >= 2:
        i_first = int(rise_at[0]) + 1
        i_last = int(rise_at[-1]) + 1
        t_first = int(kept_t[i_first])
        t_last = int(kept_t[i_last])
        span_t = t_last - t_first
        m.first_rise = t_first

        # frequency from the mean rising-edge period (see _mean_period)
        rise_t = np.asarray(kept_t, dtype=np.int64)[
            np.asarray(rise_at, dtype=np.int64) + 1]
        periods = np.diff(rise_t).astype(np.float64)
        period = _mean_period(periods)

        # 占空比 = 首末上升沿之间**高电平样本占比**（原始，不取中位数）。
        # 以前取高/低段宽度的中位数 —— 那样少数的异常脉冲会被抹掉，而"少数异常
        # 脉冲"正是产测要抓的东西（板子的窄毛刺、固件的伪边沿）。改成原始占比后
        # 它一定会反映到读数上。
        if span_t > 0:
            m.duty_pct = 100.0 * float(
                np.count_nonzero(samples[t_first:t_last])) / span_t
        else:
            m.duty_pct = None
        # freq is normalised (cycles per sample); callers multiply by the rate.
        m.freq_hz = 1.0 / period if period > 0 else None
    else:
        m.static = level
    return m


def _mean_period(periods: np.ndarray) -> float:
    """所有相邻上升沿间隔的**算术平均**（不做任何离群剔除）。

    这里以前会丢掉偏离中位数 0.5x~2x 之外的周期再平均。那等于把"多出来的边沿"
    从频率里抹掉 —— 而多出来的边沿恰恰可能是被测板的窄毛刺或固件的伪边沿，
    是产测必须看见的东西。所以改成直接平均：

      * 真实的窄脉冲 -> 周期被拉短 -> 频率读数偏高 -> FAIL，看得见；
      * 探头振铃 -> 同样读数偏高 -> FAIL：先查地线和探头，再怀疑板子。

    代价：频率对毛刺敏感。实测飞线台架上 M5 的 CH2（真值 100 kHz）会读成
    27 MHz —— 该通道由窄脉冲宽度检查判、不判频率，所以不影响判定；换成同轴探头线（补上
    AFE 的 100 Ω+15 pF）振铃会明显减小。
    """
    if periods.size == 0:
        return 0.0
    return float(periods.mean())


def measure_all(channels: np.ndarray) -> list[Measurement]:
    """Measure every channel of a (n_channels, n_samples) 0/1 array."""
    return [measure_channel(channels[ch], ch)
            for ch in range(channels.shape[0])]


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    channel: int
    ok: bool                           # pass or fail -- there is no third outcome
    kind: str = ""
    measured: str = ""
    expected: str = ""
    detail: str = ""
    sub: list["Verdict"] = field(default_factory=list)
    # For verdicts that cover several channels at once -- one SPI master is SCLK,
    # MOSI, MISO and CS_N together, not four separate results, and a mode-level
    # check like the M0 rotation spans all of them. Named so the report can say which pins
    # the result is about; falls back to "CH<n>" when empty.
    channel_text: str = ""
    # The channels that text refers to, so a failed group verdict can be narrowed
    # by the per-channel results sitting alongside it. Measured case: the I2C
    # transaction failed and the summary named "CH24/CH25", which reads as two
    # suspect wires -- but CH24's own 100 kHz check had passed, so only CH25 was
    # bad. Same for an SPI master whose SCLK and CS_N measure perfectly while the
    # data lines do not.
    channels: tuple[int, ...] = ()
    # 机器可读的一句话失败原因（"no_edge" / "order" / …）。界面据此写成人话，
    # 不必去解析 detail 里那段给人看会头大的算法内部对比文本。
    reason: str = ""
    # M0 only: when the rotation is a clean permutation, the analyser inputs
    # that received another fixture channel, "D0<-CH1 D1<-CH0 ..."
    wiring: str = ""

    @property
    def mark(self) -> str:
        return "ok" if self.ok else "** FAIL **"


def _fmt_freq(f: float) -> str:
    if f >= 1e6:
        return "%.5g MHz" % (f / 1e6)
    if f >= 1e3:
        return "%.5g kHz" % (f / 1e3)
    return "%.5g Hz" % f


def check_freq(m: Measurement, exp: Expect, tol_pct: float,
               sample_rate_hz: float) -> Verdict:
    want = exp.freq_hz
    got = m.freq_hz * sample_rate_hz
    measured = _fmt_freq(got) if m.toggling else "static %s" % m.static
    expected = _fmt_freq(want) if want is not None else "static"
    if not m.toggling:
        return Verdict(m.channel, False, "freq", measured, expected,
                       "no usable edge train")
    ok = abs(got - want) <= want * tol_pct / 100.0
    return Verdict(m.channel, ok, "freq", measured, expected,
                   "%+.3f%%" % (100.0 * (got - want) / want) if want else "")


def check_static(m: Measurement, exp: Expect) -> Verdict:
    want = exp.static
    if m.toggling:
        return Verdict(m.channel, False, "static",
                       _fmt_freq(m.freq_hz), "static %s" % want,
                       "channel is toggling")
    ok = (m.static == want)
    return Verdict(m.channel, ok, "static", "static %s" % m.static,
                   "static %s" % want)


def check_duty(m: Measurement, exp: Expect, tol_pp: float) -> Verdict:
    want = exp.duty_pct
    if m.duty_pct is None:
        return Verdict(m.channel, False, "duty", "n/a", "%.2f%%" % want,
                       "no usable edge train")
    ok = abs(m.duty_pct - want) <= tol_pp
    return Verdict(m.channel, ok, "duty", "%.2f%%" % m.duty_pct,
                   "%.2f%%" % want, "%+.2f pp" % (m.duty_pct - want))


def _high_runs(x: np.ndarray, bridge: int) -> list[tuple[int, int]]:
    """(start, end) sample indices of the high runs of a 0/1 array, with low
    dips shorter than `bridge` samples closed first -- so a glitch inside a
    real high (the 1.8 V bank read at 1.5 V) does not split it in two."""
    d = np.diff(np.concatenate(([0], x.astype(np.int8), [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    runs: list[list[int]] = []
    for a, b in zip(starts.tolist(), ends.tolist()):
        if runs and a - runs[-1][1] < bridge:
            runs[-1][1] = b
        else:
            runs.append([a, b])
    return [(a, b) for a, b in runs]


def check_walking_one(channels: np.ndarray, samples_per_step: float,
                      period: int, step_s: float = 100e-6) -> list[Verdict | None]:
    """M0, channel by channel: each one must be high for exactly one step of
    the rotation, once per turn.  check_order only looks at WHEN each channel
    first rises, which an input that is broken but still couples its edges
    through passes.  Three failures are named:

      open_input  edges but no high lasting half a step.  Measured on a 16U3
                  with a broken CH13: every high was 85-90 ns although the
                  fixture held it for 100 us -- the edges leak through the
                  open input's stray capacitance, the level never does.
      short       a high lasting 1.5 steps or more: it is also high during a
                  neighbour's step, i.e. bridged to it (named when found).
      missed_turn a turn in which the channel should have gone high and did
                  not, inside the window: an intermittent contact.

    A channel with no edge at all is left to check_order (None here).
    Glitches are deliberately not judged: the 32U3's 1.8 V bank is read at
    1.5 V in M0 and glitches there on a good board."""
    n, total = channels.shape
    step = samples_per_step
    turn = period * step
    runs = [_high_runs(channels[c], max(1, int(step * 0.01))) for c in range(n)]
    # main highs: the ones at least half a step long, that is the real steps
    mains = [[(a, b) for a, b in r if b - a >= 0.5 * step] for r in runs]
    out: list[Verdict | None] = []
    for c in range(n):
        r, m = runs[c], mains[c]
        inner = [(a, b) for a, b in r if a > 0 and b < total]  # not cut by the window
        if not r or (len(r) == 1 and not inner and r[0][1] - r[0][0] >= total - 1):
            out.append(None)                                 # static: check_order's
            continue
        want = "high for 1 step (%.0f us) once per %d-step turn" % (step_s * 1e6, period)
        widest = max(b - a for a, b in (inner or r))
        if not m:
            out.append(Verdict(c, False, "walking-one",
                               "widest high %s" % _fmt_time(widest / step * step_s),
                               want, "edges come through but no level is held: "
                               "the input looks open", reason="open_input"))
            continue
        long_ = [(a, b) for a, b in m if b - a >= 1.5 * step and a > 0 and b < total]
        if long_:
            a, b = long_[0]
            # the channel(s) high at the same time over at least half a step
            mates = [d for d in range(n) if d != c and any(
                min(b, b2) - max(a, a2) >= 0.5 * step for a2, b2 in mains[d])]
            out.append(Verdict(c, False, "walking-one",
                               "high for %.1f steps" % ((b - a) / step), want,
                               "also high during another channel's step: "
                               "shorted to " + (" ".join("CH%d" % d for d in mates)
                                                or "a neighbour") + "?",
                               reason="short"))
            continue
        # a turn missed between two highs, or before the first / after the
        # last where a whole step would still have fitted in the window
        starts = [a for a, b in m]
        margin = 0.1 * step
        missed = 0
        for s0, s1 in zip(starts, starts[1:]):
            missed += max(0, round((s1 - s0) / turn) - 1)
        if starts:
            missed += int(max(0.0, starts[0] - margin) // turn)
            missed += int(max(0.0, total - step - margin - starts[-1]) // turn)
        if missed:
            out.append(Verdict(c, False, "walking-one",
                               "%d turn(s) without a high" % missed, want,
                               "the channel missed its step: intermittent contact?",
                               reason="missed_turn"))
            continue
        out.append(Verdict(c, True, "walking-one", "1 step per turn", want))
    return out


def _fmt_time(sec: float) -> str:
    return "%.0f ns" % (sec * 1e9) if sec < 10e-6 else "%.1f us" % (sec * 1e6)


def check_threshold_control(normal: np.ndarray, high: np.ndarray,
                            vth_high: float) -> Verdict:
    """The input threshold must actually move: captured again with the
    threshold above the 3.3 V logic high, every channel that was high a good
    part of the time (>= 20 %) at the normal threshold must now read low
    (< 5 % high).  A threshold DAC or comparator reference that does not
    follow its setting passes every other mode -- 3.3 V logic clears 1.6 V
    with room to spare -- and fails here.

    Not "no edge at all": measured on a 16U3, fast edges overshoot past 5 V
    and leave blips (under 1 % high on M3's SPI lines), so the share of time
    high is judged, not the edge count."""
    lo = normal.mean(axis=1)
    hi = high.mean(axis=1)
    ref = [c for c in range(min(len(lo), len(hi))) if lo[c] >= 0.20]
    stuck = [c for c in ref if hi[c] >= 0.05]
    want = "channels high >= 20 %% at the normal threshold read < 5 %% high at %.1f V" % vth_high
    if not ref:
        return Verdict(-1, False, "threshold", "no channel high >= 20 %", want,
                       "nothing to compare against", reason="threshold")
    if stuck:
        return Verdict(-1, False, "threshold",
                       " ".join("CH%d %.0f%%" % (c, hi[c] * 100) for c in stuck[:8]),
                       want, "these inputs still read high above the logic level: "
                       "their threshold does not follow the setting",
                       channels=tuple(stuck), reason="threshold")
    return Verdict(-1, True, "threshold",
                   "%d channels dropped to <= %.1f %% high" % (len(ref), hi[ref].max() * 100),
                   want, channels=tuple(ref))


def check_order(measurements: list[Measurement], samples_per_step: float,
                tol_steps: float = 0.5, period: int | None = None) -> Verdict:
    """M3: the one-hot pattern must visit CH0, CH1, ... in order.

    Every channel's first rising edge should be one step later than the previous
    channel's. A swapped pair of probe wires shows up here even though every
    channel still reads the correct 312.5 Hz.

    结论尽量做成"哪几路不对"而不是"排在第几位对不上"：后者是算法内部视角，
    操作员要的是去查哪根线。所以把**没边沿的**和**顺序不对的**通道放进
    `channels`，并用 `reason` 给出机器可读的一句话原因，界面据此写人话。

    `period` is the length of the fixture's rotation (32).  It only matters
    when fewer channels are captured: a 16-channel analyser sees CH15 fire and
    then 17 silent steps before CH0.  Defaults to the captured channel count,
    which is what the 32-channel capture always was.
    """
    n = len(measurements)
    period = max(period or n, n)
    missing = [m.channel for m in measurements if m.first_rise is None]
    seen = [(m.channel, m.first_rise) for m in measurements
            if m.first_rise is not None]
    if len(seen) < 2:
        return Verdict(-1, False, "order", "n/a", "one rising edge per channel",
                       "not enough channels with a rising edge",
                       channels=tuple(missing), reason="no_edge")
    base = min(t for _c, t in seen)
    offsets = [(c, (t - base) / samples_per_step) for c, t in seen]
    # rotate so the channel that fires first is index 0, then require the rest to
    # ascend by one step each
    offsets.sort(key=lambda cs: cs[1])
    start_ch = offsets[0][0]
    # the captured channels in firing order, each with the step it fires on
    # relative to start_ch (one step apart when period == n)
    expect = [(c, k) for k in range(period)
              for c in [(start_ch + k) % period] if c < n]
    disordered = []
    first_bad = ""
    for rank, (ch, off) in enumerate(offsets):
        want_ch, want_off = expect[rank] if rank < len(expect) else (-1, -1)
        if ch != want_ch or abs(off - want_off) > tol_steps:
            disordered.append(ch)
            if not first_bad:
                first_bad = ("rank %d: got CH%d at %.2f steps, expected CH%d at %d"
                             % (rank, ch, off, want_ch, want_off))
    offenders = tuple(sorted(set(missing) | set(disordered)))
    reason = "no_edge" if missing else "order"
    wiring = ""
    if disordered and not missing:
        gen = infer_wiring(offsets, n, period, tol_steps)
        if gen is not None:
            reason = wiring_pattern(gen, n)
            wiring = " ".join("D%d<-CH%d" % (c, g) for c, g in sorted(gen.items())
                              if c != g)
    bits = []
    if missing:
        bits.append("%d channels have no rising edge at all" % len(missing))
    if disordered:
        # 缺通道会让后面的通道"看起来"提前了，所以先把"完全没信号的"说清楚，
        # 顺序错位只作为附带说明 —— 否则操作员会去查一堆其实没问题的线。
        bits.append("order mismatch from " + first_bad)
    return Verdict(-1, not offenders, "order",
                   "%d/%d channels have edges, starts at CH%d"
                   % (len(seen), n, start_ch),
                   "all %d channels, one step apart, ascending" % n,
                   "; ".join(bits),
                   # channels 只放"完全没信号"的那几路（真正要去查的线）；
                   # 没有缺通道时才退回顺序错位的那几路。
                   channels=tuple(missing) if missing else tuple(disordered),
                   reason=reason, wiring=wiring)


def infer_wiring(offsets: list[tuple[int, float]], n: int, period: int,
                 tol_steps: float = 0.5) -> dict[int, int] | None:
    """Analyser channel -> the fixture channel it actually receives, read off
    the steps the channels first fire on (fixture CHg fires on step g of the
    rotation).  None unless every channel lands on a whole step and together
    they are exactly fixture CH0..CH<n-1> -- i.e. a pure swap of wires, which
    is the only case worth naming.  With n == period every rotation fits (the
    absolute phase is unknown): one that is a known miswiring wins, else the
    one leaving most channels in place."""
    steps = {}
    for c, off in offsets:
        k = round(off)
        if abs(off - k) > tol_steps:
            return None
        steps[c] = k
    best = None
    for r in range(period):
        gen = {c: (k + r) % period for c, k in steps.items()}
        if sorted(gen.values()) != list(range(n)):
            continue
        score = (wiring_pattern(gen, n) != "wiring",
                 sum(g == c for c, g in gen.items()))
        if best is None or score > best[0]:
            best = (score, gen)
    return best[1] if best else None


def wiring_pattern(gen: dict[int, int], n: int) -> str:
    """Name a known miswiring: 'wiring_pairs' (every adjacent pair swapped,
    the signature of a flipped two-row connector), 'wiring_groups8' (the two
    8-channel groups swapped), else 'wiring' (some wires crossed)."""
    if n >= 2 and n % 2 == 0 and all(g == c ^ 1 for c, g in gen.items()):
        return "wiring_pairs"
    if n >= 16 and n % 16 == 0 and all(g == c ^ 8 for c, g in gen.items()):
        return "wiring_groups8"
    return "wiring"


def check_channel(m: Measurement, exp: Expect, tol_pct: float, tol_pp: float,
                  sample_rate_hz: float) -> Verdict | None:
    """Run whichever check `exp.kind` calls for.

    **判定只有"通过 / 失败"两种结果，没有第三种。**

      * 这一路由某个结构化检查判（`Kind.STRUCTURED`：走线轮转、窄脉冲宽度、
        突发填充；`Kind.PROTOCOL`：SPI/I2C/UART 解码）-> 返回 None，即**本路
        不单独出判定**，它的数据在那条结构化判定里被判；
      * 采集参数配不上这一路（`Kind.UNMEASURABLE`：超奈奎斯特、窗口太短）->
        **失败**，`kind="unjudged"`，逼人去修档案，而不是让它悄悄放行。
    """
    if exp.kind in (Kind.STRUCTURED, Kind.PROTOCOL):
        return None
    if exp.kind is Kind.UNMEASURABLE:
        got = _fmt_freq(m.freq_hz * sample_rate_hz) if m.toggling \
            else "static %s" % m.static
        want = _fmt_freq(exp.freq_hz) if exp.freq_hz else ""
        return Verdict(m.channel, False, "unjudged", got, want,
                       "测试点未被判定：%s" % exp.note,
                       reason="unjudged")
    if exp.kind is Kind.STATIC:
        return check_static(m, exp)
    if exp.kind is Kind.DUTY:
        fv = check_freq(m, exp, tol_pct, sample_rate_hz)
        dv = check_duty(m, exp, tol_pp)
        return Verdict(m.channel, fv.ok and dv.ok, "duty",
                       "%s / %s" % (fv.measured, dv.measured),
                       "%s / %s" % (fv.expected, dv.expected),
                       "%s; %s" % (fv.detail, dv.detail), sub=[fv, dv])
    return check_freq(m, exp, tol_pct, sample_rate_hz)


# ---------------------------------------------------------------------------
# deep content checks (M1..M4 protocols, M5 narrow pulses, M6 burst)
# ---------------------------------------------------------------------------


def _spi_bank_verdicts(channels: np.ndarray, sample_rate_hz: float,
                       mode: int) -> list[Verdict]:
    """M1..M4: decode every SPI master and compare against the known words.

    This is the whole point of the redesign. The old fixture judged a channel by
    its edge rate, which a floating input can imitate and a marginal edge can
    corrupt into a plausible-looking number. A decoded word is either the word
    that was sent or it is not, so the check cannot be fooled.

    Verdicts are per instance rather than per channel, because SCLK, MOSI, MISO
    and CS_N are one measurement, not four.
    """
    from . import patterns as P

    n_inst, div_sel = P.SPI_BANK_MODES[mode]
    r = deep_checks.decode_spi_bank(channels, n_inst, div_sel=div_sel,
                                    sample_rate_hz=sample_rate_hz)

    out: list[Verdict] = []
    for inst in r.instances:
        c = 4 * inst.instance
        got = ("MOSI %s | MISO %s"
               % (" ".join("%04X" % w for w in inst.mosi_words[:2]) or "-",
                  " ".join("%04X" % w for w in inst.miso_words[:2]) or "-"))
        want = ("MOSI 0x%04X | MISO 0x%04X on every complete word"
                % (inst.expect_mosi, inst.expect_miso))
        detail = ("SCLK %.4g MHz on CH%d; %d complete MOSI / %d complete MISO "
                  "words checked (first and last are capture fragments)"
                  % (inst.sclk_hz / 1e6, c, len(inst.interior_mosi),
                     len(inst.interior_miso)))
        if not inst.interior_mosi:
            detail = ("no complete MOSI word on CH%d..CH%d (SCLK %.4g MHz on CH%d) "
                      "- the capture may be too short to hold a whole word, or "
                      "SCLK/CS_N are not reaching the analyser"
                      % (c, c + 3, inst.sclk_hz / 1e6, c))
        # A structured verdict carries channel -1 and names its channels in
        # the text, so the pipeline keeps it apart from per-channel verdicts.
        out.append(Verdict(-1, inst.ok, "spi%d" % inst.instance,
                           got, want, detail,
                           channel_text="CH%d..CH%d" % (c, c + 3),
                           channels=tuple(range(c, c + 4))))
    return out


def _protocol_verdicts(channels: np.ndarray, sample_rate_hz: float,
                       mode: int = 4) -> list[Verdict]:
    """M4: the I2C and UART generators alongside the SPI bank."""
    out: list[Verdict] = []
    n = channels.shape[0]

    def ch(i):
        return channels[i] if i < n else None

    # UART TX on CH26, repeating "SLogic32U3\r\n" at 115200
    uart_ch = 26
    s = ch(uart_ch)
    if s is not None:
        got = deep_checks.decode_uart(s, sample_rate_hz, 115_200)
        want = b"SLogic32U3\r\n"
        # The capture starts wherever it starts, so the first decoded byte is
        # normally a fragment and the last is usually cut off. The transmitter
        # repeats, so "does the text appear" is the meaningful test.
        ok = want in got or want in got + got[:len(want)]
        div = int(54e6 / 115_200)       # integer division, as the RTL does it
        out.append(Verdict(-1, ok, "uart",
                           repr(got[:16]), repr(want),
                           "CH%d; 115200 baud; RTL divider %d -> %.1f Hz (%+.2f%%)"
                           % (uart_ch, div, 54e6 / div,
                              100.0 * (54e6 / div - 115_200) / 115_200),
                           channel_text="CH%d" % uart_ch,
                           channels=(uart_ch,)))

    # I2C: one write transaction per frame, SCL on CH24 and SDA on CH25
    scl, sda = ch(24), ch(25)
    if scl is not None and sda is not None:
        txs = deep_checks.decode_i2c(scl, sda)
        want = list(deep_checks_patterns_i2c())
        # Look INSIDE each transaction rather than scanning the concatenation of
        # all of them.
        #
        # Concatenating makes the result depend on how many transactions the
        # decoder happened to find and in what order: one spurious empty
        # transaction between two real ones breaks a sequence that is present in
        # full. That is exactly what happened with the fixture's duplicated STOP
        # (four per frame, now fixed in pat_proto.v) -- the verdict alternated
        # between PASS and FAIL on identical hardware because the spurious
        # transactions landed differently relative to the capture start.
        #
        # Per transaction, a spurious one contributes nothing and a real one is
        # judged on its own bytes.
        hit = None
        for tx in txs:
            b = list(tx.bytes_)
            if any(b[k:k + len(want)] == want for k in range(len(b))):
                hit = b
                break
        allb = [x for tx in txs for x in tx.bytes_]
        out.append(Verdict(-1, hit is not None, "i2c",
                           "%d tx, %s" % (len(txs),
                                          " ".join("%02X" % x for x in allb[:6])),
                           "a transaction containing %s"
                           % " ".join("%02X" % x for x in want),
                           ("CH24 SCL / CH25 SDA; 100 kHz; found in one "
                            "transaction" if hit is not None else
                            "CH24 SCL / CH25 SDA; 100 kHz; no single transaction "
                            "contained the expected sequence"),
                           channel_text="CH24/CH25",
                           channels=(24, 25)))
    return out


def deep_checks_patterns_i2c() -> tuple[int, ...]:
    from .patterns import I2C_ADDR_W, I2C_DATA
    return (I2C_ADDR_W,) + I2C_DATA


def _high_widths(s: np.ndarray) -> tuple[list[int], list[int]]:
    """(每个高电平段的宽度, 相邻两高段之间的低电平宽度)，单位采样点。"""
    d = np.diff(np.concatenate(([0], s, [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    highs = (ends - starts).astype(int).tolist()
    k = min(len(ends), len(starts))
    g = starts[1:k] - ends[:k - 1] if k > 1 else starts[:0]
    return highs, g[g > 0].astype(int).tolist()


def _narrow_pulse_verdicts(channels: np.ndarray, sample_rate_hz: float) -> list[Verdict]:
    """M5 的 CH2/CH3/CH4：判**脉冲宽度**，不判边沿率。

    这三路的信号形态让"频率"这个判据失效（不单独出逐通道判定），但它们的
    真实意图是**窄脉冲捕获能力**，完全可以用宽度来判：

      CH2  两个 18.5 ns 的脉冲，**相隔 18.5 ns**（能不能分辨开）
      CH3  每 1 ms 一个 18.5 ns 的脉冲（稀疏窄脉冲）
      CH4  宽度在 80 us 内从 1 扫到 128 个时钟（18.5 ns ~ 2.37 us）

    所以这里给它们一个**真实的判定**（合成一条跨通道判定，channel=-1，避免被
    pipeline 按通道号去重丢掉）。容差放宽到 ±2 个采样点：18.5 ns 在 200 MSa/s
    下只有 3.7 个采样点，量化本身就有 ±5 ns。
    """
    out: list[Verdict] = []
    n = channels.shape[0]
    if n < 5:
        return out
    dt_ns = 1e9 / sample_rate_hz
    clk_ns = 1e9 / 54e6                     # 一个 54 MHz 时钟 = 18.5 ns
    tol_ns = 2.0 * dt_ns                    # ±2 个采样点
    got_l: list[str] = []
    bad_l: list[str] = []
    for ch in (2, 3, 4):
        highs, _gaps = _high_widths(channels[ch])
        if not highs:
            got_l.append("CH%d 没抓到脉冲" % ch)
            bad_l.append("CH%d 完全没有脉冲（期望 %s）"
                         % (ch, "两个 18.5 ns 脉冲" if ch == 2 else
                          "每 1 ms 一个 18.5 ns 脉冲" if ch == 3 else
                          "18.5 ns~2.37 us 宽度扫描"))
            continue
        med_ns = float(np.median(highs)) * dt_ns
        if ch == 4:
            lo_ns = min(highs) * dt_ns
            hi_ns = max(highs) * dt_ns
            got_l.append("CH%d %d 个脉冲，宽 %.1f~%.1f ns" % (ch, len(highs), lo_ns, hi_ns))
            # 规格（rtl/pat_core.v:270 + docs/patterns.md）写的是一条**幂次阶梯
            # 1,2,4,...,128 clk_sys**，共 8 档，所以最窄 18.5 ns、最宽 2370 ns。
            #
            # 判据按**规格**写，不按实测写。2026-10-15 实测只到 595 ns（32 clk），
            # 查 RTL 发现 `(6'd1 << wsel)` 是 6 位表达式：wsel=6/7 时 64/128 被截成
            # 0，于是最上面两档根本不发脉冲。按实测放宽判据会把夹具的 bug 放过去，
            # 所以这里要求阶梯必须真的扫到 128 clk（留 10% 余量）。
            if lo_ns > 4 * clk_ns or hi_ns < 115 * clk_ns:
                bad_l.append("CH%d 宽度阶梯没扫到顶（实测最宽 %.1f ns，规格要求 "
                             "128 clk = %.1f ns；最窄 %.1f ns，规格 1 clk = %.1f ns）"
                             % (ch, hi_ns, 128 * clk_ns, lo_ns, clk_ns))
        else:
            got_l.append("CH%d %d 个脉冲，中位宽 %.1f ns" % (ch, len(highs), med_ns))
            if abs(med_ns - clk_ns) > tol_ns:
                bad_l.append("CH%d 中位脉宽 %.1f ns，期望约 %.1f ns（±%.1f ns）"
                             % (ch, med_ns, clk_ns, tol_ns))
    out.append(Verdict(-1, not bad_l, "narrow-pulse",
                       "; ".join(got_l),
                       "CH2/CH3 = 18.5 ns 脉冲；CH4 = 18.5 ns~2.37 us 扫描",
                       "; ".join(bad_l),
                       channel_text="CH2~CH4", channels=(2, 3, 4),
                       reason="" if not bad_l else "narrow_pulse"))
    return out


def _burst_verdicts(channels: np.ndarray, sample_rate_hz: float) -> list[Verdict]:
    """M6: measure the burst envelope and the pulse widths inside it."""
    out: list[Verdict] = []
    n = channels.shape[0]
    if n < 1:
        return out
    r = deep_checks.check_burst(channels[0], sample_rate_hz)
    period_ms = (r.period / sample_rate_hz * 1e3) if r.period else 0.0
    ok = r.ok and r.period is not None and abs(period_ms - 20.0) < 2.0
    out.append(Verdict(-1, ok, "burst",
                       "%d pulses, period %.1f ms" % (len(r.widths), period_ms),
                       "1 ms burst every 20 ms",
                       "envelope measured from the silent gap",
                       channel_text="CH0", channels=(0,)))
    # 四个窄脉冲通道的宽度是刻意做成阶梯的，但**必须合并成一条跨通道判定**。
    #
    # 以前这里一条通道一条判定、而且带的是正数通道号，于是被 pipeline 的
    # `verdicts_by_channel.setdefault(v.channel, v)` 静默丢掉 —— 通道 0..3 早被
    # 逐通道占位判定占住了，结果 M6 看着像通过，其实 burst 判定从来没跑过。
    # 结构化判定一律用 channel<0 + channel_text，才会按 kind 进 extra 保留下来。
    widths_clk = (8, 16, 32, 64)
    clk_ns = 1e9 / 54e6
    got_l, want_l, bad_l = [], [], []
    for c in range(min(4, n)):
        b = deep_checks.check_burst(channels[c], sample_rate_hz)
        want_ns = widths_clk[c] * clk_ns
        want_l.append("%.0f ns" % want_ns)
        if not b.widths:
            got_l.append("CH%d 没测到脉冲" % c)
            bad_l.append("CH%d 完全没有脉冲（期望 %.0f ns 宽）" % (c, want_ns))
            continue
        med = sorted(b.widths)[len(b.widths) // 2]
        got_ns = med / sample_rate_hz * 1e9
        got_l.append("CH%d %.0f ns" % (c, got_ns))
        if abs(got_ns - want_ns) > max(want_ns * 0.25, 2 * 1e9 / sample_rate_hz):
            bad_l.append("CH%d 实测 %.0f ns，期望 %.0f ns" % (c, got_ns, want_ns))
    out.append(Verdict(-1, not bad_l, "burst-width",
                       "; ".join(got_l), "; ".join(want_l),
                       "; ".join(bad_l),
                       channel_text="CH0~CH3",
                       channels=tuple(range(min(4, n)))))
    return out


def evaluate(mode: int, channels: np.ndarray, expects: list[Expect],
             sample_rate_hz: float, tol_pct: float = 5.0,
             tol_pp: float = 5.0) -> list[Verdict]:
    """Measure a capture and check it against the model.

    Structured checks are added on top of the per-channel ones. Modes 1..4 are the
    SPI bank: the per-channel model covers SCLK and CS_N by edge rate, and the
    decoder supplies the verdict that actually matters -- whether the known word
    came through. M0 adds a rotation check, because "which channel is high" is the
    whole point of a wiring capture and no single channel can prove it.
    Judged on the raw edges, without any glitch filtering (see measure_channel).
    """
    ms = measure_all(channels)
    # one entry per channel; None = judged by a structured check below
    per_ch = [check_channel(m, e, tol_pct, tol_pp, sample_rate_hz)
              for m, e in zip(ms, expects)]
    structured: list[Verdict] = []
    if mode == 0:
        from .patterns import WIRING_STEP_S, WIRING_STEPS
        structured.append(check_order(ms, samples_per_step=WIRING_STEP_S * sample_rate_hz,
                                      period=WIRING_STEPS))
        # each channel's own step: an open input or a bridge to a neighbour
        # passes the order check, not this one
        for c, v in enumerate(check_walking_one(channels, WIRING_STEP_S * sample_rate_hz,
                                                WIRING_STEPS, WIRING_STEP_S)):
            if v is not None and c < len(per_ch):
                per_ch[c] = v
    elif mode in (1, 2, 3, 4):
        structured.extend(_spi_bank_verdicts(channels, sample_rate_hz, mode))
        if mode == 4:
            structured.extend(_protocol_verdicts(channels, sample_rate_hz, mode))
    elif mode == 5:
        # CH2/CH3/CH4 判脉冲宽度（它们用边沿率判不了），CH0/CH1/CH5+ 仍走逐通道频率
        structured.extend(_narrow_pulse_verdicts(channels, sample_rate_hz))
    elif mode == 6:
        structured.extend(_burst_verdicts(channels, sample_rate_hz))
        # CH4 and up are judged one by one by the fill check -- per channel, so
        # a threshold-group pass takes over exactly its own channels
        complete, every = deep_checks.burst_windows(channels[0], sample_rate_hz)
        for c in range(4, min(len(per_ch), channels.shape[0])):
            r = deep_checks.check_burst_fill(channels[c], sample_rate_hz,
                                             complete, every)
            per_ch[c] = Verdict(c, r.ok, "burst-fill", r.measured,
                                "1 kHz for 0.5 ms of each 1 ms burst, low between",
                                r.detail, reason="" if r.ok else "burst_fill")
    verdicts = [v for v in per_ch if v is not None] + structured

    return verdicts


def summarise(verdicts: list[Verdict]) -> tuple[bool, int, int]:
    """(ok, passed, failed)."""
    f = sum(1 for v in verdicts if not v.ok)
    return (f == 0, len(verdicts) - f, f)


# ---------------------------------------------------------------------------
# self-test: synthesise captures and check the checkers
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    fails = 0

    # NOTE on window length: a capture must span enough periods of the SLOWEST
    # signal being checked. An early version of this self-test used a 2 ms window
    # for a 1 kHz signal, which contains exactly two periods and -- when the phase
    # lands so that the last rising edge falls outside -- only ONE rising edge, so
    # no frequency can be computed at all. The same trap applies to real captures:
    # M0's 1 kHz channel needs a window of at least a few milliseconds. This is why
    # the profile carries a per-mode window rather than one global value.

    def make(freq, n, fs, duty=0.5, phase=0.0):
        t = np.arange(n)
        period = fs / freq
        ph = np.mod(t / period + phase, 1.0)
        return (ph < duty).astype(np.uint8)

    print("== clean signals ==")
    cases = [(964320.0, 200_000_000, 400_000, 0.5),
             (421875.0, 200_000_000, 400_000, 1 / 32),
             (250000.0, 200_000_000, 400_000, 0.5),
             (100000.0, 200_000_000, 400_000, 0.01),
             (1000.0, 2_000_000, 40_000, 0.5)]       # 20 ms -> 20 periods
    for freq, fs, n, duty in cases:
        s = make(freq, n, fs, duty)
        m = measure_channel(s, 0)
        got = m.freq_hz * fs
        err = 100.0 * (got - freq) / freq
        dok = m.duty_pct is not None and abs(m.duty_pct - duty * 100) < 1.0
        ok = abs(err) < 0.5
        print("  %9.1f Hz duty %5.2f%% -> %10.1f Hz duty %5.2f%%  %s"
              % (freq, duty * 100, got, m.duty_pct if m.duty_pct is not None else -1,
                 "ok" if (ok and dok) else "FAIL"))
        fails += 0 if (ok and dok) else 1

    print("== static detection ==")
    for lvl in (0, 1):
        m = measure_channel(np.full(1000, lvl, dtype=np.uint8), 0)
        print("  constant %d -> static=%s toggling=%s" % (lvl, m.static, m.toggling))
        if m.static != lvl or m.toggling:
            print("  FAIL"); fails += 1

    print("== M0 walking one, channel by channel ==")
    def rotation(n=16, step=200, period=32, total=10_000, start=3):
        t = np.arange(total)
        return np.stack([(((t // step) + start) % period == c).astype(np.uint8)
                         for c in range(n)])
    good = rotation()
    opened = good.copy()                       # CH13: only an 18-sample blip per edge
    for k in np.flatnonzero(np.diff(good[13].astype(np.int8)) == 1) + 1:
        opened[13, k:k + 200] = 0
        opened[13, k:k + 18] = 1
    shorted = good.copy(); shorted[5] |= good[4]; shorted[4] |= good[5]
    missed = good.copy()
    k = int(np.flatnonzero(good[7])[0]); missed[7, k:k + 200] = 0
    for name, chans, want in (("good", good, {}), ("open CH13", opened, {13: "open_input"}),
                              ("CH4/CH5 bridged", shorted, {4: "short", 5: "short"}),
                              ("CH7 misses a turn", missed, {7: "missed_turn"})):
        got = {v.channel: v.reason for v in check_walking_one(chans, 200, 32)
               if v is not None and not v.ok}
        ok = got == want
        print("  %-18s -> %s %s" % (name, got or "all ok", "ok" if ok else "FAIL"))
        fails += 0 if ok else 1
    v = check_walking_one(shorted, 200, 32)[4]
    print("   ", v.detail)

    print("== threshold control ==")
    sq = (np.arange(4000) // 100 % 2).astype(np.uint8)
    normal = np.stack([sq, sq, np.zeros_like(sq)])
    blips = np.zeros_like(sq); blips[::500] = 1                 # overshoot leftovers
    for name, high, want in (("follows", np.stack([blips, 0 * sq, 0 * sq]), True),
                             ("CH1 stuck", np.stack([0 * sq, sq, 0 * sq]), False)):
        v = check_threshold_control(normal, high, 5.0)
        ok = v.ok is want
        print("  %-9s -> %s %s %s" % (name, v.ok, v.measured, "ok" if ok else "FAIL"))
        fails += 0 if ok else 1

    print("== M0 wiring diagnosis ==")
    def firing(perm, period=32):
        # analyser channel c receives fixture CH perm[c], which fires on step perm[c]
        return [(c, float(g)) for c, g in enumerate(perm)]
    cases = [("identity", list(range(16)), None),
             ("pairs", [c ^ 1 for c in range(16)], "wiring_pairs"),
             ("groups8", [c ^ 8 for c in range(16)], "wiring_groups8"),
             ("one cross", [0, 1, 2, 5, 4, 3] + list(range(6, 16)), "wiring"),
             ("32ch pairs", [c ^ 1 for c in range(32)], "wiring_pairs")]
    for name, perm, want in cases:
        offs = sorted(firing(perm), key=lambda cs: cs[1])
        base = offs[0][1]
        gen = infer_wiring([(c, o - base) for c, o in offs], len(perm), 32)
        got = None if gen is None or all(g == c for c, g in gen.items()) \
            else wiring_pattern(gen, len(perm))
        ok = gen is not None and got == want
        print("  %-10s -> %s %s" % (name, got, "ok" if ok else "FAIL"))
        fails += 0 if ok else 1

    print("== M3 rotation order ==")
    # 4 ms window so a whole 3.2 ms rotation fits; a 2 ms window holds less than
    # one cycle, and then every channel shows a single pulse whose "frequency" is
    # just an artefact of where the window happened to start.
    nch, n, fs = 8, 800_000, 200_000_000
    t = np.arange(n)
    step = int(100e-6 * fs)
    chans = np.zeros((nch, n), dtype=np.uint8)
    for ch in range(nch):
        ph = (t - (ch + 2) * step) % (nch * step)   # start at CH2
        chans[ch] = (ph < step).astype(np.uint8)
    ms = measure_all(chans)
    v = check_order(ms, samples_per_step=step)
    print("  %s  %s  (each channel reads %.2f Hz)"
          % (v.mark, v.measured, ms[0].freq_hz * fs))
    if not v.ok:
        print("  FAIL: correct rotation rejected"); fails += 1

    print("== M3 swapped pair must be caught ==")
    chans2 = chans.copy()
    chans2[[3, 4]] = chans2[[4, 3]]
    v2 = check_order(measure_all(chans2), samples_per_step=step)
    print("  %s  %s" % (v2.mark, (v2.detail or "")[:100]))
    if v2.ok:
        print("  FAIL: swap not detected"); fails += 1

    print("== a channel that never fires must not silently pass order ==")
    chans3 = chans.copy()
    chans3[5] = 0
    v3 = check_order(measure_all(chans3), samples_per_step=step)
    print("  %s  (%d channels seen)" % (v3.mark, 7))
    if v3.ok:
        print("  FAIL: a dead channel was accepted"); fails += 1

    print("== 16 of a 32-step rotation (16U3 in front of the fixture) ==")
    # 10 ms, as the M0 plan captures: a channel needs two rising edges, and the
    # 32-step rotation takes 3.2 ms
    nch16, per, n16 = 16, 32, 2_000_000
    t16 = np.arange(n16)
    chans16 = np.zeros((nch16, n16), dtype=np.uint8)
    for ch in range(nch16):
        ph = (t16 - (ch - 10) * step - step // 2) % (per * step)   # CH10 fires first
        chans16[ch] = (ph < step).astype(np.uint8)
    v4 = check_order(measure_all(chans16), samples_per_step=step, period=per)
    print("  %s  %s" % (v4.mark, v4.measured))
    if not v4.ok:
        print("  FAIL: partial rotation rejected"); fails += 1
    if check_order(measure_all(chans16), samples_per_step=step).ok:
        print("  FAIL: without period the 17-step wrap should not pass"); fails += 1
    chans16b = chans16.copy()
    chans16b[[3, 4]] = chans16b[[4, 3]]
    if check_order(measure_all(chans16b), samples_per_step=step, period=per).ok:
        print("  FAIL: swap in a partial rotation not detected"); fails += 1

    print()
    print("RESULT: %s" % ("OK" if not fails else "%d FAILURES" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
