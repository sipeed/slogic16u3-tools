"""Waveform unpacking and PWM verification, generic over channel count.

Data format: sigrok-cli `-O binary` output.  Each sample is `unitsize`
bytes little-endian, where unitsize = ceil(device_total_channels / 8) --
the device's native width, regardless of how many channels were enabled
for the capture (verified empirically on SLogic16U3: 8-channel capture
still yields 2 bytes/sample).  The stream may carry textual
`FRAME-BEGIN\\n` / `FRAME-END\\n` markers which must be stripped.

The legacy slogic_cli 4-channel nibble-packed format is NOT supported
here (slogic_cli is deprecated); use cli/show.py for old archives.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

FRAME_MARKERS = (b"FRAME-BEGIN\n", b"FRAME-END\n")


def strip_frame_markers(data: bytes) -> bytes:
    for marker in FRAME_MARKERS:
        if marker in data:
            data = data.replace(marker, b"")
    return data


def extract_channels(data: bytes, num_channels: int,
                     unitsize: int | None = None) -> np.ndarray:
    """Unpack packed samples into per-channel bit arrays.

    Returns array of shape (num_channels, n_samples), values 0/1.
    `unitsize` defaults to ceil(num_channels/8); pass the device unitsize
    explicitly when verifying a channel subset (e.g. 8 of 16 channels).
    """
    if unitsize is None:
        unitsize = (num_channels + 7) // 8
    if num_channels > unitsize * 8:
        raise ValueError(f"num_channels={num_channels} 超出 unitsize={unitsize} 容量")
    n = len(data) // unitsize
    if n == 0:
        raise ValueError("数据为空或不足一个样本")
    arr = np.frombuffer(data[:n * unitsize], dtype=np.uint8).reshape(n, unitsize)
    bits = np.unpackbits(arr, axis=1, bitorder="little")   # little-endian bit/byte order
    return np.ascontiguousarray(bits[:, :num_channels].T)


def load_capture_file(path: Path, num_channels: int, unitsize: int) -> np.ndarray:
    return extract_channels(strip_frame_markers(Path(path).read_bytes()),
                            num_channels, unitsize)


def detect_pwm_freq(samples: np.ndarray, sample_rate: float) -> float | None:
    edges = np.where((samples[:-1] == 0) & (samples[1:] == 1))[0]
    if len(edges) < 2:
        return None
    avg_period = np.diff(edges).mean()
    if avg_period == 0:
        return None
    return sample_rate / avg_period


def check_pwm_duty(samples: np.ndarray) -> float | None:
    edges = np.where((samples[:-1] == 0) & (samples[1:] == 1))[0]
    if len(edges) < 2:
        return None
    span = samples[edges[0]:edges[-1]]
    if len(span) == 0:
        return None
    return float(np.count_nonzero(span)) / len(span)


@dataclass(frozen=True)
class ChannelVerdict:
    channel: int
    freq_hz: float | None
    duty: float | None
    freq_ok: bool
    duty_ok: bool

    @property
    def ok(self) -> bool:
        return self.freq_ok and self.duty_ok


def verify_channels(channels: np.ndarray, sample_rate: float,
                    expected_freq_hz: float, expected_duty_pct: float,
                    freq_tol_pct: float, duty_tol_pp: float,
                    max_samples: int = 200_000) -> tuple[bool, list[ChannelVerdict]]:
    """Verify each channel carries the expected PWM signal."""
    verdicts: list[ChannelVerdict] = []
    for ch in range(channels.shape[0]):
        samples = channels[ch, :max_samples]
        freq = detect_pwm_freq(samples, sample_rate)
        duty = check_pwm_duty(samples)
        freq_ok = freq is not None and abs(freq - expected_freq_hz) <= expected_freq_hz * freq_tol_pct / 100.0
        duty_ok = duty is not None and abs(duty * 100.0 - expected_duty_pct) <= duty_tol_pp
        verdicts.append(ChannelVerdict(ch, freq, duty, freq_ok, duty_ok))
    return all(v.ok for v in verdicts), verdicts


def _selftest() -> int:
    print("== waveform 自测：合成 10MHz/50% 方波 ==")
    sample_rate = 200_000_000
    n = 100_000
    t = np.arange(n)
    square = ((t // 10) % 2 == 0).astype(np.uint8)   # 10 samples high, 10 low @200M -> 10MHz 50%
    failures = 0
    for num_ch, unitsize in ((4, 1), (8, 1), (16, 2), (32, 4)):
        words = np.zeros(n, dtype=np.uint64)
        for ch in range(num_ch):
            words |= square.astype(np.uint64) << ch
        packed = words.astype(f"<u{max(unitsize, 1)}").tobytes()[:n * unitsize]
        chans = extract_channels(packed, num_ch, unitsize)
        ok, verdicts = verify_channels(chans, sample_rate, 10e6, 50.0, 5.0, 5.0)
        v0 = verdicts[0]
        print(f"  {num_ch:>2}ch unitsize={unitsize}: {'PASS' if ok else 'FAIL'} "
              f"(ch0: {v0.freq_hz/1e6:.4f}MHz {v0.duty*100:.2f}%)")
        failures += 0 if ok else 1
    # marker stripping
    stripped = strip_frame_markers(b"FRAME-BEGIN\n" + b"\x01\x02" + b"FRAME-END\n")
    assert stripped == b"\x01\x02", stripped
    print("  FRAME 标记剥离: PASS")
    # wrong expectation must FAIL
    ch16 = extract_channels(np.repeat(square, 1).astype("<u2").tobytes(), 16, 2)
    ok, _ = verify_channels(ch16[:1], sample_rate, 20e6, 50.0, 5.0, 5.0)
    print(f"  错误期望(20MHz)应 FAIL: {'PASS' if not ok else 'FAIL'}")
    failures += 0 if not ok else 1

    legacy = REGRESSION_FILE
    if legacy.is_file():
        print(f"== 回归: {legacy.name} (16ch@200M, 旧 slogic_cli 格式与 sigrok 16ch 格式一致) ==")
        chans = extract_channels(legacy.read_bytes(), 16, 2)
        ok, verdicts = verify_channels(chans, 200e6, 10e6, 50.0, 5.0, 8.0)
        for v in verdicts[:4]:
            f = f"{v.freq_hz/1e6:.4f}MHz" if v.freq_hz else "N/A"
            d = f"{v.duty*100:.2f}%" if v.duty is not None else "N/A"
            print(f"  CH{v.channel}: {f} {d} {'ok' if v.ok else 'X'}")
        print(f"  overall: {'PASS' if ok else 'FAIL'}")
    return failures


REGRESSION_FILE = Path(__file__).resolve().parents[1] / "out" / "16ch_200M_wave.bin"

if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
