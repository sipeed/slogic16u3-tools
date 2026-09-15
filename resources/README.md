# resources/ 资源目录约定

本目录由**管理员**按以下约定放置文件，产测 GUI 启动时自检并提示缺失项。
新增产品 = 新增一份 `products/<product_id>.toml` + 放置对应固件与刷机脚本，**无需修改任何代码**。

```
resources/
├── products/                  # 产品档案（TOML，一产品一文件，文件名 = product_id）
│   ├── slogic16u3.toml
│   └── slogic32u3.toml
├── firmware/<product_id>/
│   └── app.bin                # 应用固件（OTA 模式下经 SPI Flash 写入）
├── blank_flash/<product_id>/
│   ├── manifest.toml          # 空板刷机命令清单（步骤名/argv/超时/是否进一键流程）
│   └── *.sh / *.bat / 工具    # manifest 引用的脚本与资源，相对路径按本目录解析
└── bin/                       # sigrok-cli 按平台命名放置（拷贝或软链接均可）
    ├── sigrok-cli-linux-x86_64        # Linux: AppImage 重命名/软链
    ├── sigrok-cli-windows-x86_64.exe  # Windows
    └── sigrok-cli-macos-arm64         # macOS: zip 解压后的可执行文件
```

## 放置示例（Linux 工位）

```bash
# sigrok-cli（来自 SLogic 发布包）
ln -sf /path/to/sigrok-cli-SLogic-linux-x86_64-musl.AppImage resources/bin/sigrok-cli-linux-x86_64

# 应用固件
cp firmware_v1.2.3.bin resources/firmware/slogic16u3/app.bin

# 空板刷机脚本（原 /home/sipeed007/gowin/scripts/ 下三个脚本）
cp efuse_lock.sh gowin_flash.sh usb_rst.sh resources/blank_flash/slogic16u3/
```

## 启动警告 → 解决办法对照

GUI 顶栏"⚠ 警告"角标里的每一条都对应一个待放置/待确认项，补齐后重启即消：

| 警告 | 解决办法 |
|---|---|
| `usb.ota_pid 未配置` | 硬件确认该产品 OTA 模式 PID 后填入 `products/<id>.toml` 的 `[usb] ota_pid` |
| `应用固件缺失: .../app.bin` | 把应用固件放到 `firmware/<id>/app.bin` |
| `blank_flash manifest 缺失` | 在 `blank_flash/<id>/` 创建 `manifest.toml`（可复制 slogic16u3 的模板） |
| `步骤 'xxx' 引用的文件缺失` | 把 manifest argv 引用的脚本/工具放入同目录（如从产线机 `/home/sipeed007/gowin/scripts/` 迁入） |
| `未找到 sigrok-cli 二进制` | 按平台命名放入 `bin/`（见上文） |

## 跨平台（Linux / Windows）

- manifest 每个步骤的 `argv` 为通用命令；提供 `argv_linux` / `argv_windows` /
  `argv_darwin` 时对应平台优先使用，因此同一份资源包可同时服务两种产线工位：
  Linux 放 `.sh`，Windows 放 `.bat`/`.exe`，互不干扰；
- 烧录（flash）一步可用 **openFPGALoader**（开源、跨平台、支持 Gowin）替代
  Gowin programmer——把对应平台的可执行文件与 OTA 位流一起放进
  `blank_flash/<id>/`，随资源包整体分发；**eFuse Lock 无开源替代**，仍需
  Gowin 官方工具（Linux/Windows 均有 CLI），由工位环境提供；
- Windows 工位需 libusb 环境（WinUSB 驱动/Zadig 与 `libusb-1.0.dll`），与
  旧产测环境要求一致。

## 打包分发

`python build.py`（PyInstaller）产出单二进制，与本 `resources/` 目录平级摆放：

```
SLogicPT/
├── slogic-pt-linux-x86_64        # 或 slogic-pt-windows-x86_64.exe
├── resources/                    # 本目录整体拷贝，管理员随时可改，无需重新打包
└── out/                          # 运行时自动生成（采样数据）
```

## 产品档案要点

- `usb.ota_pid` 未知时可先不写，GUI 会提示 "OTA 未配置" 并禁用 OTA 与一键流程，其余功能不受影响；
- `capture.samplerates` 必须填 sigrok 驱动实际支持的档位（`sigrok-cli -d <driver> --show` 查询），
  GUI 会再按 `channels × rate / 8 ≤ max_bandwidth_mbps` 过滤展示；
- `firmware.app_flash_addr` 因产品而异，填错会导致固件无法启动；
- `[[capture.tests]]` 是"一键全流程"逐组执行的测试点。
