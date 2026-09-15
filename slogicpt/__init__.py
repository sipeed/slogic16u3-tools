"""SLogic 系列逻辑分析仪产线产测工具（数据驱动多产品）。

启动 GUI:      python -m slogicpt
DFU 烧写 CLI:  python -m slogicpt.dfu.spi_flash firmware.bin [--pid 0x30F1 --addr 0x0]
模块自测:      python -m slogicpt.profiles / .waveform / .pipeline / .blank_flash / .device_watch / .sigrok
产品档案与资源: resources/（见 resources/README.md）
"""
