"""SLogic production-test station GUI (multi-product, profile-driven).

Test-executive paradigm:

- The LEFT side is a single linear TEST SEQUENCE list built from the
  product profile -- the same steps drive both modes: START runs them all
  in order (automated), and every row has its own run button (manual,
  step-by-step).  Each row live-updates status (待执行/执行中/等待操作/
  通过/失败) and duration.
- The RIGHT side is a detail pane with three tabs: 运行日志 (live log),
  测试参数 (custom capture parameters + expected table for engineering
  debug), 结果报告 (structured session report with one-click copy so the
  operator can paste it into an issue/feedback channel).
- The BOTTOM is a full-width state banner: 待机 / 运行中 / 等待操作
  (operator prompts) / PASS / FAIL + failure summary.

All product specifics come from resources/products/*.toml.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PyQt5.QtCore import QModelIndex, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import device_watch
import pipeline as pipeline_mod
from device_watch import Mode
from pipeline import StepStatus, sequence_plan
from profiles import (
    OUTPUT_DIR, ProductProfile, check_resources, format_rate,
    load_profiles, parse_rate,
)
from sigrok import SigrokCli, find_sigrok_binary

ACCENT = "#1565c0"

STATUS_STYLE = {
    StepStatus.PENDING: ("○", "#9e9e9e", "待执行"),
    StepStatus.RUNNING: ("▶", "#1565c0", "执行中…"),
    StepStatus.WAITING_USER: ("⏳", "#b26a00", "等待操作"),
    StepStatus.PASSED: ("✔", "#2e7d32", "通过"),
    StepStatus.FAILED: ("✘", "#c62828", "失败"),
    StepStatus.SKIPPED: ("−", "#9e9e9e", "跳过"),
}


class ExpectedTable(QTableWidget):
    """Edit only on explicit intent: double-click / click-on-selected / F2.
    A click on the blank area drops the current cell so stray keystrokes
    can't silently overwrite the last-clicked value."""

    def __init__(self):
        super().__init__()
        self.setEditTriggers(QAbstractItemView.DoubleClicked
                             | QAbstractItemView.SelectedClicked
                             | QAbstractItemView.EditKeyPressed)

    def mousePressEvent(self, event):
        if not self.indexAt(event.pos()).isValid():
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
        super().mousePressEvent(event)


class StepRow(QFrame):
    """One row of the test sequence: status glyph, number+name, duration,
    per-step run button (manual mode)."""

    def __init__(self, index: int, step_id: str, label: str, on_run):
        super().__init__()
        self.step_id = step_id
        self.label_text = label
        self._t_start: float | None = None
        self.duration_s: float | None = None
        self.status = StepStatus.PENDING

        self.setFrameShape(QFrame.StyledPanel)
        lay = QHBoxLayout()
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(6)
        self.icon = QLabel()
        self.icon.setFixedWidth(20)
        self.icon.setAlignment(Qt.AlignCenter)
        self.name = QLabel(f"{index}. {label}")
        self.info = QLabel("")
        self.info.setStyleSheet("color:#777;")
        self.run_btn = QPushButton("▶")
        self.run_btn.setFixedSize(30, 26)
        self.run_btn.setToolTip(f"单步执行：{label}")
        self.run_btn.clicked.connect(lambda: on_run(step_id))
        lay.addWidget(self.icon)
        lay.addWidget(self.name, 1)
        lay.addWidget(self.info)
        lay.addWidget(self.run_btn)
        self.setLayout(lay)
        self.set_status(StepStatus.PENDING)

    def reset(self):
        self._t_start = None
        self.duration_s = None
        self.set_status(StepStatus.PENDING)
        self.info.setText("")

    def set_status(self, status: StepStatus):
        if status == StepStatus.RUNNING and self._t_start is None:
            self._t_start = time.time()
        if status in (StepStatus.PASSED, StepStatus.FAILED) and self._t_start:
            self.duration_s = time.time() - self._t_start
            self.info.setText(f"{self.duration_s:.1f}s")
        self.status = status
        glyph, color, text = STATUS_STYLE[status]
        self.icon.setText(glyph)
        self.icon.setStyleSheet(f"color:{color}; font-weight:bold;")
        self.name.setStyleSheet(f"color:{color};" if status != StepStatus.PENDING else "")
        bg = {"passed": "#edf7ee", "failed": "#fdecea",
              "running": "#e8f0fb", "waiting_user": "#fff8e1"}.get(status.value, "")
        self.setStyleSheet(f"QFrame {{ background:{bg or 'transparent'};"
                           f" border:1px solid #d0d0d0; border-radius:4px; }}")
        self.setToolTip(text)


class ProductionTestGUI(QWidget):
    log_signal = pyqtSignal(str)
    step_signal = pyqtSignal(str, object)
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
        self.step_rows: dict[str, StepRow] = {}
        self._session_t0: float | None = None
        self._prompt_text = ""

        self.init_ui()
        self.log_signal.connect(self.log_box.append)
        self.step_signal.connect(self._on_step)
        self.prompt_signal.connect(self._on_prompt)
        self.prompt_clear_signal.connect(self._on_prompt_clear)
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
        root.setSpacing(5)
        root.setContentsMargins(8, 6, 8, 6)
        root.addLayout(self._build_header())
        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self._build_sequence_panel(), 2)
        body.addWidget(self._build_detail_panel(), 3)
        root.addLayout(body, 1)
        root.addWidget(self._build_banner())
        self.setLayout(root)
        self._on_product_changed(self.product_combo.currentIndex())

    def _build_header(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(8)
        h.addWidget(QLabel("产品:"))
        self.product_combo = QComboBox()
        for p in self.profiles:
            self.product_combo.addItem(p.display_name, p.id)
        self.product_combo.currentIndexChanged.connect(self._on_product_changed)
        h.addWidget(self.product_combo)

        self.device_status_label = QLabel("…")
        f = QFont(); f.setBold(True)
        self.device_status_label.setFont(f)
        h.addWidget(self.device_status_label, 1)

        self.warn_chip = QPushButton()
        self.warn_chip.setFlat(True)
        self.warn_chip.clicked.connect(self._show_problems_dialog)
        h.addWidget(self.warn_chip)
        self._render_problem_chip()

        self.start_btn = QPushButton("▶ 一键全流程")
        f2 = QFont(); f2.setPointSize(f2.pointSize() + 1); f2.setBold(True)
        self.start_btn.setFont(f2)
        self.start_btn.setStyleSheet(
            f"QPushButton {{ background:{ACCENT}; color:white; padding:5px 18px;"
            f" border-radius:4px; }} QPushButton:disabled {{ background:#b8b8b8; }}")
        self.start_btn.clicked.connect(self.run_full_test)
        h.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setStyleSheet(
            "QPushButton { background:#c62828; color:white; padding:5px 12px;"
            " border-radius:4px; } QPushButton:disabled { background:#b8b8b8; }")
        self.stop_btn.clicked.connect(self.cancel_pipeline)
        self.stop_btn.setEnabled(False)
        h.addWidget(self.stop_btn)
        return h

    def _build_sequence_panel(self) -> QWidget:
        panel = QGroupBox("测试序列（自动按序执行，或点 ▶ 单步）")
        panel.setStyleSheet(
            f"QGroupBox {{ border:1px solid #bbb; border-radius:4px; margin-top:10px;"
            f" font-weight:bold; }} QGroupBox::title {{ subcontrol-origin:margin;"
            f" left:8px; padding:0 3px; color:{ACCENT}; }}")
        outer = QVBoxLayout()
        outer.setContentsMargins(4, 8, 4, 6)
        self.seq_container = QVBoxLayout()
        self.seq_container.setSpacing(3)
        holder = QWidget()
        holder.setLayout(self.seq_container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        aux_head = QLabel("辅助操作（不计入序列结果）")
        aux_head.setStyleSheet("color:#666; font-weight:bold; margin-top:4px;")
        outer.addWidget(aux_head)
        self.aux_layout = QHBoxLayout()
        self.aux_layout.setSpacing(4)
        outer.addLayout(self.aux_layout)
        panel.setLayout(outer)
        return panel

    def _build_detail_panel(self) -> QWidget:
        self.tabs = QTabWidget()

        # tab 1: live log
        log_page = QWidget(); v = QVBoxLayout(); v.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        self.step_label = QLabel(""); self.step_label.setStyleSheet("color:#555;")
        head.addWidget(self.step_label); head.addStretch(1)
        clear = QPushButton("清空"); clear.clicked.connect(lambda: self.log_box.clear())
        head.addWidget(clear)
        v.addLayout(head)
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True)
        v.addWidget(self.log_box, 1)
        log_page.setLayout(v)
        self.tabs.addTab(log_page, "运行日志")

        # tab 2: engineering parameters / custom capture
        param_page = QWidget(); pv = QVBoxLayout(); pv.setContentsMargins(4, 4, 4, 4)
        cap = QGridLayout(); cap.setSpacing(4)
        cap.addWidget(QLabel("通道数:"), 0, 0)
        self.channel_combo = QComboBox()
        self.channel_combo.currentTextChanged.connect(self._on_channels_changed)
        cap.addWidget(self.channel_combo, 0, 1)
        cap.addWidget(QLabel("采样率:"), 0, 2)
        self.rate_combo = QComboBox()
        cap.addWidget(self.rate_combo, 0, 3)
        cap.addWidget(QLabel("采样点数:"), 1, 0)
        self.samples_edit = QLineEdit("1M")
        cap.addWidget(self.samples_edit, 1, 1)
        cap.addWidget(QLabel("电压阈值(V):"), 1, 2)
        self.volt_edit = QLineEdit("1.6")
        cap.addWidget(self.volt_edit, 1, 3)
        pv.addLayout(cap)
        self.sampling_btn = QPushButton("▶ 自定义采样验证（工程调试）")
        self.sampling_btn.setStyleSheet(
            "QPushButton { background:#2e7d32; color:white; font-weight:bold;"
            " padding:4px; border-radius:4px; }"
            " QPushButton:disabled { background:#b8b8b8; }")
        self.sampling_btn.clicked.connect(self.run_sampling)
        pv.addWidget(self.sampling_btn)
        pv.addWidget(QLabel("每通道期望值（双击修改；序列步骤使用产品档案值）:"))
        self.expected_table = ExpectedTable()
        self.expected_table.setColumnCount(2)
        self.expected_table.setHorizontalHeaderLabels(["Freq (Hz)", "Duty (%)"])
        self.expected_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.expected_table.verticalHeader().setDefaultSectionSize(24)
        self.expected_table.verticalHeader().setFixedWidth(34)
        pv.addWidget(self.expected_table, 1)
        fw_row = QHBoxLayout()
        fw_row.addWidget(QLabel("固件覆盖:"))
        self.ota_file_edit = QLineEdit()
        self.ota_file_edit.setPlaceholderText("默认使用产品档案固件")
        fw_btn = QPushButton("…"); fw_btn.setFixedWidth(30)
        fw_btn.clicked.connect(self.select_ota_file)
        fw_row.addWidget(self.ota_file_edit, 1)
        fw_row.addWidget(fw_btn)
        pv.addLayout(fw_row)
        param_page.setLayout(pv)
        self.tabs.addTab(param_page, "测试参数")

        # tab 3: session report
        report_page = QWidget(); rv = QVBoxLayout(); rv.setContentsMargins(4, 4, 4, 4)
        rhead = QHBoxLayout()
        rhead.addWidget(QLabel("本次会话报告（可直接粘贴反馈）:"))
        rhead.addStretch(1)
        self.copy_report_btn = QPushButton("📋 复制报告")
        self.copy_report_btn.clicked.connect(self.copy_report)
        rhead.addWidget(self.copy_report_btn)
        rv.addLayout(rhead)
        self.report_box = QTextEdit(); self.report_box.setReadOnly(True)
        rv.addWidget(self.report_box, 1)
        report_page.setLayout(rv)
        self.tabs.addTab(report_page, "结果报告")
        return self.tabs

    def _build_banner(self) -> QLabel:
        self.banner = QLabel("待机")
        bf = QFont(); bf.setPointSize(20); bf.setBold(True)
        self.banner.setFont(bf)
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setFixedHeight(52)
        self._set_banner("idle", "待机 — 选择产品并连接设备")
        return self.banner

    def _set_banner(self, kind: str, text: str):
        colors = {
            "idle": ("#eceff1", "#555"),
            "running": ("#e8f0fb", ACCENT),
            "waiting": ("#fff3cd", "#7a5c00"),
            "pass": ("#1e9e33", "white"),
            "fail": ("#c62828", "white"),
        }
        bg, fg = colors[kind]
        self.banner.setText(text)
        self.banner.setStyleSheet(
            f"background:{bg}; color:{fg}; border-radius:6px; padding:2px;")

    # ------------------------------------------------- problems chip/dialog

    def _render_problem_chip(self):
        errors = sum(p.severity == "error" for p in self.problems)
        warnings = len(self.problems) - errors
        if not self.problems:
            self.warn_chip.hide()
            return
        parts = ([f"⛔ {errors} 错误"] if errors else []) + \
                ([f"⚠ {warnings} 警告"] if warnings else [])
        self.warn_chip.setText(" / ".join(parts))
        color = "#c62828" if errors else "#b26a00"
        self.warn_chip.setStyleSheet(
            f"QPushButton {{ color: {color}; font-weight: bold;"
            f" border: 1px solid {color}; border-radius: 10px; padding: 2px 10px; }}")
        self.warn_chip.setToolTip("点击查看资源自检详情")
        self.warn_chip.show()

    def _show_problems_dialog(self):
        box = QMessageBox(self)
        box.setWindowTitle("资源自检")
        box.setIcon(QMessageBox.Warning if self.problems else QMessageBox.Information)
        box.setText("启动自检发现以下问题（缺失项对应功能已禁用，补齐资源后重启生效）：")
        box.setDetailedText("\n\n".join(str(p) for p in self.problems) or "无问题")
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
        self._rebuild_sequence(p)
        self._rebuild_aux(p)
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
        self._on_channels_changed(self.channel_combo.currentText())
        self._update_enablement()

    def _rebuild_sequence(self, p: ProductProfile):
        while self.seq_container.count():
            item = self.seq_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.step_rows.clear()
        plan = sequence_plan(p)
        if not plan:
            self.seq_container.addWidget(QLabel("档案未定义任何步骤"))
        for i, (sid, label) in enumerate(plan, start=1):
            row = StepRow(i, sid, label, self.run_single_step)
            self.seq_container.addWidget(row)
            self.step_rows[sid] = row
        self.seq_container.addStretch(1)

    def _rebuild_aux(self, p: ProductProfile):
        while self.aux_layout.count():
            item = self.aux_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.aux_buttons: list[QPushButton] = []
        for step in (p.blank_flash_steps or []):
            if step.in_pipeline:
                continue
            btn = QPushButton(f"🔒 {step.label}")
            btn.setToolTip("产线终检操作，谨慎执行")
            btn.clicked.connect(lambda _, s=step: self.run_manifest_step(s))
            self.aux_layout.addWidget(btn)
            self.aux_buttons.append(btn)
        self.reflash_btn = QPushButton("♻ 复烧（返修）")
        self.reflash_btn.setToolTip("等待设备进入 OTA 模式（超时提示人工操作）→ 重写应用固件 → 等待应用模式")
        self.reflash_btn.clicked.connect(self.run_reflash)
        self.aux_layout.addWidget(self.reflash_btn)
        self.aux_buttons.append(self.reflash_btn)
        self.aux_layout.addStretch(1)

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

    # --------------------------------------------------------- device status

    def refresh_device_status(self):
        self.detected = device_watch.scan_devices(self.profiles)
        if self.detected:
            self.device_status_label.setText(
                "设备: " + ", ".join(str(d) for d in self.detected))
            self.device_status_label.setStyleSheet("color: #2e7d32;")
        else:
            self.device_status_label.setText("未检测到 SLogic 设备")
            self.device_status_label.setStyleSheet("color: #c62828;")
        self._update_enablement()

    def _detected_mode(self, p: ProductProfile) -> Mode | None:
        for d in self.detected:
            if d.profile.id == p.id:
                return d.mode
        return None

    def _update_enablement(self):
        p = self.profile
        busy = self.pipeline is not None and self.pipeline.running
        self.stop_btn.setEnabled(busy)
        self.product_combo.setEnabled(not busy)
        if p is None:
            self.start_btn.setEnabled(False)
            self.sampling_btn.setEnabled(False)
            return
        fw_ok = (p.app_firmware is not None and p.app_firmware.is_file()) \
            or bool(self.ota_file_edit.text().strip())
        seq_ready = (self.sigrok is not None and p.ota_pid is not None
                     and fw_ok and p.blank_flash_steps is not None)
        self.start_btn.setEnabled(not busy and seq_ready)
        if not seq_ready:
            missing = []
            if self.sigrok is None: missing.append("sigrok-cli")
            if p.ota_pid is None: missing.append("ota_pid")
            if not fw_ok: missing.append("app 固件")
            if p.blank_flash_steps is None: missing.append("blank_flash manifest")
            self.start_btn.setToolTip("缺少: " + ", ".join(missing))
        else:
            self.start_btn.setToolTip("")
        for sid, row in self.step_rows.items():
            if sid.startswith("capture"):
                ok = self.sigrok is not None
            elif sid in ("flash_app",):
                ok = p.ota_pid is not None and fw_ok
            elif sid == "wait_ota":
                ok = p.ota_pid is not None
            else:
                ok = True
            row.run_btn.setEnabled(not busy and ok)
        for btn in getattr(self, "aux_buttons", []):
            btn.setEnabled(not busy)
        if hasattr(self, "reflash_btn"):
            self.reflash_btn.setEnabled(not busy and p.ota_pid is not None and fw_ok)
        self.sampling_btn.setEnabled(
            not busy and self.sigrok is not None
            and self._detected_mode(p) == Mode.APP)

    # -------------------------------------------------------------- actions

    def _callbacks(self) -> pipeline_mod.PipelineCallbacks:
        return pipeline_mod.PipelineCallbacks(
            on_log=self.log_signal.emit,
            on_step=lambda sid, st: self.step_signal.emit(sid, st),
            on_user_prompt=self.prompt_signal.emit,
            on_user_prompt_clear=self.prompt_clear_signal.emit,
            on_finished=lambda ok, rep: self.finished_signal.emit(ok, rep))

    def _start(self, pl: pipeline_mod.Pipeline, *, reset_rows: bool):
        if self.pipeline is not None and self.pipeline.running:
            return
        self.pipeline = pl
        self._session_t0 = time.time()
        if reset_rows:
            for row in self.step_rows.values():
                row.reset()
        else:
            for step in pl.steps:
                if step.id in self.step_rows:
                    self.step_rows[step.id].reset()
        self._set_banner("running", "运行中…")
        self.tabs.setCurrentIndex(0)
        pl.start()
        self._update_enablement()

    def _override_firmware(self) -> Path | None:
        override = self.ota_file_edit.text().strip()
        return Path(override) if override else None

    def run_full_test(self):
        p = self.profile
        if p is None or self.sigrok is None:
            return
        self.log_signal.emit(f"===== 一键全流程: {p.display_name} =====")
        self._start(pipeline_mod.Pipeline.full_test(
            p, self.sigrok, self._callbacks(), self._override_firmware()),
            reset_rows=True)

    def run_single_step(self, step_id: str):
        p = self.profile
        if p is None:
            return
        row = self.step_rows.get(step_id)
        self.log_signal.emit(f"===== 单步执行: {row.label_text if row else step_id} =====")
        self._start(pipeline_mod.Pipeline.single_step(
            p, self.sigrok, self._callbacks(), step_id, self._override_firmware()),
            reset_rows=False)

    def run_manifest_step(self, step):
        p = self.profile
        if p is None:
            return
        self.log_signal.emit(f"===== 辅助操作: {step.label} =====")
        self._start(pipeline_mod.Pipeline.manifest_step(p, self._callbacks(), step),
                    reset_rows=False)

    def run_reflash(self):
        p = self.profile
        if p is None:
            return
        fw = self._override_firmware() or p.app_firmware
        if fw is None or not fw.is_file():
            QMessageBox.warning(self, "固件缺失", f"固件文件不存在: {fw}")
            return
        self.log_signal.emit(f"===== 复烧（返修）: {p.display_name} =====")
        self._start(pipeline_mod.Pipeline.reflash(p, self._callbacks(), fw),
                    reset_rows=False)

    def run_sampling(self):
        p = self.profile
        if p is None or self.sigrok is None:
            return
        try:
            channels = int(self.channel_combo.currentText())
            rate = parse_rate(self.rate_combo.currentText())
            volt = float(self.volt_edit.text())
            samples = self.samples_edit.text().strip() or p.default_samples
            expected_rows = [
                (float(self.expected_table.item(r, 0).text()),
                 float(self.expected_table.item(r, 1).text()))
                for r in range(channels)]
        except (TypeError, ValueError, AttributeError) as e:
            QMessageBox.warning(self, "参数错误", str(e))
            return
        self.log_signal.emit(f"===== 自定义采样: {channels}ch@{format_rate(rate)} =====")
        self._start(pipeline_mod.Pipeline.capture_only(
            p, self.sigrok, self._callbacks(), channels=channels,
            samplerate_hz=rate, samples=samples, voltage_threshold_v=volt,
            expected_rows=expected_rows), reset_rows=False)

    def cancel_pipeline(self):
        if self.pipeline is not None:
            self.pipeline.request_cancel()
            self.log_signal.emit("已请求停止…")

    def select_ota_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择固件", "", "BIN Files (*.bin)")
        if path:
            self.ota_file_edit.setText(path)
            self._update_enablement()

    def copy_report(self):
        QApplication.clipboard().setText(self.report_box.toPlainText())
        self.copy_report_btn.setText("已复制 ✔")
        QTimer.singleShot(1500, lambda: self.copy_report_btn.setText("📋 复制报告"))

    # -------------------------------------------------------------- slots

    def _on_step(self, sid: str, status: StepStatus):
        row = self.step_rows.get(sid)
        if row is not None:
            row.set_status(status)
        if status == StepStatus.RUNNING:
            label = row.label_text if row else sid
            self.step_label.setText(f"当前步骤: {label}")
            if not self._prompt_text:
                self._set_banner("running", f"运行中 — {label}")

    def _on_prompt(self, text: str):
        self._prompt_text = text
        self._set_banner("waiting", f"⏳ {text}")

    def _on_prompt_clear(self):
        self._prompt_text = ""

    def _on_finished(self, ok: bool, report: str):
        elapsed = time.time() - self._session_t0 if self._session_t0 else 0
        fail_steps = [r.label_text for r in self.step_rows.values()
                      if r.status == StepStatus.FAILED]
        if ok:
            self._set_banner("pass", f"PASS  ({elapsed:.1f}s)")
        else:
            summary = f"；失败步骤: {', '.join(fail_steps)}" if fail_steps else ""
            self._set_banner("fail", f"FAIL  ({elapsed:.1f}s){summary}")
        self._render_report(ok, report, elapsed)
        self.step_label.setText("")
        self.pipeline = None
        self._update_enablement()

    def _render_report(self, ok: bool, detail: str, elapsed: float):
        p = self.profile
        lines = [
            "==== SLogic 产测报告 ====",
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"产品: {p.display_name} ({p.id})" if p else "产品: -",
            f"设备: {', '.join(str(d) for d in self.detected) or '未检测到'}",
            f"总结果: {'PASS' if ok else 'FAIL'}  (耗时 {elapsed:.1f}s)",
            "",
            "-- 步骤状态 --",
        ]
        for row in self.step_rows.values():
            _, _, text = STATUS_STYLE[row.status]
            dur = f" ({row.duration_s:.1f}s)" if row.duration_s else ""
            lines.append(f"[{text}] {row.name.text()}{dur}")
        if detail:
            lines += ["", "-- 详情 --", detail]
        lines += ["", "如需反馈问题，请复制本报告并附上运行日志相关片段。"]
        self.report_box.setPlainText("\n".join(lines))
        if not ok:
            self.tabs.setCurrentIndex(2)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    font = app.font()
    font.setPointSize(font.pointSize() + 2)
    app.setFont(font)
    gui = ProductionTestGUI()
    gui.resize(1280, 780)
    gui.show()
    sys.exit(app.exec_())
