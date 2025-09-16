# analyze_wave.py
import sys
import re
import os
import numpy as np
import matplotlib.pyplot as plt
from termcolor import colored

def parse_filename(filename):
    # e.g. 8ch_400M_wave.bin or 16ch_20M_wave.bin
    m = re.match(r"(\d+)ch_(\d+)([kKmM])_wave\.bin", filename)
    if not m:
        raise ValueError("Filename must be like '8ch_400M_wave.bin'")
    ch = int(m.group(1))
    rate = int(m.group(2))
    unit = m.group(3).upper()
    if unit == 'M':
        rate *= 1_000_000
    elif unit == 'K':
        rate *= 1_000
    return ch, rate

def extract_channels(data, num_channels):
    if num_channels == 16:
        # Each sample is 2 bytes, little-endian
        data = np.frombuffer(data, dtype=np.uint16)
        channels = [(data >> i) & 1 for i in range(16)]
    elif num_channels == 8:
        # Each sample is 1 byte
        data = np.frombuffer(data, dtype=np.uint8)
        channels = [(data >> i) & 1 for i in range(8)]
    elif num_channels == 4:
        # Each sample is 4 bits (packed two samples per byte)
        data = np.frombuffer(data, dtype=np.uint8)
        # Unpack nibbles
        n = len(data) * 2
        unpacked = np.empty(n, dtype=np.uint8)
        unpacked[0::2] = data & 0x0F
        unpacked[1::2] = (data >> 4) & 0x0F
        channels = [(unpacked >> i) & 1 for i in range(4)]
    else:
        raise ValueError("Unsupported channel count: %d" % num_channels)
    return channels

def detect_pwm_freq(samples, sample_rate):
    # Find rising edges (0->1)
    edges = np.where((samples[:-1] == 0) & (samples[1:] == 1))[0]
    if len(edges) < 2:
        return None
    periods = np.diff(edges)  # in samples
    avg_period = np.mean(periods)
    if avg_period == 0:
        return None
    freq = sample_rate / avg_period
    return freq

def check_pwm_duty(samples):
    # Find rising edges (0->1)
    edges = np.where((samples[:-1] == 0) & (samples[1:] == 1))[0]
    if len(edges) < 2:
        return None
    # Analyze each period
    duty_cycles = []
    for i in range(len(edges) - 1):
        period = samples[edges[i]:edges[i+1]]
        if len(period) == 0:
            continue
        high = np.count_nonzero(period)
        duty = high / len(period)
        duty_cycles.append(duty)
    if not duty_cycles:
        return None
    avg_duty = np.mean(duty_cycles)
    return avg_duty

CHANNEL_SAMPLE_DESIRED = [
    (10*10**6, 50),
    (50*10**6, 50),
    (50*10**6, 50),
    (50*10**6, 50),
    (50*10**6, 50),
    (50*10**6, 50),
    (50*10**6, 50),
    (50*10**6, 50),
]

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 main.py <xch_xM_wave.bin>")
        sys.exit(1)
    filename = sys.argv[1]
    base_filename = os.path.basename(filename)
    num_channels, sample_rate = parse_filename(base_filename)
    print(f"Detected: {num_channels} channels, {sample_rate} Hz sample rate")

    with open(filename, "rb") as f:
        raw = f.read()

    channels = extract_channels(raw, num_channels)
    print(f"Total samples: {len(channels[0])}")

    plt.figure(figsize=(12, 6))
    for ch in range(num_channels):
        samples = channels[ch][:1000]
        freq = detect_pwm_freq(samples, sample_rate)
        duty = check_pwm_duty(samples)
        # Prepare label for both console and plot
        if freq:
            freq_str = f"{freq/1e6:.6f}MHz"
        else:
            freq_str = "N/A"
        if duty is not None:
            duty_str = f"{duty*100:.2f}%"
        else:
            duty_str = "N/A"

        # Remove validation and fail print
        label = f'CH{ch} ({freq_str}, {duty_str})'
        # Print to console
        print(f"CH{ch}: PWM freq = {freq_str}, duty cycle = {duty_str}")
        plt.plot(samples + ch*2, label=label)
    plt.legend(loc='upper right', fontsize='small')

    plt.title(base_filename)

    plt.xlabel("Sample Index")
    plt.ylabel("Logic Level (offset by channel)")
    plt.tight_layout()
    plt.gca().invert_yaxis()
    plt.show()

if __name__ == "__main__":
    main()