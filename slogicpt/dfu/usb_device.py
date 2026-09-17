import time

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
        # 与设备检测共用同一 libusb 后端：Windows 优先加载内置的
        # resources/bin/libusb-1.0.dll（见 device_watch.libusb_backend），
        # 否则系统查找。旧代码在此自建后端且回退分支传目录名，Windows 无
        # 系统 libusb 时 DFU 烧写必失败。
        from ..device_watch import libusb_backend
        backend = libusb_backend()
        if backend is None:
            raise ValueError(
                "未找到 libusb 后端（libusb-1.0.dll）：请放入 resources/bin/ "
                "或安装系统 libusb-1.0")

        self.interface_num = interface_num

        # 上一次会话若异常中断，DFU 端点会残留未读响应/处于 stall：
        # 下一笔 bulk 写报 [Errno 5]，下一笔读按精确长度收包报 [Errno 75] Overflow。
        # 实测唯一可靠的复位手段是 dev.reset() + 重枚举，clear_halt/排空都不足以清干净。
        # 因此每次建链前先做一次全设备复位（幂等、无害），复位后端点行为完全正常。
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is None:
            raise ValueError("设备未找到，请检查VID/PID或连接状态")
        try:
            dev.reset()  # 复位后设备重枚举；某些内核会抛 "No such device" 但已生效
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(dev)
        time.sleep(2.0)  # 等待重枚举完成

        self.dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if self.dev is None:
            raise ValueError("复位后未找到设备，请检查连接状态")

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
        # 批量 IN 端点上，若申请长度不是 wMaxPacketSize 的整数倍，设备发来一个
        # 满包就会触发 [Errno 75] Overflow。按整包倍数申请缓冲再截取实际长度，
        # 是 libusb 上规避该问题的标准做法。
        mps = self.ep_in.wMaxPacketSize
        buf_size = ((size + mps - 1) // mps) * mps
        data = bytes(self.ep_in.read(buf_size, timeout))
        return data[:size]

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