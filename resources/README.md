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

## 产品档案要点

- `usb.ota_pid` 未知时可先不写，GUI 会提示 "OTA 未配置" 并禁用 OTA 与一键流程，其余功能不受影响；
- `capture.samplerates` 必须填 sigrok 驱动实际支持的档位（`sigrok-cli -d <driver> --show` 查询），
  GUI 会再按 `channels × rate / 8 ≤ max_bandwidth_mbps` 过滤展示；
- `firmware.app_flash_addr` 因产品而异，填错会导致固件无法启动；
- `[[capture.tests]]` 是"一键全流程"逐组执行的测试点。
