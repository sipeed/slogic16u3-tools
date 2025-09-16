from ctypes import (
    c_uint32,
    Structure, Union,
    sizeof
)

class Register(Union):
    class Bits(Structure):
        defaults: dict[str, int] = {}

        def __init__(self):
            super().__init__()
            for field, *_ in self._fields_:
                setattr(self, field, self.defaults.get(field, 0))

        def __str__(self):
            fields = []
            for name, _, *rest in self._fields_:
                value = getattr(self, name)
                # 单比特字段用十进制，多位字段或 Reserved 用十六进制
                bit_length = rest[0] if rest else 0
                fmt = f"{name}={value:#x}" if bit_length > 1 or name.startswith("Reserved") else f"{name}={value}"
                fields.append(fmt)
            return f"({', '.join(fields)})"

    _anonymous_ = ("bits",)  # 允许直接访问位字段

    def __init__(self):
        super().__init__()
        self.bits.__init__()

    def __str__(self):
        return f"{self.__class__.__name__}(value=0x{self.value:08x}, Bits={self.bits})"


# TransferFormatRegister 定义
class TransferFormatRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("CPHA", c_uint32, 1),          # Bit 0: Clock phase
            ("CPOL", c_uint32, 1),          # Bit 1: Clock polarity
            ("SlvMode", c_uint32, 1),       # Bit 2: Slave mode
            ("LSB", c_uint32, 1),           # Bit 3: Bit order
            ("MOSIBiDir", c_uint32, 1),     # Bit 4: MOSI bidirectional mode
            ("Reserved6_5", c_uint32, 2),   # Bits 6:5: Reserved
            ("DataMerge", c_uint32, 1),     # Bit 7: Data merge mode
            ("DataLen", c_uint32, 5),       # Bits 12:8: Data unit length
            ("Reserved15_13", c_uint32, 3), # Bits 15:13: Reserved
            ("AddrLen", c_uint32, 2),       # Bits 17:16: Address length
            ("Reserved31_18", c_uint32, 14) # Bits 31:18: Reserved
        ]
        defaults = {
            "AddrLen": 0x2,
            "DataLen": 0x7,
            "DataMerge": 1
        }
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


# TransferControlRegister 定义
class TransferControlRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("RdTranCnt", c_uint32, 9),    # Bits 8:0: Read transfer count
            ("DummyCnt", c_uint32, 2),     # Bits 10:9: Dummy count
            ("TokenValue", c_uint32, 1),   # Bit 11: Token value
            ("WrTranCnt", c_uint32, 9),    # Bits 20:12: Write transfer count
            ("TokenEn", c_uint32, 1),      # Bit 21: Token enable
            ("DualQuad", c_uint32, 2),     # Bits 23:22: Dual/Quad mode
            ("TransMode", c_uint32, 4),    # Bits 27:24: Transfer mode
            ("AddrFmt", c_uint32, 1),      # Bit 28: Address format
            ("AddrEn", c_uint32, 1),       # Bit 29: Address enable
            ("CmdEn", c_uint32, 1),        # Bit 30: Command enable
            ("SlvDataOnly", c_uint32, 1),  # Bit 31: Slave data only
        ]
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class CommandRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("CMD", c_uint32, 8),          # Bits 7:0: SPI Command
            ("Reserved31_8", c_uint32, 24) # Bits 31:8: Reserved
        ]
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class AddressRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("ADDR", c_uint32, 32),       # Bits 31:0: SPI Address (Master mode only)
        ]
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class DataRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("DATA", c_uint32, 32),       # Bits 31:0: SPI data
        ]
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class ControlRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("SPIRST", c_uint32, 1),      # Bit 0: SPI reset (auto-clearing)
            ("RXFIFORST", c_uint32, 1),    # Bit 1: RX FIFO reset (auto-clearing)
            ("TXFIFORST", c_uint32, 1),    # Bit 2: TX FIFO reset (auto-clearing)
            ("RXDMAEN", c_uint32, 1),      # Bit 3: RX DMA enable
            ("TXDMAEN", c_uint32, 1),      # Bit 4: TX DMA enable
            ("Reserved7_5", c_uint32, 3),  # Bits 7:5: Reserved
            ("RXTHRES", c_uint32, 8),      # Bits 15:8: RX FIFO threshold
            ("TXTHRES", c_uint32, 8),      # Bits 23:16: TX FIFO threshold
            ("Reserved31_24", c_uint32, 8) # Bits 31:24: Reserved
        ]
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]

class StatusRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("SPIActive", c_uint32, 1),      # Bit 0: SPI active status
            ("Reserved7_1", c_uint32, 7),    # Bits 7:1: Reserved
            ("RXNUM_low", c_uint32, 6),      # Bits 13:8: RXNUM[5:0]
            ("RXEMPTY", c_uint32, 1),        # Bit 14: RX FIFO empty
            ("RXFULL", c_uint32, 1),         # Bit 15: RX FIFO full
            ("TXNUM_low", c_uint32, 6),      # Bits 21:16: TXNUM[5:0]
            ("TXEMPTY", c_uint32, 1),        # Bit 22: TX FIFO empty
            ("TXFULL", c_uint32, 1),         # Bit 23: TX FIFO full
            ("Reserved25_24", c_uint32, 2),   # Bits 25:24: RXNUM[7:6]
            ("Reserved27_26", c_uint32, 2),   # Bits 27:26: Reserved
            ("TXNUM_high", c_uint32, 2),     # Bits 29:28: TXNUM[7:6]
            ("Reserved31_30", c_uint32, 2)    # Bits 31:30: Reserved
        ]
        defaults = {
            "TXEMPTY": 0x1,
            "RXEMPTY": 0x1
        }
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class InterruptEnableRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("RXFIFOORIntEn", c_uint32, 1),  # Bit 0: RX FIFO overrun interrupt enable
            ("TXFIFOURIntEn", c_uint32, 1),   # Bit 1: TX FIFO underrun interrupt enable
            ("RXFIFOIntEn", c_uint32, 1),     # Bit 2: RX FIFO threshold interrupt enable
            ("TXFIFOIntEn", c_uint32, 1),     # Bit 3: TX FIFO threshold interrupt enable
            ("EndIntEn", c_uint32, 1),        # Bit 4: Transfer end interrupt enable
            ("SlvCmdEn", c_uint32, 1),        # Bit 5: Slave command interrupt enable
            ("Reserved31_6", c_uint32, 26)    # Bits 31:6: Reserved
        ]
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class InterruptStatusRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("RXFIFOORInt", c_uint32, 1),  # Bit 0: RX FIFO overrun interrupt (W1C)
            ("TXFIFOURInt", c_uint32, 1),   # Bit 1: TX FIFO underrun interrupt (W1C)
            ("RXFIFOInt", c_uint32, 1),    # Bit 2: RX FIFO threshold interrupt (W1C)
            ("TXFIFOInt", c_uint32, 1),    # Bit 3: TX FIFO threshold interrupt (W1C)
            ("EndInt", c_uint32, 1),       # Bit 4: Transfer end interrupt (W1C)
            ("SlvCmdInt", c_uint32, 1),    # Bit 5: Slave command interrupt (W1C)
            ("Reserved31_6", c_uint32, 26) # Bits 31:6: Reserved
        ]
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]

class TimingRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("SCLK_DIV", c_uint32, 8),      # Bits 7:0: Clock divider (实际有效位)
            ("CSHT", c_uint32, 4),          # Bits 11:8: CS hold time
            ("CS2SCLK", c_uint32, 2),       # Bits 13:12: CS to SCLK delay
            ("Reserved31_14", c_uint32, 18)  # Bits 31:14: Reserved
        ]
        defaults = {
            "SCLK_DIV": 0xff,
            "CSHT": 0x2
        }
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class ConfigurationRegister(Register):
    class Bits(Register.Bits):
        _fields_ = [
            ("RxFIFOSize", c_uint32, 4),    # Bits 3:0: RX FIFO深度
            ("TxFIFOSize", c_uint32, 4),    # Bits 7:4: TX FIFO深度
            ("DualSPI", c_uint32, 1),      # Bit 8: 支持Dual SPI
            ("QuadSPI", c_uint32, 1),      # Bit 9: 支持Quad SPI
            ("Reserved10", c_uint32, 1),    # Bit 10: 保留
            ("DirectIO", c_uint32, 1),      # Bit 11: 支持直接IO
            ("AHBMem", c_uint32, 1),        # Bit 12: 支持AHB总线内存映射
            ("EILMMem", c_uint32, 1),       # Bit 13: 支持EILM总线内存映射
            ("Slave", c_uint32, 1),         # Bit 14: 支持从模式
            ("Reserved31_15", c_uint32, 17) # Bits 31:15: 保留
        ]
        defaults = {
            "RxFIFOSize": 0x6,
            "TxFIFOSize": 0x6
        }
    _fields_ = [
        ("bits", Bits),
        ("value", c_uint32)
    ]


class SPIConfigRegister(Union):
    class Bits(Structure):
        _fields_ = [
            ("ControlRegister", ControlRegister),
            ("TransferFormatRegister", TransferFormatRegister),
            ("TransferControlRegister", TransferControlRegister),
            ("InterruptEnableRegister", InterruptEnableRegister),
            ("TimingRegister", TimingRegister),
            ("InterruptStatusRegister", InterruptStatusRegister),
            ("ConfigurationRegister", ConfigurationRegister),
            ("StatusRegister", StatusRegister),
            ("AddressRegister", AddressRegister),
            ("CommandRegister", CommandRegister)
        ]

        def __init__(self):
            for field, *_ in self._fields_:
                getattr(self, field).__init__()

        def __str__(self):
            reg_info = []
            for reg_name, reg_type in self._fields_:
                reg = getattr(self, reg_name)
                reg_info.append(f"{reg_name}: {reg.value:08x}")
            return "\n".join(reg_info)

    _anonymous_ = ("bits",)  # 允许直接访问位字段
    _fields_ = [
        ("bits", Bits),
        ("reg_array", c_uint32*10)
    ]

    def __init__(self):
        super().__init__()
        self.bits.__init__()

    @classmethod
    def size(cls) -> int:
        return sizeof(cls)

    def memory_view(self) -> str:
        """返回寄存器组的原始内存视图"""
        return "\n".join(f"Reg[{i}]: 0x{self.reg_array[i]:08x}" 
                        for i in range(10))

    def __str__(self):
        sections = []
        for reg_name, reg_type in self.bits._fields_:
            reg = getattr(self.bits, reg_name)
            sections.append(f"{reg}")
        
        return "\n".join(sections) + f"\n\nRaw Memory Dump:\n{self.memory_view()}"


# 示例使用
if __name__ == "__main__":
    tfr = TransferFormatRegister()
    print(tfr)
    tfr.LSB = 1
    print(tfr)

    cr = CommandRegister()
    print(cr)
    cr.CMD = 0x9f
    print(cr)

    sr = StatusRegister()
    print(sr)

    spiconfig = SPIConfigRegister()
    print(bytes(spiconfig))
    print(spiconfig)

    raw='00000000800702000000000000000000000000000000000066000000004040000000000000000000'
    print(SPIConfigRegister.from_buffer_copy(bytes.fromhex(raw)))