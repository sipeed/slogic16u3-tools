from usb_device import USBDevice
from spi_device import SPIDevice

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


# python spi_flash.py firmware.bin
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} <firmware.bin>')
        sys.exit(1)

    # 直接读取文件（二进制模式）
    with open(sys.argv[1], 'rb') as f:
        firmware = f.read()
        firmware_size = len(firmware)
    print(f"Read {firmware_size} bytes")

    alignment = 0x10000  # 64KB 对齐
    # padding_size = (alignment - (firmware_size % alignment)) % alignment  # 计算需要填充的长度

    # firmware += bytes([0xFF] * padding_size) # 在末尾补 0xFF
    # print(f'raw size: {firmware_size}')
    # firmware_size += padding_size
    # print(f'paded size: {firmware_size}')

    with SPIFlashDevice(0x359F, 0x30F1) as flash: 
        # Reset flash
        assert flash.reset()
        
        # Read ID and UID
        print("ID:", flash.read_id().hex())
        print("UID:", flash.read_uid().hex())
        print("STATUS:")
        data = flash.spi.xfer(b'\x05', 1)
        print(data.hex())
        data = flash.spi.xfer(b'\x35', 1)
        print(data.hex())
        data = flash.spi.xfer(b'\x15', 1)
        print(data.hex())
        
        # Read data
        data = flash.read_data(0x0, firmware_size)
        print(f"Dump {len(data)} bytes")
        open('dump.bin', 'wb').write(data)

        # start = 0x100000
        start = 0x0

        print(f"=======================================================================")
        # Erase
        for addr in range(start, start+firmware_size, alignment):
            flash.erase_64kb(addr)
        data = flash.read_data(start, firmware_size)
        # print(f"Dump {len(data)} bytes to erased.bin")
        # open('erased.bin', 'wb').write(data)
        print(data.count(0xFF) == len(data))

        # data = b''
        # for addr in range(start, start+firmware_size, alignment):
        #     tmp = flash.read_data(addr, alignment)
        #     while tmp.count(0xFF) != len(tmp):
        #         print(f"Erase 0x{addr:06X}:")
        #         with flash.we():
        #             flash.spi.xfer(b'\xD8' + flash._addr_to_bytes(addr))
        #         tmp = flash.read_data(addr, alignment)
        #         tmp = flash.read_data(addr, alignment)
        #     data += tmp
        # print(f"Read {len(data)} bytes:") #, data.hex())
        # open('erased.bin', 'wb').write(data)
        # print(data.count(0xFF) == len(data))


        print(f"=======================================================================")
        # program
        flash.page_size = 0x20
        flash.program(start, firmware)
        print(f"=======================================================================")
        print("Check Program Result(True=Pass, False=Fail):")
        data = flash.read_data(start, firmware_size)
        # print(f"Dump {len(data)} bytes to rdback.bin")
        # open('rdback.bin', 'wb').write(data)
        print(firmware == data)

        # flash.page_size = 0x40
        # data = b''
        # for addr in range(start, start+firmware_size, flash.page_size):
        #     page_payload = firmware[addr-start:addr-start+flash.page_size]
        #     tmp = flash.read_data(addr, flash.page_size)
        #     while tmp != page_payload:
        #         print(f"Program Page 0x{addr:06X}:")
        #         flash.program_page(addr, page_payload)
        #         # with flash.we():
        #         #     flash.spi.xfer(b'\x02' + flash._addr_to_bytes(addr) + page_payload)
        #         tmp = flash.read_data(addr, flash.page_size)
        #         tmp = flash.read_data(addr, flash.page_size)
        #     data += tmp
        # print(f"Read {len(data)} bytes:") #, data.hex())
        # open('rdback.bin', 'wb').write(data)
        # print(firmware == data)
