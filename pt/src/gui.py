"""SLogic production-test GUI (multi-product, profile-driven).

All product specifics come from resources/products/*.toml; this module is
pure UI + wiring.  See resources/README.md for the admin resource layout.

Layout: a fixed top bar (device status / product / warnings chip / operator
prompt) that never reflows, and three columns -- operation blocks numbered
in production order (1 blank-flash, 2 OTA, 4 lock, 5 re-flash + FULL TEST),
the 3 capture-test block, and logs.  Startup problems live behind the
warnings chip (click for details) instead of squeezing the controls.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

import device_watch
import pipeline as pipeline_mod
from device_watch import Mode
from profiles import (
    OUTPUT_DIR, ProductProfile, check_resources, format_rate,
    load_profiles, parse_rate,
)
from sigrok import SigrokCli, find_sigrok_binary

# workflow block colors (production order)
C_BLANK = "#1976d2"    # 1 烧空板
C_OTA = "#ef6c00"      # 2 OTA
C_TEST = "#2e7d32"     # 3 测试
C_LOCK = "#c62828"     # 4 锁定
C_REFLASH = "#00796b"  # 5 复烧
C_FULL = "#5e35b1"     # 一键全流程


def _group_style(color: str) -> str:
    return (f"QGroupBox {{ border: 2px solid {color}; border-radius: 4px;"
            f" margin-top: 7px; font-weight: bold; }}"
            f" QGroupBox::title {{ subcontrol-origin: margin; left: 6px;"
            f" padding: 0 3px; color: {color}; }}")


def _button_style(color: str) -> str:
    return (f"QPushButton {{ background: {color}; color: white;"
            f" font-weight: bold; padding: 3px 8px; border-radius: 3px; }}"
            f" QPushButton:disabled {{ background: #b8b8b8; color: #f0f0f0; }}")


def _compact(layout, spacing: int = 3, margins: tuple = (6, 8, 6, 5)):
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    return layout


class ProductionTestGUI(QWidget):
    log_signal = pyqtSignal(str)
    output_signal = pyqtSignal(str)
    step_signal = pyqtSignal(str, str)
    prompt_signal = pyqtSignal(str)
    prompt_clear_signal = pyqtSignal()
    finished_signal = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.profiles, self.problems = load_profiles()
        self.sigrok_bin = find_sigrok_binary()
        self.problems += check_resources(self.profiles, self.sigrok_bin)
        self.sigrok = SigrokCli(self.sigrok_bin) if self.sigrok_bin else None
        self.pipeline: pipeline_mod.Pipeline | None = None
        self.detected: list[device_watch.DeviceStatus] = []

        self.init_ui()
        self.log_signal.connect(self.log_box.append)
        self.output_signal.connect(self.output_box.append)
        self.step_signal.connect(self._on_step)
        self.prompt_signal.connect(self._show_prompt)
        self.prompt_clear_signal.connect(self._clear_prompt)
        self.finished_signal.connect(self._on_finished)

        if not self.profiles:
            QMessageBox.critical(
                self, "无产品档案",
                "resources/products/ 下没有可用的产品档案，请参照 resources/README.md 放置。")
        self.device_timer = QTimer(self)
        self.device_timer.timeout.connect(self.refresh_device_status)
        self.device_timer.start(1500)
        self.refresh_device_status()

    # ------------------------------------------------------------------ UI

    def init_ui(self):
        self.setWindowTitle("SLogic Production Test")
        root = QVBoxLayout()
        root.setSpacing(3)
        root.setContentsMargins(6, 4, 6, 4)

        # -- top bar: status | product | warnings chip (fixed, never reflows)
        top = QHBoxLayout()
        top.setSpacing(6)
        big = QFont(); big.setPointSize(12); big.setBold(True)
        self.device_status_label = QLabel("...")
        self.device_status_label.setFont(big)
        top.addWidget(self.device_status_label, 1)
        top.addWidget(QLabel("Product:"))
        self.product_combo = QComboBox()
        for p in self.profiles:
            self.product_combo.addItem(p.display_name, p.id)
        self.product_combo.currentIndexChanged.connect(self._on_product_changed)
        top.addWidget(self.product_combo)
        self.warn_chip = QPushButton()
        self.warn_chip.setFlat(True)
        self.warn_chip.clicked.connect(self._show_problems_dialog)
        top.addWidget(self.warn_chip)
        self._render_problem_chip()
        root.addLayout(top)

        # -- operator prompt strip: fixed height, only style changes
        self.prompt_label = QLabel("")
        self.prompt_label.setFixedHeight(24)
        self.prompt_label.setAlignment(Qt.AlignCenter)
        self._clear_prompt()
        root.addWidget(self.prompt_label)

        # -- three columns
        cols = QHBoxLayout()
        cols.setSpacing(6)
        cols.addLayout(self._build_ops_column(), 2)
        cols.addLayout(self._build_test_column(), 2)
        cols.addLayout(self._build_log_column(), 3)
        root.addLayout(cols, 1)
        self.setLayout(root)
        self._on_product_changed(self.product_combo.currentIndex())

    def _build_ops_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)

        # 1 blank flash (manifest in_pipeline steps)
        self.blank_group = QGroupBox("① 烧空板 Blank Flash")
        self.blank_group.setStyleSheet(_group_style(C_BLANK))
        self.blank_layout = _compact(QVBoxLayout())
        self.blank_group.setLayout(self.blank_layout)
        col.addWidget(self.blank_group)

        # 2 OTA
        ota_group = QGroupBox("② OTA 应用固件写入")
        ota_group.setStyleSheet(_group_style(C_OTA))
        ota = _compact(QGridLayout())
        self.ota_file_edit = QLineEdit()
        self.ota_file_edit.setPlaceholderText("默认使用产品档案固件")
        ota_select = QPushButton("Select…")
        ota_select.clicked.connect(self.select_ota_file)
        self.ota_btn = QPushButton("② OTA 烧写")
        self.ota_btn.setStyleSheet(_button_style(C_OTA))
        self.ota_btn.clicked.connect(self.run_ota)
        ota.addWidget(self.ota_file_edit, 0, 0)
        ota.addWidget(ota_select, 0, 1)
        ota.addWidget(self.ota_btn, 1, 0, 1, 2)
        ota_group.setLayout(ota)
        col.addWidget(ota_group)

        # 4 lock (manifest non-pipeline steps, e.g. eFuse lock)
        self.lock_group = QGroupBox("④ 锁定 Lock")
        self.lock_group.setStyleSheet(_group_style(C_LOCK))
        self.lock_layout = _compact(QVBoxLayout())
        self.lock_group.setLayout(self.lock_layout)
        col.addWidget(self.lock_group)

        # 5 re-flash (rework)
        reflash_group = QGroupBox("⑤ 复烧 Re-flash（返修）")
        reflash_group.setStyleSheet(_group_style(C_REFLASH))
        rf = _compact(QVBoxLayout())
        self.reflash_btn = QPushButton("⑤ 进入OTA并复烧")
        self.reflash_btn.setStyleSheet(_button_style(C_REFLASH))
        self.reflash_btn.setToolTip("等待设备进入 OTA 模式（超时提示人工操作）→ 重写应用固件 → 等待应用模式")
        self.reflash_btn.clicked.connect(self.run_reflash)
        rf.addWidget(self.reflash_btn)
        reflash_group.setLayout(rf)
        col.addWidget(reflash_group)

        # full test + cancel
        full_group = QGroupBox("一键全流程 ①→②→③")
        full_group.setStyleSheet(_group_style(C_FULL))
        fl = _compact(QVBoxLayout())
        self.full_btn = QPushButton("FULL TEST")
        full_font = QFont(); full_font.setPointSize(12); full_font.setBold(True)
        self.full_btn.setFont(full_font)
        self.full_btn.setStyleSheet(_button_style(C_FULL))
        self.full_btn.clicked.connect(self.run_full_test)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_pipeline)
        self.cancel_btn.setEnabled(False)
        self.step_label = QLabel("")
        fl.addWidget(self.full_btn)
        fl.addWidget(self.cancel_btn)
        fl.addWidget(self.step_label)
        full_group.setLayout(fl)
        col.addWidget(full_group)

        col.addStretch(1)
        return col

    def _build_test_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        test_group = QGroupBox("③ 采样测试 Capture && Verify")
        test_group.setStyleSheet(_group_style(C_TEST))
        cap = _compact(QGridLayout())
        cap.addWidget(QLabel("Channels:"), 0, 0)
        self.channel_combo = QComboBox()
        self.channel_combo.currentTextChanged.connect(self._on_channels_changed)
        cap.addWidget(self.channel_combo, 0, 1)
        cap.addWidget(QLabel("Sample Rate:"), 1, 0)
        self.rate_combo = QComboBox()
        cap.addWidget(self.rate_combo, 1, 1)
        cap.addWidget(QLabel("Samples:"), 2, 0)
        self.samples_edit = QLineEdit("1M")
        cap.addWidget(self.samples_edit, 2, 1)
        cap.addWidget(QLabel("Volt Threshold (V):"), 3, 0)
        self.volt_edit = QLineEdit("1.6")
        cap.addWidget(self.volt_edit, 3, 1)
        self.sampling_btn = QPushButton("③ SAMPLING")
        self.sampling_btn.setStyleSheet(_button_style(C_TEST))
        self.sampling_btn.clicked.connect(self.run_sampling)
        cap.addWidget(self.sampling_btn, 4, 0, 1, 2)
        cap.addWidget(QLabel("Expected (freq Hz / duty %):"), 5, 0, 1, 2)
        self.expected_table = QTableWidget()
        self.expected_table.setColumnCount(2)
        self.expected_table.setHorizontalHeaderLabels(["Freq (Hz)", "Duty (%)"])
        self.expected_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.expected_table.verticalHeader().setDefaultSectionSize(19)
        self.expected_table.verticalHeader().setFixedWidth(28)
        cap.addWidget(self.expected_table, 6, 0, 1, 2)
        test_group.setLayout(cap)
        col.addWidget(test_group, 1)
        return col

    def _build_log_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(3)
        self.verdict_label = QLabel("")
        vfont = QFont(); vfont.setPointSize(36); vfont.setBold(True)
        self.verdict_label.setFont(vfont)
        self.verdict_label.setAlignment(Qt.AlignCenter)
        self.verdict_label.setFixedHeight(60)
        col.addWidget(self.verdict_label)

        log_head = QHBoxLayout()
        log_head.addWidget(QLabel("Log:"))
        log_head.addStretch(1)
        log_clear = QPushButton("Clear"); log_clear.clicked.connect(lambda: self.log_box.clear())
        log_head.addWidget(log_clear)
        col.addLayout(log_head)
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True)
        col.addWidget(self.log_box, 2)

        out_head = QHBoxLayout()
        out_head.addWidget(QLabel("Result:"))
        out_head.addStretch(1)
        out_clear = QPushButton("Clear"); out_clear.clicked.connect(lambda: self.output_box.clear())
        out_head.addWidget(out_clear)
        col.addLayout(out_head)
        self.output_box = QTextEdit(); self.output_box.setReadOnly(True)
        col.addWidget(self.output_box, 1)
        return col

    # ------------------------------------------------- problems chip/dialog

    def _render_problem_chip(self):
        errors = sum(p.severity == "error" for p in self.problems)
        warnings = len(self.problems) - errors
        if not self.problems:
            self.warn_chip.hide()
            return
        parts = []
        if errors:
            parts.append(f"⛔ {errors} 错误")
        if warnings:
            parts.append(f"⚠ {warnings} 警告")
        self.warn_chip.setText(" / ".join(parts))
        color = "#c62828" if errors else "#b26a00"
        self.warn_chip.setStyleSheet(
            f"QPushButton {{ color: {color}; font-weight: bold;"
            f" border: 1px solid {color}; border-radius: 10px; padding: 2px 10px; }}")
        self.warn_chip.setToolTip("点击查看资源自检详情")
        self.warn_chip.show()

    def _show_problems_dialog(self):
        text = "\n\n".join(str(p) for p in self.problems) or "无问题"
        box = QMessageBox(self)
        box.setWindowTitle("资源自检")
        box.setIcon(QMessageBox.Warning if self.problems else QMessageBox.Information)
        box.setText("启动自检发现以下问题（缺失项对应的功能已禁用，补齐资源后重启生效）：")
        box.setDetailedText(text)
        box.exec_()

    # ------------------------------------------------------- profile plumbing

    @property
    def profile(self) -> ProductProfile | None:
        pid = self.product_combo.currentData()
        for p in self.profiles:
            if p.id == pid:
                return p
        return None

    def _on_product_changed(self, _idx):
        p = self.profile
        if p is None:
            return
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.addItems([str(c) for c in p.channel_options])
        self.channel_combo.setCurrentText(str(p.channel_options[-1]))
        self.channel_combo.blockSignals(False)
        self.samples_edit.setText(p.default_samples)
        self.volt_edit.setText(f"{p.voltage_threshold_v:g}")
        self.ota_file_edit.setText("")
        self.ota_file_edit.setPlaceholderText(
            str(p.app_firmware) if p.app_firmware else "档案未配置固件，请手动选择")
        self._rebuild_manifest_buttons(p)
        self._on_channels_changed(self.channel_combo.currentText())
        self._update_enablement()

    def _on_channels_changed(self, text: str):
        p = self.profile
        if p is None or not text:
            return
        ch = int(text)
        rates = p.legal_rates(ch)
        current = self.rate_combo.currentText()
        self.rate_combo.clear()
        self.rate_combo.addItems([format_rate(r) for r in rates])
        if current in [format_rate(r) for r in rates]:
            self.rate_combo.setCurrentText(current)
        else:
            self.rate_combo.setCurrentIndex(self.rate_combo.count() - 1)
        self.expected_table.setRowCount(ch)
        for row in range(ch):
            self.expected_table.setItem(row, 0, QTableWidgetItem(f"{p.expected.freq_hz:.0f}"))
            self.expected_table.setItem(row, 1, QTableWidgetItem(f"{p.expected.duty_pct:g}"))

    def _rebuild_manifest_buttons(self, p: ProductProfile):
        """1 = manifest steps with in_pipeline=true; 4 = the rest (e.g. lock)."""
        for layout in (self.blank_layout, self.lock_layout):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        self.blank_buttons: list[QPushButton] = []
        self.lock_buttons: list[QPushButton] = []
        if not p.blank_flash_steps:
            self.blank_layout.addWidget(QLabel("无 manifest（见 resources/README.md）"))
            self.lock_layout.addWidget(QLabel("无 manifest"))
            return
        for step in p.blank_flash_steps:
            in_blank = step.in_pipeline
            btn = QPushButton(("① " if in_blank else "④ ") + step.label)
            btn.setStyleSheet(_button_style(C_BLANK if in_blank else C_LOCK))
            btn.clicked.connect(lambda _, s=step: self.run_blank_step(s))
            if in_blank:
                self.blank_layout.addWidget(btn)
                self.blank_buttons.append(btn)
            else:
                self.lock_layout.addWidget(btn)
                self.lock_buttons.append(btn)
        if not self.lock_buttons:
            self.lock_layout.addWidget(QLabel("manifest 中无锁定类步骤"))
        if not self.blank_buttons:
            self.blank_layout.addWidget(QLabel("manifest 中无 in_pipeline 步骤"))

    # --------------------------------------------------------- device status

    def refresh_device_status(self):
        self.detected = device_watch.scan_devices(self.profiles)
        if self.detected:
            self.device_status_label.setText(
                "Found: " + ", ".join(str(d) for d in self.detected))
            self.device_status_label.setStyleSheet("color: green;")
        else:
            self.device_status_label.setText("No SLogic device found")
            self.device_status_label.setStyleSheet("color: red;")
        self._update_enablement()

    def _detected_mode(self, p: ProductProfile) -> Mode | None:
        for d in self.detected:
            if d.profile.id == p.id:
                return d.mode
        return None

    def _update_enablement(self):
        p = self.profile
        busy = self.pipeline is not None and self.pipeline.running
        self.cancel_btn.setEnabled(busy)
        self.product_combo.setEnabled(not busy)
        if p is None:
            for w in (self.sampling_btn, self.ota_btn, self.full_btn, self.reflash_btn):
                w.setEnabled(False)
            return
        mode = self._detected_mode(p)
        fw_ok = (p.app_firmware is not None and p.app_firmware.is_file()) \
            or bool(self.ota_file_edit.text().strip())
        self.sampling_btn.setEnabled(
            not busy and self.sigrok is not None and mode == Mode.APP)
        self.ota_btn.setEnabled(
            not busy and p.ota_pid is not None and fw_ok and mode == Mode.OTA)
        self.ota_btn.setToolTip("" if p.ota_pid is not None else "该产品 ota_pid 未配置")
        self.reflash_btn.setEnabled(not busy and p.ota_pid is not None and fw_ok)
        for btn in getattr(self, "blank_buttons", []) + getattr(self, "lock_buttons", []):
            btn.setEnabled(not busy)
        pipeline_ready = (self.sigrok is not None and p.ota_pid is not None
                          and fw_ok and p.blank_flash_steps is not None)
        self.full_btn.setEnabled(not busy and pipeline_ready)
        if not pipeline_ready:
            missing = []
            if self.sigrok is None: missing.append("sigrok-cli")
            if p.ota_pid is None: missing.append("ota_pid")
            if not fw_ok: missing.append("app 固件")
            if p.blank_flash_steps is None: missing.append("blank_flash manifest")
            self.full_btn.setToolTip("缺少: " + ", ".join(missing))
        else:
            self.full_btn.setToolTip("")

    # -------------------------------------------------------------- actions

    def _callbacks(self) -> pipeline_mod.PipelineCallbacks:
        return pipeline_mod.PipelineCallbacks(
            on_log=self.log_signal.emit,
            on_step=lambda n, st: self.step_signal.emit(n, st.value),
            on_user_prompt=self.prompt_signal.emit,
            on_user_prompt_clear=self.prompt_clear_signal.emit,
            on_finished=lambda ok, rep: self.finished_signal.emit(ok, rep))

    def _start(self, p: pipeline_mod.Pipeline):
        self.pipeline = p
        self.verdict_label.setText("")
        self.verdict_label.setStyleSheet("")
        self.step_label.setText("")
        p.start()
        self._update_enablement()

    def _override_firmware(self) -> Path | None:
        override = self.ota_file_edit.text().strip()
        return Path(override) if override else None

    def run_sampling(self):
        p = self.profile
        if p is None or self.sigrok is None:
            return
        try:
            channels = int(self.channel_combo.currentText())
            rate = parse_rate(self.rate_combo.currentText())
            volt = float(self.volt_edit.text())
            samples = self.samples_edit.text().strip() or p.default_samples
            expected_rows = []
            for row in range(channels):
                expected_rows.append((
                    float(self.expected_table.item(row, 0).text()),
                    float(self.expected_table.item(row, 1).text())))
        except (TypeError, ValueError, AttributeError) as e:
            QMessageBox.warning(self, "参数错误", str(e))
            return
        self._start(pipeline_mod.Pipeline.capture_only(
            p, self.sigrok, self._callbacks(), channels=channels,
            samplerate_hz=rate, samples=samples, voltage_threshold_v=volt,
            expected_rows=expected_rows))

    def run_ota(self):
        p = self.profile
        if p is None:
            return
        fw = self._override_firmware() or p.app_firmware
        if fw is None or not fw.is_file():
            QMessageBox.warning(self, "固件缺失", f"固件文件不存在: {fw}")
            return
        self._start(pipeline_mod.Pipeline.flash_only(p, self._callbacks(), fw))

    def run_reflash(self):
        p = self.profile
        if p is None:
            return
        fw = self._override_firmware() or p.app_firmware
        if fw is None or not fw.is_file():
            QMessageBox.warning(self, "固件缺失", f"固件文件不存在: {fw}")
            return
        self.output_box.append(f"===== ⑤ 复烧: {p.display_name} =====")
        self._start(pipeline_mod.Pipeline.reflash(p, self._callbacks(), fw))

    def run_blank_step(self, step):
        p = self.profile
        if p is None:
            return
        pl = pipeline_mod.Pipeline(p, None, self._callbacks(), [])
        pl.steps = [pipeline_mod.StepDef(
            f"blank:{step.name}", lambda: pl._run_blank_step(step))]
        self._start(pl)

    def run_full_test(self):
        p = self.profile
        if p is None or self.sigrok is None:
            return
        pl = pipeline_mod.Pipeline.full_test(p, self.sigrok, self._callbacks())
        if (fw := self._override_firmware()) is not None:
            pl._firmware_override = fw
        self.output_box.append(f"===== FULL TEST: {p.display_name} =====")
        self._start(pl)

    def cancel_pipeline(self):
        if self.pipeline is not None:
            self.pipeline.request_cancel()
            self.log_signal.emit("已请求取消…")

    def select_ota_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select firmware.bin", "", "BIN Files (*.bin)")
        if path:
            self.ota_file_edit.setText(path)
            self._update_enablement()

    # -------------------------------------------------------------- slots

    def _on_step(self, name: str, status: str):
        self.step_label.setText(f"Step: {name} [{status}]")

    def _show_prompt(self, text: str):
        self.prompt_label.setText(text)
        self.prompt_label.setStyleSheet(
            "background:#ffec99;color:#7a5c00;font-weight:bold;"
            "padding:4px;border-radius:4px;")

    def _clear_prompt(self):
        self.prompt_label.setText("")
        self.prompt_label.setStyleSheet("background:transparent;")

    def _on_finished(self, ok: bool, report: str):
        if report:
            self.output_box.append(report)
        self.verdict_label.setText("PASS" if ok else "FAIL")
        self.verdict_label.setStyleSheet(
            "color: white; background:#1e9e33; border-radius:8px;" if ok
            else "color: white; background:#c62828; border-radius:8px;")
        self.pipeline = None
        self._update_enablement()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    gui = ProductionTestGUI()
    gui.resize(1180, 700)
    gui.show()
    sys.exit(app.exec_())
