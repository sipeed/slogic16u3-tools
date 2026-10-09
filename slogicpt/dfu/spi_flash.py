from ..i18n import t
from .usb_device import USBDevice
from .spi_device import SPIDevice

class SPIFlashDevice:
    # 默认值即保守稳定值（与旧硬编码一致）；由 product.toml [ota] 经 flash_firmware
    # 覆盖，便于在不重编译的前提下调参提速。
    def __init__(self, vid, pid, write_chunk: int = 0x0C, read_chunk: int = 0x40,
                 usb_timeout_ms: int = 1000, wip_timeout_s: float = 5.0,
                 reset_settle_s: float = 2.0):
        self.usb_device = USBDevice(vid, pid, timeout_ms=usb_timeout_ms,
                                    settle_s=reset_settle_s)
        self.page_size = 0x100
        self.write_chunk = write_chunk
        self.read_chunk = read_chunk
        self.usb_timeout_ms = usb_timeout_ms
        self.wip_timeout_s = wip_timeout_s

    def __enter__(self):
        self.spi = SPIDevice(self.usb_device, timeout=self.usb_timeout_ms).__enter__()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.spi.__exit__(exc_type, exc_val, exc_tb)
        
    def reset(self):
        """Reset the flash device"""
        return self.spi.reset()
        
    def read_id(self):
        """Read manufacturer and device ID (3 bytes)"""
        return self.spi.xfer(b'\x9F', 3)
        
    def read_uid(self):
        """Read unique ID (16 bytes)"""
        return self.spi.xfer(b'\x4B', 16, 4)
        
    # 回读分块大小 self.read_chunk（默认 64）。注意：USB-SPI 桥每次 CMD_READ_DATA
    # 多半只返回单个批量包（≤64B，High-Speed 可能更大）；请求过大可能 [Errno 75]
    # Overflow 并卡死端点。由 [ota].read_chunk 调整，稳步上调确认。
    def read_data(self, addr, length):
        """Read data from specified address"""
        data = b''
        got = 0
        while got < length:
            need = length - got
            if need > self.read_chunk:
                need = self.read_chunk
            data += self.spi.xfer(b'\x0B' + self._addr_to_bytes(addr+got), need, 1)
            got += need
        assert(len(data) == got)
        assert(length == got)
        return data
        
    def we(self):
        """Context manager for write enable/disable operations"""
        return self._WriteEnableManager(self)
        
    def erase_64kb(self, addr):
        """Erase a 64KB block at specified address"""
        with self.we():
            print(f'erase 64KB 0x{addr:06X}...')
            self.spi.xfer(b'\xD8' + self._addr_to_bytes(addr))
        
    # 页编程每次 SPI 事务的数据字节 self.write_chunk（默认 12）。保守值 12 来自
    # 桥接器单事务上限（0x02 PP + 3 字节地址 + ≤12 数据 = 16B）——超限则数据不发出、
    # TX FIFO 卡死、总线挂起（SR1 恒读 0xff）。提速主杠杆，由 [ota].write_chunk 调大；
    # program() 已按 256 页边界分块（PP 在页内回卷，跨界会写错地址），故上限 256。
    def program_page(self, addr, payload):
        """写入一段 ≤12 字节且不跨 256 页边界的数据（单次 PP 事务）"""
        with self.we():
            self.spi.xfer(b'\x02' + self._addr_to_bytes(addr) + payload)

    def program(self, addr, payload):
        length = len(payload)
        programed = 0
        last_pct = -1
        while programed < length:
            a = addr + programed
            room = 0x100 - (a & 0xFF)          # 到下一个 256 页边界的剩余字节
            need = min(self.write_chunk, room, length - programed)
            data = payload[programed: programed+need]
            if data.count(0xFF) != need:       # 全 0xFF 段跳过（擦除后本就是 0xFF）
                self.program_page(a, data)
            programed += need
            pct = int(100.0 * programed / length)
            if pct != last_pct:
                print(f'[{pct}%]program 0x{addr:06X}+0x{programed:X}...')
                last_pct = pct
        assert(length == programed)
        
    def _addr_to_bytes(self, addr):
        """Convert 24-bit address to 3 bytes (big-endian)"""
        return bytes([
            (addr >> 16) & 0xFF,
            (addr >> 8) & 0xFF,
            addr & 0xFF
        ])
        
    class _WriteEnableManager:
        # 页写完成轮询超时取自 flash_dev.wip_timeout_s（默认 5s）：页写 <1ms、
        # 64KB 擦除 ~数百 ms，5s 足够且能兜住通信异常（SR1 恒 0xFF 的死等）。

        def __init__(self, flash_dev):
            self.flash_dev = flash_dev

        def __enter__(self):
            self.flash_dev.spi.xfer(b'\x06')  # Write Enable
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            # 已在异常中就不再等待/覆盖原异常，尽力发一次 WRDI 即退出
            if exc_type is not None:
                self.flash_dev.spi.xfer(b'\x04')
                return
            # 轮询 WIP 直到写完成。旧代码无超时保护：通信异常时 SR1 恒读 0xff
            #（bit0=1）会死循环——这正是之前"卡住不动"的根因。
            import time
            t0 = time.time()
            while True:
                sr = self.flash_dev.spi.xfer(b'\x05', 1)[0]  # Status Register-1
                if sr == 0xFF:
                    raise RuntimeError(t("Flash communication error: SR1 stuck reading 0xFF (bus hung)"))
                if not (sr & 0x1):  # S0:WIP=0，写入完成
                    break
                if time.time() - t0 > self.flash_dev.wip_timeout_s:
                    raise RuntimeError(t("Flash write wait timed out: WIP did not clear in time"))
            self.flash_dev.spi.xfer(b'\x04')  # Write Disable


ERASE_BLOCK = 0x10000  # 64KB


def flash_firmware(vid: int, pid: int, addr: int, firmware: bytes,
                   verify: bool = True, dump_file: str | None = None,
                   ota=None) -> None:
    """Erase + program + optional verify.  Raises RuntimeError on failure.

    `ota` (optional): any object carrying write_chunk / read_chunk /
    usb_timeout_ms / wip_timeout_s / reset_settle_s (a profiles.OtaParams);
    None keeps the conservative defaults.  Duck-typed so dfu/ stays decoupled."""
    if addr % ERASE_BLOCK != 0:
        raise RuntimeError(t("Start address 0x{addr:06X} is not 64KB-aligned").format(addr=addr))
    size = len(firmware)
    if size == 0:
        raise RuntimeError(t("Firmware is empty"))

    kw = {} if ota is None else dict(
        write_chunk=ota.write_chunk, read_chunk=ota.read_chunk,
        usb_timeout_ms=ota.usb_timeout_ms, wip_timeout_s=ota.wip_timeout_s,
        reset_settle_s=ota.reset_settle_s)
    with SPIFlashDevice(vid, pid, **kw) as flash:
        if not flash.reset():
            raise RuntimeError(t("SPI flash reset failed"))
        # 读 ID 确认 flash 可用：上次会话中途中断可能让 flash 停在坏状态，
        # 首次读 ID 返回 0xffffff/0x000000，此时重试一次 reset 再读；仍无响应则明确报错，
        # 绝不在坏状态上进入擦除/编程（否则又会触发 WIP 死等/写不进）。
        dev_id = flash.read_id().hex()
        if dev_id in ('ffffff', '000000'):
            flash.reset()
            dev_id = flash.read_id().hex()
            if dev_id in ('ffffff', '000000'):
                raise RuntimeError(t("Flash not responding (ID={dev_id}); check the DFU connection and retry").format(dev_id=dev_id))
        print("ID:", dev_id)
        print("UID:", flash.read_uid().hex())

        if dump_file:
            data = flash.read_data(addr, size)
            open(dump_file, 'wb').write(data)
            print(f"Dumped {len(data)} bytes to {dump_file}")

        for a in range(addr, addr + size, ERASE_BLOCK):
            flash.erase_64kb(a)
        erased = flash.read_data(addr, size)
        if erased.count(0xFF) != len(erased):
            raise RuntimeError(t("Post-erase verify failed: region is not all 0xFF"))

        flash.program(addr, firmware)

        if verify:
            readback = flash.read_data(addr, size)
            if readback != firmware:
                diff = next(i for i in range(size) if readback[i] != firmware[i])
                raise RuntimeError(t("Flash verify failed: first difference at 0x{pos:06X}").format(pos=addr+diff))
            print("Verify OK")


def _parse_int(s: str) -> int:
    return int(s, 0)  # accepts 0x..., decimal


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="通过 DFU 模式 USB-SPI 通道烧写 SPI Flash 固件")
    parser.add_argument("firmware", help="固件 .bin 文件路径")
    parser.add_argument("--vid", type=_parse_int, default=0x359F, help="USB VID (默认 0x359F)")
    parser.add_argument("--pid", type=_parse_int, default=0x30F1, help="USB PID (默认 0x30F1)")
    parser.add_argument("--addr", type=_parse_int, default=0x0, help="烧写起始地址，须 64KB 对齐 (默认 0x0)")
    parser.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                        help="烧写后回读校验 (默认开)")
    parser.add_argument("--dump", metavar="FILE", default=None, help="烧写前先 dump 原内容到文件")
    args = parser.parse_args(argv)

    with open(args.firmware, 'rb') as f:
        firmware = f.read()
    print(f"Read {len(firmware)} bytes from {args.firmware}")

    try:
        flash_firmware(args.vid, args.pid, args.addr, firmware,
                       verify=args.verify, dump_file=args.dump)
    except (RuntimeError, ValueError, AssertionError) as e:
        print(f"FAILED: {e}")
        return 1
    print("Done")
    return 0


# python spi_flash.py firmware.bin [--vid 0x359F --pid 0x30F1 --addr 0x0]
if __name__ == "__main__":
    import sys
    sys.exit(main())
