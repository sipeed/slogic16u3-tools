"""Compatibility shim -- the implementation moved to waveform.py.

Kept so any external scripts importing `logic_analyzer` keep working.
Note: the old slogic_cli 4-channel nibble-packed format is no longer
supported (see waveform.extract_channels docstring).
"""
from waveform import (  # noqa: F401
    check_pwm_duty,
    detect_pwm_freq,
    extract_channels,
)
