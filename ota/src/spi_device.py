from usb_device import USBDevice
from spi_config import SPIConfigRegister
from spi_data_packet import SPIPacket
import time


class SPIDevice:
    def __init__(self, usb_dev: USBDevice, timeout: int = 1000):
        self.usb = usb_dev
        self.timeout = timeout  # 默认超时时间(ms)

    def __enter__(self):
        """支持with上下文管理"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文时自动关闭"""
        self.usb.close()

    def read_register_raw(self) -> bytes:
        # 构造读取请求
        packet = SPIPacket(command=SPIPacket.CMD_READ_REGISTER)

        # 发送请求并读取响应
        self.usb.write(packet.serialize(), self.timeout)
        return self.usb.read(SPIConfigRegister.size(), self.timeout)

    def read_register(self) -> SPIConfigRegister:
        # 反序列化为寄存器对象
        return SPIConfigRegister.from_buffer_copy(self.read_register_raw())

    def set_register_payload(
        self, spi_config: SPIConfigRegister, payload: bytes = b""
    ) -> bool:
        packet = SPIPacket(
            command=SPIPacket.CMD_SET_REGISTER,
            data=bytes(spi_config) + payload,
        )
        raw = packet.serialize()
        return self.usb.write(raw, self.timeout) == len(raw)

    def set_register(self, spi_config: SPIConfigRegister) -> bool:
        return self.set_register_payload(spi_config)

    def read_data_raw(self, nbytes: int) -> bytes:
        packet = SPIPacket(command=SPIPacket.CMD_READ_DATA)
        self.usb.write(packet.serialize(), self.timeout)
        return self.usb.read(nbytes, self.timeout)

    def reset(self) -> bool:
        config = SPIConfigRegister()
        config.TransferControlRegister.TransMode = 0x7  # No data
        config.ControlRegister.SPIRST = 1
        config.ControlRegister.RXFIFORST = 1
        config.ControlRegister.TXFIFORST = 1
        return self.set_register(config)

    def xfer(self, wr_data: bytes, rd_nbytes: int = 0, dummy: int = 0) -> bytes:
        wr_nbytes = len(wr_data)
        padding = (4 - wr_nbytes % 4) % 4
        wr_data += b"\xff" * padding

        config = SPIConfigRegister()
        if wr_nbytes == 0 and rd_nbytes == 0:
            return b""
        elif rd_nbytes == 0:
            config.TransferControlRegister.TransMode = 0x1  # Write only
            config.TransferControlRegister.WrTranCnt = wr_nbytes - 1
        elif wr_nbytes == 0:
            config.TransferControlRegister.TransMode = 0x2  # Read only
            config.TransferControlRegister.RdTranCnt = rd_nbytes - 1
        else:
            config.TransferControlRegister.RdTranCnt = rd_nbytes - 1
            config.TransferControlRegister.WrTranCnt = wr_nbytes - 1

            if dummy == 0 or dummy <= padding:
                config.TransferControlRegister.TransMode = 0x3  # Write and read
                config.TransferControlRegister.WrTranCnt += dummy
            else:
                config.TransferControlRegister.TransMode = 0x5  # Write, dummy, and read
                config.TransferControlRegister.DummyCnt = dummy - 1 - padding
                config.TransferControlRegister.WrTranCnt += padding

        # Retry mechanism for SPI transfers with timing delays
        max_retries = 3
        for retry in range(max_retries):
            try:
                assert self.set_register_payload(config, wr_data)

                if rd_nbytes:
                    data = self.read_data_raw(
                        config.TransferControlRegister.RdTranCnt + 1
                    )
                    time.sleep(0.001)  # 1ms delay for SPI to settle
                    sr = self.read_register().StatusRegister.value

                    # Check if status is acceptable (allow some variance)
                    if sr == 0x00404000 or (sr & 0x00C0C000) == 0x00404000:
                        return data
                    elif retry == max_retries - 1:
                        assert sr == 0x00404000, (
                            f"Status check failed after {max_retries} attempts, got 0x{sr:08X}"
                        )
                else:
                    time.sleep(0.001)  # 1ms delay for SPI to settle
                    sr = self.read_register().StatusRegister.value

                    if sr == 0x00404000 or (sr & 0x00C0C000) == 0x00404000:
                        return b""
                    elif retry == max_retries - 1:
                        assert sr == 0x00404000, (
                            f"Status check failed after {max_retries} attempts, got 0x{sr:08X}"
                        )

            except AssertionError:
                if retry == max_retries - 1:
                    raise
                time.sleep(0.01)  # 10ms delay before retry
                continue

        # This should never be reached
        raise RuntimeError("Unexpected code path in SPI transfer")
