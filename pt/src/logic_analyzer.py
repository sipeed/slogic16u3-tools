import re
import numpy as np

def parse_filename(filename):
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
        data = np.frombuffer(data, dtype=np.uint16)
        channels = [(data >> i) & 1 for i in range(16)]
    elif num_channels == 8:
        data = np.frombuffer(data, dtype=np.uint8)
        channels = [(data >> i) & 1 for i in range(8)]
    elif num_channels == 4:
        data = np.frombuffer(data, dtype=np.uint8)
        n = len(data) * 2
        unpacked = np.empty(n, dtype=np.uint8)
        unpacked[0::2] = data & 0x0F
        unpacked[1::2] = (data >> 4) & 0x0F
        channels = [(unpacked >> i) & 1 for i in range(4)]
    else:
        raise ValueError("Unsupported channel count: %d" % num_channels)
    return channels

def detect_pwm_freq(samples, sample_rate):
    edges = np.where((samples[:-1] == 0) & (samples[1:] == 1))[0]
    if len(edges) < 2:
        return None
    periods = np.diff(edges)
    avg_period = np.mean(periods)
    if avg_period == 0:
        return None
    freq = sample_rate / avg_period
    return freq

def check_pwm_duty(samples):
    edges = np.where((samples[:-1] == 0) & (samples[1:] == 1))[0]
    if len(edges) < 2:
        return None
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