import usb.core
import usb.util

class USBDevice:
    def __init__(self, vid: int, pid: int, interface_num: int = 0):
        """
        初始化USB设备
        :param vid: 厂商ID (Vendor ID)
        :param pid: 产品ID (Product ID)
        :param interface_num: 使用的接口编号（默认为0）
        """
        self.dev = usb.core.find(idVendor=vid, idProduct=pid)
        if self.dev is None:
            raise ValueError("设备未找到，请检查VID/PID或连接状态")

        self.interface_num = interface_num
        usb.util.claim_interface(self.dev, interface_num)

        # 自动探测输入输出端点
        cfg = self.dev.get_active_configuration()
        interface = cfg[(interface_num, 0)]

        # 查找批量传输端点
        self.ep_out = None
        self.ep_in = None
        for endpoint in interface:
            if usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                self.ep_out = endpoint
            elif usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_IN:
                self.ep_in = endpoint

        if None in (self.ep_out, self.ep_in):
            raise ValueError("未找到所需的输入/输出端点")

    def write(self, data: bytes, timeout: int = 1000) -> int:
        """
        发送数据到设备
        :param data: 要发送的字节数据
        :param timeout: 超时时间（毫秒）
        :return: 实际发送的字节数
        """
        return self.ep_out.write(data, timeout)

    def read(self, size: int, timeout: int = 1000) -> bytes:
        """
        从设备读取数据
        :param size: 要读取的最大字节数
        :param timeout: 超时时间（毫秒）
        :return: 读取到的字节数据
        """
        return bytes(self.ep_in.read(size, timeout))

    def close(self):
        """释放USB设备资源"""
        usb.util.release_interface(self.dev, self.interface_num)
        usb.util.dispose_resources(self.dev)

    def __enter__(self):
        """支持with上下文管理"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文时自动关闭"""
        self.close()


# 使用示例
if __name__ == "__main__":
    # 示例：连接VID=0x359F, PID=0x30F1的设备
    try:
        with USBDevice(0x359F, 0x30F1) as dev:
            # 写入数据
            # dev.write(b"\x01\x02\x03")
            
            # 读取数据
            response = dev.read(40)
            print(f"Received: {response.hex()}")
            
    except Exception as e:
        print(f"USB通信错误: {e}")