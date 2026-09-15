from .usb_device import USBDevice
from .spi_device import SPIDevice

class SPIFlashDevice:
    def __init__(self, vid, pid):
        self.usb_device = USBDevice(vid, pid)
        self.page_size = 0x100
        
    def __enter__(self):
        self.spi = SPIDevice(self.usb_device).__enter__()
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
        
    # USB-SPI 桥每次 CMD_READ_DATA 只能返回单个批量包（≤64B）；请求 65B 起即
    # [Errno 75] Overflow 并卡死端点。故回读按 64B 分块。
    READ_CHUNK = 0x40

    def read_data(self, addr, length):
        """Read data from specified address"""
        data = b''
        got = 0
        while got < length:
            need = length - got
            if need > self.READ_CHUNK:
                need = self.READ_CHUNK
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
        
    # 桥接器单次 SPI 写事务上限为 16 字节（PP 指令 0x02 + 3 字节地址 + ≤12 字节数据）；
    # 超过则数据根本不发出、TX FIFO 卡死、总线挂起（SR1 恒读 0xff）。故每次页编程最多
    # 写 12 字节，且不得跨 256 字节页边界（PP 在页内回卷，跨界会写错地址）。
    WRITE_CHUNK = 0x0C  # 12

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
            need = min(self.WRITE_CHUNK, room, length - programed)
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
        WIP_TIMEOUT_S = 5.0  # 页写 <1ms、64KB 擦除 ~数百 ms，5s 足够且能兜住通信异常

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
                    raise RuntimeError("Flash 通信异常：SR1 恒读 0xFF（总线挂起）")
                if not (sr & 0x1):  # S0:WIP=0，写入完成
                    break
                if time.time() - t0 > self.WIP_TIMEOUT_S:
                    raise RuntimeError("Flash 写等待超时：WIP 未在预期内清零")
            self.flash_dev.spi.xfer(b'\x04')  # Write Disable


ERASE_BLOCK = 0x10000  # 64KB


def flash_firmware(vid: int, pid: int, addr: int, firmware: bytes,
                   verify: bool = True, dump_file: str | None = None) -> None:
    """Erase + program + optional verify.  Raises RuntimeError on failure."""
    if addr % ERASE_BLOCK != 0:
        raise RuntimeError(f"起始地址 0x{addr:06X} 未按 64KB 对齐")
    size = len(firmware)
    if size == 0:
        raise RuntimeError("固件为空")

    with SPIFlashDevice(vid, pid) as flash:
        if not flash.reset():
            raise RuntimeError("SPI flash reset 失败")
        # 读 ID 确认 flash 可用：上次会话中途中断可能让 flash 停在坏状态，
        # 首次读 ID 返回 0xffffff/0x000000，此时重试一次 reset 再读；仍无响应则明确报错，
        # 绝不在坏状态上进入擦除/编程（否则又会触发 WIP 死等/写不进）。
        dev_id = flash.read_id().hex()
        if dev_id in ('ffffff', '000000'):
            flash.reset()
            dev_id = flash.read_id().hex()
            if dev_id in ('ffffff', '000000'):
                raise RuntimeError(f"Flash 无响应（ID={dev_id}），请检查 OTA 连接后重试")
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
            raise RuntimeError("擦除后校验失败：区域非全 0xFF")

        flash.program(addr, firmware)

        if verify:
            readback = flash.read_data(addr, size)
            if readback != firmware:
                diff = next(i for i in range(size) if readback[i] != firmware[i])
                raise RuntimeError(f"烧写校验失败：首个差异在 0x{addr+diff:06X}")
            print("Verify OK")


def _parse_int(s: str) -> int:
    return int(s, 0)  # accepts 0x..., decimal


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="通过 OTA 模式 USB-SPI 通道烧写 SPI Flash 固件")
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
