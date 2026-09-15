from .usb_device import USBDevice
from .spi_config import SPIConfigRegister
from .spi_data_packet import SPIPacket

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
        packet = SPIPacket(
            command=SPIPacket.CMD_READ_REGISTER
        )

        # 发送请求并读取响应
        self.usb.write(packet.serialize(), self.timeout)
        return self.usb.read(SPIConfigRegister.size(), self.timeout)

    def read_register(self) -> SPIConfigRegister:
        # 反序列化为寄存器对象
        return SPIConfigRegister.from_buffer_copy(self.read_register_raw())

    def set_register_payload(self, spi_config: SPIConfigRegister, payload: bytes = b'') -> bool:
        packet = SPIPacket(
            command=SPIPacket.CMD_SET_REGISTER,
            data=bytes(spi_config) + payload,
        )
        raw = packet.serialize()
        return self.usb.write(raw, self.timeout) == len(raw)

    def set_register(self, spi_config: SPIConfigRegister) -> bool:
        return self.set_register_payload(spi_config)

    def read_data_raw(self, nbytes: int) -> bytes:
        packet = SPIPacket(
            command=SPIPacket.CMD_READ_DATA
        )
        self.usb.write(packet.serialize(), self.timeout)
        return self.usb.read(nbytes, self.timeout)

    def _assert_idle(self) -> None:
        """校验桥接器完成一次传输后处于可继续状态：
        SPIActive=0（无进行中的传输）且 RXEMPTY=1（RX FIFO 已空——残留会让后续
        读取错位并最终 Overflow）。StatusRegister 本身不含错误位（溢出/欠载等错误在
        InterruptStatusRegister），TX FIFO 占用计数是瞬时值：36 字节页写入时 TX FIFO
        尚未排空即被采样属正常，故不作错误判据（旧代码硬比 0x00404000 会误判失败）。"""
        st = self.read_register().StatusRegister
        assert st.SPIActive == 0 and st.RXEMPTY == 1, \
            f"SPI 桥接器状态异常: 0x{st.value:08X}"

    def reset(self) -> bool:
        config = SPIConfigRegister()
        config.TransferControlRegister.TransMode = 0x7 # No data
        config.ControlRegister.SPIRST = 1
        config.ControlRegister.RXFIFORST = 1
        config.ControlRegister.TXFIFORST = 1
        return self.set_register(config)

    def xfer(self, wr_data: bytes, rd_nbytes: int=0, dummy: int=0) -> bytes:
        wr_nbytes = len(wr_data)
        padding = (4 - wr_nbytes % 4) % 4
        wr_data += b'\xff' * padding

        config = SPIConfigRegister()
        if wr_nbytes == 0 and rd_nbytes == 0:
            return b''
        elif rd_nbytes == 0:
            config.TransferControlRegister.TransMode = 0x1 # Write only
            config.TransferControlRegister.WrTranCnt = wr_nbytes -1
        elif wr_nbytes == 0:
            config.TransferControlRegister.TransMode = 0x2 # Read only
            config.TransferControlRegister.RdTranCnt = rd_nbytes -1
        else:
            config.TransferControlRegister.RdTranCnt = rd_nbytes -1
            config.TransferControlRegister.WrTranCnt = wr_nbytes -1

            if dummy == 0 or dummy <= padding:
                config.TransferControlRegister.TransMode = 0x3 # Write and read
                config.TransferControlRegister.WrTranCnt += dummy
            else:
                config.TransferControlRegister.TransMode = 0x5 # Write, dummy, and read
                config.TransferControlRegister.DummyCnt = dummy - 1 - padding
                config.TransferControlRegister.WrTranCnt += padding

        assert(self.set_register_payload(config, wr_data))
        if rd_nbytes:
            data = self.read_data_raw(config.TransferControlRegister.RdTranCnt + 1)
            self._assert_idle()
            return data
        else:
            self._assert_idle()
            return b''
