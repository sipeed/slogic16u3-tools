# TODO

## 资源放置（消除 GUI 启动警告）— 交付负责同事

- [ ] `resources/firmware/slogic16u3/app.bin` — 16U3 应用固件
- [ ] `resources/firmware/slogic32u3/app.bin` — 32U3 应用固件
- [ ] `resources/blank_flash/slogic16u3/` 三个脚本：`gowin_flash.sh`、`usb_rst.sh`、`efuse_lock.sh`
      （从产线机 `/home/sipeed007/gowin/scripts/` 迁入；flash 步骤也可改用 openFPGALoader，
      manifest 中有示例）
- [ ] `resources/blank_flash/slogic32u3/` 同上三个脚本 + OTA 位流（manifest 已建好）
- [ ] Windows 工位如需：上述脚本的 `.bat` 版本（manifest 已写好 `argv_windows`）

## 固件 / 驱动侧

- [ ] **32U3 分组模式输入前端全零**：切 `logic_channels=16/8/4` 后真实引脚信号全零
      （阈值 0.3–3.0V 扫描无效；`pattern=Emulation` 数据完整 → 数据通路正常，
      故障定位在分组模式的引脚采样/输入 mux）。修复后把 `slogic32u3.toml` 中
      注释掉的 16ch@400M、8ch@800M 测试点启用
- [ ] **sigrok 驱动实现 `conn=` 设备选择**（当前 "Not supported now!"）：
      多台 SLogic 并接时 sigrok-cli 无法采集；工具已预留 conn 调用通路
- [ ] 16U3 回插后实测 8ch@400M 分组（16U3 是否也有分组全零问题）

## 工具 / 工位侧

- [ ] Windows 机器上跑 `python build.py` 出 exe 并验证（PyInstaller 不支持交叉打包）；
      工位需 WinUSB 驱动（Zadig）+ `libusb-1.0.dll`
- [ ] 产线首板走一遍一键全流程（空板 → DFU → 应用 → 采样 PASS）并连续多板复跑
