# SPI Data Packet Structure (Little-Endian):
# ---------------------------------------
# Packet Format:
# +------------+------------+------------------+
# | Command    | Data Length| Data             |
# | (4 bytes)  | (4 bytes)  | (N bytes)        |
# +------------+------------+------------------+
# 
# 所有多字节字段（Command, Data Length）均采用小端字节序存储
# 数据部分（Data）保持原始字节顺序不变
#
# 示例数据包: 
# 00 00 02 00 03 00 00 00 11 22 33
#   - Command:    00 00 02 00 (小端) → 0x00020000 (Set register)
#   - Data Length:03 00 00 00 (小端) → 0x00000003 (3 bytes)
#   - Data:       11 22 33 (原始顺序)


class SPIPacket:
    """SPI 数据包处理器 (小端字节序)"""
    
    # 命令常量
    CMD_SET_REGISTER   = 0x00020000  # 写寄存器命令
    CMD_READ_REGISTER  = 0x00020001  # 读寄存器命令
    CMD_READ_DATA      = 0x00020002  # 通用数据读取命令

    def __init__(self, command, data: bytes = b''):
        """
        初始化 SPI 数据包
        
        Args:
            command (int): 4字节命令值（主机字节序，如0x00020000）
            data (bytes): 数据部分（原始字节序）
        """
        self.command = command
        self.data = data
        
    @property
    def length(self):
        """返回数据长度（字节数）"""
        return len(self.data)
    
    def __str__(self):
        """格式化输出：命令(hex)、长度(dec)、数据(hex)"""
        command_name = {
            self.CMD_SET_REGISTER: "SET_REGISTER",
            self.CMD_READ_REGISTER: "READ_REGISTER",
            self.CMD_READ_DATA: "READ_DATA"
        }.get(self.command, f"UNKNOWN_CMD(0x{self.command:08X})")
        
        return (
            f"SPIPacket(\n"
            f"  command=0x{self.command:08X} ({command_name}),\n"
            f"  length={self.length},\n"
            f"  data={self.data.hex() if self.data else 'None'}\n"
            f")"
        )
    
    def serialize(self) -> bytes:
        """
        序列化为小端格式的SPI数据包
        
        Returns:
            bytes: 小端格式的字节流，结构为：
                   [4-byte LE command][4-byte LE length][N-byte data]
        """
        return (
            self.command.to_bytes(4, 'little') +  # 命令转小端
            self.length.to_bytes(4, 'little') +   # 长度转小端
            self.data                             # 数据保持原样
        )
    
    @classmethod
    def parse(cls, raw_data):
        """
        从小端格式的SPI数据包解析
        
        Args:
            raw_data (bytes): 接收到的原始数据（含小端字段）
            
        Returns:
            SPIPacket: 解析后的对象
            
        Raises:
            ValueError: 数据包格式无效时抛出
        """
        if len(raw_data) < 8:
            raise ValueError("数据包过短（至少需要8字节头部）")
            
        # 小端解析（注意返回值转换为主机字节序的int）
        command = int.from_bytes(raw_data[:4], 'little')
        length = int.from_bytes(raw_data[4:8], 'little')
        
        # 验证数据长度
        if len(raw_data) != 8 + length:
            raise ValueError(
                f"数据长度不匹配（声明长度={length}，实际长度={len(raw_data)-8}）"
            )
            
        return cls(command, raw_data[8:8+length])


# 示例使用
if __name__ == "__main__":
    # 示例1：构造一个设置寄存器数据包
    packet = SPIPacket(
        command=SPIPacket.CMD_SET_REGISTER,
        data=bytes.fromhex("112233")
    )
    
    # 序列化为小端字节流
    spi_data = packet.serialize()
    print(f"序列化数据: {spi_data.hex(' ')}")  
    # 输出: 00 00 02 00 03 00 00 00 11 22 33
    
    # 示例2：解析接收到的数据
    received_data = bytes.fromhex("00 00 02 00 03 00 00 00 11 22 33")
    parsed = SPIPacket.parse(received_data)

    print(parsed)
    
    # 示例3：错误检查
    try:
        SPIPacket.parse(bytes.fromhex("00 00 02 00 05 00"))  # 长度不足
    except ValueError as e:
        print(f"错误捕获: {e}")  # 数据包过短...