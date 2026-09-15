"""OTA<->APP 模式切换。

三种方式（见产品档案 [mode_switch].method）；本模块只实现 usb_reconfig，
script/manual 由 pipeline._switch 分别以运行脚本 / 仅提示处理：

- "usb_reconfig"：向当前模式的设备发送 USB 控制传输 RECONFIG（见"USB LA 协议
  规范" 0x30 设备管理扩展），触发 FPGA 重配置，设备离线后以另一模式重枚举。
  32U3 支持双向（OTA<->APP）。
- "script"：运行切换脚本（如 16U3 外置 JTAG + gowin_cli），工具追加方向参数
  ota2app / app2ota；脚本缺失时自动回退为 manual。
- "manual"：工具不发起切换，切换步骤仅提示，由后续"等待设备"步骤轮询目标
  模式出现。

RECONFIG 控制传输字段（Vendor OUT, Device recipient）：
    bmRequestType = 0x40
    bRequest      = 0x30
    wValue        = 0x0001   # 1=触发重配置（0=no-op 探测）
    wIndex        = 0x5253   # 固定 magic 'RS'
    wLength       = 0        # 无数据
规范注明：触发后设备可能立即离线，当前 USB session 不保证仍可继续——因此控制
传输的 status stage 报错（pipe/no-device）属预期，视作已发出，真正的成功判据是
随后目标模式设备重枚举（由调用方 wait_for_pid 确认）。
"""
from __future__ import annotations

from typing import Callable

import usb.core
import usb.util

RECONFIG_REQTYPE = 0x40    # Vendor, host-to-device, device recipient
RECONFIG_BREQUEST = 0x30
RECONFIG_TRIGGER = 0x0001
RECONFIG_MAGIC = 0x5253


class ModeSwitchError(Exception):
    pass


def reconfig(vid: int, pid: int, log_cb: Callable[[str], None] | None = None) -> None:
    """向 (vid, pid) 设备发送 RECONFIG 触发重配置。

    设备未找到时抛 ModeSwitchError；控制传输本身因设备离线而报错则视为已发出
    （规范预期行为），不抛异常。切换是否成功由调用方轮询目标 PID 判定。
    """
    def _log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise ModeSwitchError(f"未找到设备 {vid:#06x}:{pid:#06x}，无法发送 RECONFIG")
    try:
        dev.ctrl_transfer(RECONFIG_REQTYPE, RECONFIG_BREQUEST,
                          RECONFIG_TRIGGER, RECONFIG_MAGIC, None)
        _log(f"RECONFIG 已发送至 {vid:#06x}:{pid:#06x}")
    except usb.core.USBError as e:
        # 触发后设备可能立即离线，status stage 报错属预期
        _log(f"RECONFIG 已触发（设备离线，控制传输返回 {e}，属预期）")
    finally:
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("用法: python -m slogicpt.mode_switch <vid> <pid>  # 手动向设备发送 RECONFIG")
        sys.exit(2)
    v = int(sys.argv[1], 0)
    p = int(sys.argv[2], 0)
    try:
        reconfig(v, p, print)
        print("完成（请观察设备是否以另一模式重枚举）")
    except ModeSwitchError as e:
        print(f"失败: {e}")
        sys.exit(1)
