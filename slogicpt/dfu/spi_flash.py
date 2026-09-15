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
        
    def read_data(self, addr, length):
        """Read data from specified address"""
        data = b''
        got = 0
        while got < length:
            need = length - got
            if need > 0x50:
                need = 0x50
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
        
    def program_page(self, addr, payload):
        """Program a page at specified address with given payload"""
        with self.we():
            self.spi.xfer(b'\x02' + self._addr_to_bytes(addr) + payload)

    def program(self, addr, payload):
        length = len(payload)
        programed = 0
        while programed < length:
            need = length - programed
            if need > self.page_size:
                need = self.page_size
            data = payload[programed: programed+need]
            if data.count(0xFF) != len(data):
                print(f'[{100.0*programed/length:.2f}%]program 0x{addr+programed:06X}...')
                self.program_page(addr+programed, data)
            else:
                print(f'skip 0x{addr+programed:06X}...')
            programed += need
        assert(length == programed)
        
    def _addr_to_bytes(self, addr):
        """Convert 24-bit address to 3 bytes (big-endian)"""
        return bytes([
            (addr >> 16) & 0xFF,
            (addr >> 8) & 0xFF,
            addr & 0xFF
        ])
        
    class _WriteEnableManager:
        def __init__(self, flash_dev):
            self.flash_dev = flash_dev
            
        def __enter__(self):
            self.flash_dev.spi.xfer(b'\x06')  # Write Enable
            return self
            
        def __exit__(self, exc_type, exc_val, exc_tb):
            while 0x1 & self.flash_dev.spi.xfer(b'\x05', 1)[0]:  # Status Register-1 S0:WIP
                continue
            self.flash_dev.spi.xfer(b'\x04')  # Write Disable


ERASE_BLOCK = 0x10000  # 64KB
PROGRAM_PAGE_SIZE = 0x20  # 实测稳定值


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
        print("ID:", flash.read_id().hex())
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

        flash.page_size = PROGRAM_PAGE_SIZE
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
