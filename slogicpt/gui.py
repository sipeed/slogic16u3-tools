"""SLogic production-test station GUI (multi-product, profile-driven).

Test-executive paradigm:

- The LEFT side is a single linear TEST SEQUENCE list built from the
  product profile -- the same steps drive both modes: START runs them all
  in order (automated), and every row has its own run button (manual,
  step-by-step).  Each row live-updates status (pending/running/waiting/
  pass/fail) and duration.
- The RIGHT side stacks two always-visible panes (no tabs): the result
  report on top and the live run log on bottom (like the old two-window
  layout).  Custom capture parameters (for engineering debug) live behind
  a ⚙ gear on the sampling step; the language selector sits in the header,
  and resource-path overrides + one-time environment setup sit under the
  left-panel auxiliary section.
- The BOTTOM is a full-width state banner: standby / running / waiting
  (operator prompts) / PASS / FAIL + failure summary.

All product specifics come from resources/products/*.toml.

UI strings are bilingual (see slogicpt.i18n): English literals are the
source keys, Chinese is looked up in slogicpt._translations. Wrap only
constant literals in t(); interpolate outside via .format().
"""
from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from pathlib import Path

from PyQt5.QtCore import QModelIndex, QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from . import device_watch
from . import env_setup
from . import pipeline as pipeline_mod
from . import programmer
from .device_watch import Mode
from .i18n import LANGS, get_language, set_language, t
from .pipeline import StepStatus, sequence_plan
from .profiles import (
    OUTPUT_DIR, ProductProfile, check_resources, format_rate,
    load_profiles, parse_rate,
)
from .sigrok import SigrokCli, find_sigrok_binary

ACCENT = "#1565c0"

# STATUS_STYLE stores the English text key; translate at the read site with
# t(...) so a live language switch (UI rebuild) picks up the new language.
STATUS_STYLE = {
    StepStatus.PENDING: ("○", "#9e9e9e", "Pending"),
    StepStatus.RUNNING: ("▶", "#1565c0", "Running…"),
    StepStatus.WAITING_USER: ("⏳", "#b26a00", "Waiting"),
    StepStatus.PASSED: ("✔", "#2e7d32", "Pass"),
    StepStatus.FAILED: ("✘", "#c62828", "Fail"),
    StepStatus.SKIPPED: ("−", "#9e9e9e", "Skip"),
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

    def __init__(self, index: int, step_id: str, label: str, on_run,
                 on_settings=None):
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
        self.run_btn.setToolTip(t("Single step: {label}").format(label=label))
        self.run_btn.clicked.connect(lambda: on_run(step_id))
        lay.addWidget(self.icon)
        lay.addWidget(self.name, 1)
        lay.addWidget(self.info)
        # optional ⚙ gear (e.g. custom capture params on the sampling step)
        # opens a popup; only shown when the row supplies a handler.
        if on_settings is not None:
            self.settings_btn = QPushButton("⚙")
            self.settings_btn.setFixedSize(30, 26)
            self.settings_btn.setToolTip(t("Test Params"))
            self.settings_btn.clicked.connect(lambda: on_settings(step_id))
            lay.addWidget(self.settings_btn)
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
        self.setToolTip(t(text))


class ProductionTestGUI(QWidget):
    log_signal = pyqtSignal(str)
    step_signal = pyqtSignal(str, object)
    prompt_signal = pyqtSignal(str)
    prompt_clear_signal = pyqtSignal()
    manual_switch_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)
    probe_signal = pyqtSignal(object)
    env_signal = pyqtSignal(bool, str)   # (ok, action)  action in {deploy,remove}

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
        # external JTAG programmer state, driven by the manual 🔄 scan button
        # (None = not scanned yet); separate from USB DFU/APP auto-scan.
        self.programmer_present: bool | None = None
        self.efuse_status = "unknown"       # unknown | unlocked | locked
        self.probe_cable: int | None = None
        self._rescan_after = False          # auto re-probe after a lock succeeds
        self._switch_popup: QMessageBox | None = None  # manual-switch popup (non-modal)

        self.init_ui()
        # Signals connect ONCE here to stable forwarding slots; a language
        # switch rebuilds the central widget (new log_box/report_box objects),
        # so the slots must dereference self.<widget> at call time, never bind
        # to a specific widget instance.
        self.log_signal.connect(self._append_log)
        self.step_signal.connect(self._on_step)
        self.prompt_signal.connect(self._on_prompt)
        self.prompt_clear_signal.connect(self._on_prompt_clear)
        self.manual_switch_signal.connect(self._on_manual_switch)
        self.finished_signal.connect(self._on_finished)
        self.probe_signal.connect(self._on_probe)
        self.env_signal.connect(self._on_env_done)
        self._refresh_env_buttons()

        if not self.profiles:
            QMessageBox.critical(
                self, t("No product profiles"),
                t("No usable product profiles under resources/products/; "
                  "see resources/README.md to add them."))
        self.device_timer = QTimer(self)
        self.device_timer.timeout.connect(self.refresh_device_status)
        self.device_timer.start(1500)
        self.refresh_device_status()

    # ------------------------------------------------------------------ UI

    def init_ui(self):
        # A single outer layout holds one swappable central widget so the whole
        # UI can be torn down and rebuilt in the new language (see _rebuild_ui).
        self.setWindowTitle("SLogic Production Test")
        self._outer = QVBoxLayout()
        self._outer.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self._outer)
        self._central = self._build_central()
        self._outer.addWidget(self._central)
        self._on_product_changed(self.product_combo.currentIndex())

    def _build_central(self) -> QWidget:
        central = QWidget()
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
        central.setLayout(root)
        self._build_param_dialog()   # off-screen popup; ⚙ on the sampling step
        return central

    def _rebuild_ui(self):
        """Tear down and rebuild the central widget in the current language,
        preserving operator-visible state (product, overrides, log, report)."""
        prod_idx = self.product_combo.currentIndex()
        fw = self.fw_file_edit.text()
        dfu = self.dfu_file_edit.text()
        ekey = self.ekey_file_edit.text()
        log_text = self.log_box.toPlainText()
        report_text = self.report_box.toPlainText()

        self._outer.removeWidget(self._central)
        self._central.deleteLater()
        if getattr(self, "_param_dialog", None) is not None:
            self._param_dialog.deleteLater()   # top-level popup, not in _central
        self._central = self._build_central()
        self._outer.addWidget(self._central)

        # restore state (block signals so restoring the product index doesn't
        # fire _on_product_changed before we invoke it explicitly below)
        self.product_combo.blockSignals(True)
        self.product_combo.setCurrentIndex(prod_idx)
        self.product_combo.blockSignals(False)
        self.fw_file_edit.setText(fw)
        self.dfu_file_edit.setText(dfu)
        self.ekey_file_edit.setText(ekey)
        if log_text:
            self.log_box.setPlainText(log_text)
        if report_text:
            self.report_box.setPlainText(report_text)
        self._on_product_changed(prod_idx)
        self.refresh_device_status()
        self._refresh_env_buttons()
        self._render_problem_chip()
        self._update_enablement()

    def _build_header(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(8)
        self.scan_btn = QPushButton("🔄")
        self.scan_btn.setToolTip(
            t("Scan the external programmer (JTAG) and read the eFuse lock state"))
        self.scan_btn.setFixedWidth(36)
        self.scan_btn.clicked.connect(self.run_probe)
        h.addWidget(self.scan_btn)
        h.addWidget(QLabel(t("Product:")))
        self.product_combo = QComboBox()
        for p in self.profiles:
            self.product_combo.addItem(p.display_name, p.id)
        self.product_combo.currentIndexChanged.connect(self._on_product_changed)
        h.addWidget(self.product_combo)

        self.device_status_label = QLabel("…")
        f = QFont(); f.setBold(True)
        self.device_status_label.setFont(f)
        h.addWidget(self.device_status_label, 1)

        # language selector (moved here from the old config tab); switching
        # rebuilds the whole UI when idle -- see _on_language_changed.  Set the
        # current index BEFORE connecting so restoring it doesn't fire the slot.
        self.lang_combo = QComboBox()
        for code, name in LANGS:
            self.lang_combo.addItem(name, code)
        cur = self.lang_combo.findData(get_language())
        if cur >= 0:
            self.lang_combo.setCurrentIndex(cur)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        h.addWidget(QLabel("🌐"))
        h.addWidget(self.lang_combo)

        self.warn_chip = QPushButton()
        self.warn_chip.setFlat(True)
        self.warn_chip.clicked.connect(self._show_problems_dialog)
        h.addWidget(self.warn_chip)
        self._render_problem_chip()

        self.start_btn = QPushButton("▶ " + t("Run Full Test"))
        f2 = QFont(); f2.setPointSize(f2.pointSize() + 1); f2.setBold(True)
        self.start_btn.setFont(f2)
        self.start_btn.setStyleSheet(
            f"QPushButton {{ background:{ACCENT}; color:white; padding:5px 18px;"
            f" border-radius:4px; }} QPushButton:disabled {{ background:#b8b8b8; }}")
        self.start_btn.clicked.connect(self.run_full_test)
        h.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■ " + t("Stop"))
        self.stop_btn.setStyleSheet(
            "QPushButton { background:#c62828; color:white; padding:5px 12px;"
            " border-radius:4px; } QPushButton:disabled { background:#b8b8b8; }")
        self.stop_btn.clicked.connect(self.cancel_pipeline)
        self.stop_btn.setEnabled(False)
        h.addWidget(self.stop_btn)
        return h

    def _build_sequence_panel(self) -> QWidget:
        panel = QGroupBox(t("Test Sequence (auto in order, or ▶ per step)"))
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

        aux_head = QLabel(t("Auxiliary (not counted in sequence result)"))
        aux_head.setStyleSheet("color:#666; font-weight:bold; margin-top:4px;")
        outer.addWidget(aux_head)
        self.aux_layout = QHBoxLayout()
        self.aux_layout.setSpacing(4)
        outer.addLayout(self.aux_layout)

        # resource-path overrides + one-time environment setup live under the
        # auxiliary section (moved out of the old config tab).
        outer.addWidget(self._build_resource_group())
        env_box = self._build_env_group()
        if env_box is not None:
            outer.addWidget(env_box)

        panel.setLayout(outer)
        return panel

    def _build_detail_panel(self) -> QWidget:
        """Right side: two always-visible panes (no tabs) -- result report on
        top, live run log on bottom, like the old two-window layout."""
        split = QSplitter(Qt.Vertical)

        # top: session report
        report_page = QWidget(); rv = QVBoxLayout(); rv.setContentsMargins(4, 4, 4, 4)
        rhead = QHBoxLayout()
        rhead.addWidget(QLabel(t("Session report (paste directly into feedback):")))
        rhead.addStretch(1)
        self.copy_report_btn = QPushButton("📋 " + t("Copy Report"))
        self.copy_report_btn.clicked.connect(self.copy_report)
        rhead.addWidget(self.copy_report_btn)
        rv.addLayout(rhead)
        self.report_box = QTextEdit(); self.report_box.setReadOnly(True)
        rv.addWidget(self.report_box, 1)
        report_page.setLayout(rv)
        split.addWidget(report_page)

        # bottom: live log
        log_page = QWidget(); v = QVBoxLayout(); v.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        head.addWidget(QLabel(t("Run Log")))   # top-left title, mirrors the report pane
        self.step_label = QLabel(""); self.step_label.setStyleSheet("color:#555;")
        head.addWidget(self.step_label); head.addStretch(1)
        clear = QPushButton(t("Clear")); clear.clicked.connect(lambda: self.log_box.clear())
        head.addWidget(clear)
        v.addLayout(head)
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True)
        v.addWidget(self.log_box, 1)
        log_page.setLayout(v)
        split.addWidget(log_page)

        split.setStretchFactor(0, 2)   # report
        split.setStretchFactor(1, 3)   # log gets a bit more room
        return split

    def _build_resource_group(self) -> QGroupBox:
        """资源路径覆盖：显示产品档案解析出的路径（placeholder），可临时改用别的
        文件（仅本次会话生效，不写回 TOML）。三项：app 固件 / DFU 镜像 / eFuse 密钥。"""
        res_box = QGroupBox(
            t("Resource Path Overrides (blank = use profile; this session only)"))
        res_grid = QGridLayout()
        res_grid.setContentsMargins(6, 4, 6, 4)
        res_grid.setVerticalSpacing(3)
        self.fw_file_edit = self._add_resource_row(
            res_grid, 0, t("app firmware:"), self.select_fw_file)
        self.dfu_file_edit = self._add_resource_row(
            res_grid, 1, t("DFU image:"), self.select_dfu_file)
        self.ekey_file_edit = self._add_resource_row(
            res_grid, 2, t("eFuse key:"), self.select_ekey_file)
        res_grid.setColumnStretch(1, 1)
        res_box.setLayout(res_grid)
        return res_box

    def _build_env_group(self) -> QGroupBox | None:
        """环境准备（一次性）：本平台的先决条件一键部署/移除——Linux=udev 设备权限；
        Windows=FTDI A 通道 WinUSB 驱动。其它平台返回 None（不显示该组）。"""
        if not env_setup.requirement():
            return None
        env_box = QGroupBox(
            t("Environment Setup (one-time) · {name}").format(name=env_setup.title()))
        eg = QGridLayout(); eg.setContentsMargins(6, 4, 6, 4)
        self.env_hint = QLabel(env_setup.hint())
        self.env_hint.setWordWrap(True)
        self.env_hint.setStyleSheet("color:#555;")
        eg.addWidget(self.env_hint, 0, 0, 1, 2)
        self.env_deploy_btn = QPushButton(t("Deploy"))
        self.env_deploy_btn.setStyleSheet(
            "QPushButton { background:#2e7d32; color:white; font-weight:bold;"
            " padding:4px 14px; border-radius:4px; }"
            " QPushButton:disabled { background:#b8b8b8; }")
        self.env_deploy_btn.clicked.connect(lambda: self._run_env_setup("deploy"))
        self.env_remove_btn = QPushButton(t("Remove"))
        self.env_remove_btn.setStyleSheet(
            "QPushButton { padding:4px 14px; border-radius:4px; }"
            " QPushButton:disabled { color:#999; }")
        self.env_remove_btn.clicked.connect(lambda: self._run_env_setup("remove"))
        eg.addWidget(self.env_deploy_btn, 1, 0)
        eg.addWidget(self.env_remove_btn, 1, 1)
        eg.setColumnStretch(0, 1)
        eg.setColumnStretch(1, 1)
        env_box.setLayout(eg)
        return env_box

    def _build_param_dialog(self) -> None:
        """Build the (hidden) custom-capture parameters popup opened by the ⚙
        gear on the sampling step.  Built as part of _build_central so its
        widgets exist for _on_product_changed and are rebuilt (retranslated)
        on a language switch."""
        dlg = QDialog(self)
        dlg.setWindowTitle(t("Test Params"))
        dlg.setModal(False)
        pv = QVBoxLayout(); pv.setContentsMargins(8, 8, 8, 8)
        cap = QGridLayout(); cap.setSpacing(4)
        cap.addWidget(QLabel(t("Channels:")), 0, 0)
        self.channel_combo = QComboBox()
        self.channel_combo.currentTextChanged.connect(self._on_channels_changed)
        cap.addWidget(self.channel_combo, 0, 1)
        cap.addWidget(QLabel(t("Sample Rate:")), 0, 2)
        self.rate_combo = QComboBox()
        cap.addWidget(self.rate_combo, 0, 3)
        cap.addWidget(QLabel(t("Samples:")), 1, 0)
        self.samples_edit = QLineEdit("1M")
        cap.addWidget(self.samples_edit, 1, 1)
        cap.addWidget(QLabel(t("Voltage Threshold (V):")), 1, 2)
        self.volt_edit = QLineEdit("1.6")
        cap.addWidget(self.volt_edit, 1, 3)
        pv.addLayout(cap)
        self.sampling_btn = QPushButton("▶ " + t("Custom Capture Verify (engineering)"))
        self.sampling_btn.setStyleSheet(
            "QPushButton { background:#2e7d32; color:white; font-weight:bold;"
            " padding:4px; border-radius:4px; }"
            " QPushButton:disabled { background:#b8b8b8; }")
        self.sampling_btn.clicked.connect(self.run_sampling)
        pv.addWidget(self.sampling_btn)
        pv.addWidget(QLabel(
            t("Per-channel expected values (double-click to edit; "
              "sequence steps use profile values):")))
        self.expected_table = ExpectedTable()
        self.expected_table.setColumnCount(2)
        self.expected_table.setHorizontalHeaderLabels(["Freq (Hz)", "Duty (%)"])
        self.expected_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.expected_table.verticalHeader().setDefaultSectionSize(24)
        self.expected_table.verticalHeader().setFixedWidth(34)
        pv.addWidget(self.expected_table, 1)
        dlg.setLayout(pv)
        dlg.resize(460, 420)
        self._param_dialog = dlg

    def _open_params(self, _sid: str | None = None) -> None:
        """Show the custom-capture params popup (⚙ on the sampling step)."""
        self._param_dialog.show()
        self._param_dialog.raise_()
        self._param_dialog.activateWindow()

    def _build_banner(self) -> QLabel:
        self.banner = QLabel(t("Standby"))
        bf = QFont(); bf.setPointSize(20); bf.setBold(True)
        self.banner.setFont(bf)
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setFixedHeight(52)
        self._set_banner("idle", t("Standby — select a product and connect the device"))
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
        parts = (["⛔ " + t("{n} errors").format(n=errors)] if errors else []) + \
                (["⚠ " + t("{n} warnings").format(n=warnings)] if warnings else [])
        self.warn_chip.setText(" / ".join(parts))
        color = "#c62828" if errors else "#b26a00"
        self.warn_chip.setStyleSheet(
            f"QPushButton {{ color: {color}; font-weight: bold;"
            f" border: 1px solid {color}; border-radius: 10px; padding: 2px 10px; }}")
        self.warn_chip.setToolTip(t("Click for resource self-check details"))
        self.warn_chip.show()

    def _show_problems_dialog(self):
        box = QMessageBox(self)
        box.setWindowTitle(t("Resource Self-check"))
        box.setIcon(QMessageBox.Warning if self.problems else QMessageBox.Information)
        box.setText(t("Startup self-check found the following issues (missing items "
                      "disable the related function; add resources and restart to "
                      "take effect):"))
        box.setDetailedText("\n\n".join(str(p) for p in self.problems) or t("No issues"))
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
        # a different product means a different chip/cable -- the last scan's
        # programmer/eFuse verdict no longer applies, so clear it.
        self.programmer_present = None
        self.efuse_status = "unknown"
        self.probe_cable = None
        self._rebuild_sequence(p)
        self._rebuild_aux(p)
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.addItems([str(c) for c in p.channel_options])
        self.channel_combo.setCurrentText(str(p.default_channels))
        self.channel_combo.blockSignals(False)
        self.samples_edit.setText(p.default_samples)
        self.volt_edit.setText(f"{p.voltage_threshold_v:g}")
        # 资源路径覆盖：清空覆盖、placeholder 显示档案解析出的当前路径
        prog = p.programmer
        dfu_img = prog.flash.image if (prog and prog.flash) else None
        ekey = prog.efuse_key_file if prog else None
        for edit, path, empty_hint in (
                (self.fw_file_edit, p.app_firmware,
                 t("Profile has no firmware; please select manually")),
                (self.dfu_file_edit, dfu_img, t("Profile has no DFU image")),
                (self.ekey_file_edit, ekey, t("Profile has no eFuse key"))):
            edit.setText("")
            edit.setPlaceholderText(str(path) if path else empty_hint)
        self._on_channels_changed(self.channel_combo.currentText())
        # profile-declared default rate for the default channel count
        default_rate = format_rate(p.default_samplerate_hz)
        if self.rate_combo.findText(default_rate) >= 0:
            self.rate_combo.setCurrentText(default_rate)
        self.refresh_device_status()   # status text is per selected product

    def _rebuild_sequence(self, p: ProductProfile):
        while self.seq_container.count():
            item = self.seq_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.step_rows.clear()
        plan = sequence_plan(p)
        if not plan:
            self.seq_container.addWidget(QLabel(t("Profile defines no steps")))
        for i, (sid, label) in enumerate(plan, start=1):
            # the sampling/capture step carries a ⚙ gear that opens the custom
            # capture-params popup (the old "Test Params" tab).
            on_settings = self._open_params if sid.startswith("capture") else None
            row = StepRow(i, sid, label, self.run_single_step, on_settings)
            self.seq_container.addWidget(row)
            self.step_rows[sid] = row
        self.seq_container.addStretch(1)

    def _rebuild_aux(self, p: ProductProfile):
        while self.aux_layout.count():
            item = self.aux_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # 需外置 JTAG 烧录器的辅助按钮（由 _update_enablement 按 programmer_present
        # 门控）。注：eFuse 写锁不再是独立按钮——它是烧空板的前置步骤（加密 DFU
        # 须先写入 AES 密钥才能启动），由 blank:efuse 步骤自动完成；锁定状态显示
        # 在设备状态栏前缀（refresh_device_status）。
        self.programmer_aux_buttons: list[QPushButton] = []
        # DFU<->APP 手动切换：文案与方向按当前设备模式在 _update_switch_btn 中更新；
        # 启用状态另行管理（需检测到设备才可用）。
        self.switch_btn = QPushButton("🔀 DFU ↔ APP " + t("Switch"))
        self.switch_btn.clicked.connect(self.run_switch_mode)
        self.aux_layout.addWidget(self.switch_btn)
        self.reflash_btn = QPushButton("♻ " + t("Reflash (repair)"))
        self.reflash_btn.setToolTip(
            t("Wait for DFU mode (prompt manual action on timeout) → "
              "rewrite app firmware → wait for app mode"))
        self.reflash_btn.clicked.connect(self.run_reflash)
        self.aux_layout.addWidget(self.reflash_btn)
        self.aux_layout.addStretch(1)

    def _efuse_badge(self) -> str:
        """eFuse lock badge (shown before the device status): unknown/unlocked/locked."""
        return {"locked": "🔒" + t("eFuse locked"),
                "unlocked": "🔓" + t("eFuse unlocked")}.get(
            self.efuse_status, "⚪" + t("eFuse unknown"))

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
        """Status is about the SELECTED product; other attached SLogic
        devices are only a secondary hint (they also block sigrok capture
        until the driver supports device selection)."""
        if not device_watch.backend_ok():
            # 无 libusb 后端时所有 usb.core.find 静默失败 -> 看不到任何设备；
            # 显式告警而非静默（典型：Windows 未装 libusb-1.0.dll）
            self.device_status_label.setText(
                "⚠ " + t("libusb backend not found (libusb-1.0.dll): cannot enumerate "
                         "USB devices, DFU/APP detection disabled — see "
                         "resources/README.md"))
            self.device_status_label.setStyleSheet("color:#c62828;")
            self.detected = []
            self._update_enablement()
            return
        self.detected = device_watch.scan_devices(self.profiles)
        p = self.profile
        conflict = self._conflicting_app_device(p)
        if conflict is not None:
            # 产品与在线设备不符：只提示切换到正确产品，其它操作在 _update_enablement 中禁用
            self.device_status_label.setText(
                "⚠ " + t("Online {conflict} differs from selected {name}: switch to "
                         "the correct product, or plug in the matching device").format(
                             conflict=conflict, name=p.display_name))
            self.device_status_label.setStyleSheet("color:#c62828;")
            self._update_enablement()
            return
        mine = next((d for d in self.detected
                     if p is not None and d.profile.id == p.id), None)
        # the DFU pid is shared across products, so one physical DFU
        # device matches several profiles -- list it once, and name it
        # "SLogic DFU" when the product can't be told apart
        shared_dfu = {k for k, n in Counter(
            (q.vid, q.dfu_pid) for q in self.profiles
            if q.dfu_pid is not None).items() if n > 1}
        mine_key = (mine.profile.vid, mine.pid) if mine else None
        others, seen = [], set()
        for d in self.detected:
            key = (d.profile.vid, d.pid)
            if (p is not None and d.profile.id == p.id) or key == mine_key or key in seen:
                continue
            seen.add(key)
            others.append("SLogic DFU" if key in shared_dfu else str(d))
        others_txt = ("　(" + t("also online: {items}").format(items=", ".join(others)) + ")"
                      if others else "")
        badge = self._efuse_badge()   # eFuse 状态（来自 🔄 扫描）前置显示
        if mine is not None:
            self.device_status_label.setText(
                f"{badge} | " + t("Device: {dev}").format(dev=mine) + others_txt)
            self.device_status_label.setStyleSheet("color: #2e7d32;")
        else:
            name = p.display_name if p else "SLogic"
            self.device_status_label.setText(
                f"{badge} | " + t("No {name} device detected").format(name=name) + others_txt)
            self.device_status_label.setStyleSheet("color: #c62828;")
        self._update_enablement()

    def _detected_mode(self, p: ProductProfile) -> Mode | None:
        for d in self.detected:
            if d.profile.id == p.id:
                return d.mode
        return None

    def _conflicting_app_device(self, p: ProductProfile | None):
        """在线的、属于其它产品的 APP 模式设备。APP 的 PID 各产品唯一，故能明确
        判定"插错产品"；DFU 的 PID 各产品共用、无法区分，永远不算冲突。"""
        if p is None:
            return None
        for d in self.detected:
            if d.mode == Mode.APP and d.profile.id != p.id:
                return d
        return None

    def _update_enablement(self):
        p = self.profile
        busy = self.pipeline is not None and self.pipeline.running
        self.stop_btn.setEnabled(busy)
        self.product_combo.setEnabled(not busy)   # 产品选择始终可用，以便纠正不符
        if p is None:
            self.start_btn.setEnabled(False)
            self.sampling_btn.setEnabled(False)
            return
        conflict = None if busy else self._conflicting_app_device(p)
        if conflict is not None:
            # 产品与在线设备不符：禁用一切操作，只允许（顶栏）切换到正确产品
            self.start_btn.setEnabled(False)
            self.start_btn.setToolTip(
                t("Online device is {online} (APP), differs from selected {sel} — "
                  "switch to the correct product or replug").format(
                      online=conflict.profile.display_name, sel=p.display_name))
            self.sampling_btn.setEnabled(False)
            for row in self.step_rows.values():
                row.run_btn.setEnabled(False)
            for btn in getattr(self, "programmer_aux_buttons", []):
                btn.setEnabled(False)
            if hasattr(self, "reflash_btn"):
                self.reflash_btn.setEnabled(False)
            self._update_switch_btn(p, allow=False)
            return
        fw_ok = (p.app_firmware is not None and p.app_firmware.is_file()) \
            or bool(self.fw_file_edit.text().strip())
        # USB auto-scan drives DFU/APP presence; the manual 🔄 scan drives the
        # external programmer state. Steps are gated by their real precondition.
        mode = self._detected_mode(p)
        dfu_present = mode == Mode.DFU
        app_present = mode == Mode.APP
        prog_ok = self.programmer_present is True
        has_blank = p.programmer is not None and p.programmer.flash is not None
        seq_ready = (self.sigrok is not None and p.dfu_pid is not None
                     and fw_ok and (not has_blank or prog_ok))
        self.start_btn.setEnabled(not busy and seq_ready)
        if not seq_ready:
            missing = []
            if self.sigrok is None: missing.append("sigrok-cli")
            if p.dfu_pid is None: missing.append("dfu_pid")
            if not fw_ok: missing.append(t("app firmware"))
            if has_blank and not prog_ok:
                missing.append(t("external programmer (click 🔄 scan)"))
            self.start_btn.setToolTip(t("Missing: {items}").format(items=", ".join(missing)))
        else:
            self.start_btn.setToolTip("")
        for sid, row in self.step_rows.items():
            tip = ""
            if sid.startswith("blank:"):
                ok = prog_ok
                if not ok:
                    tip = t("Connect the external programmer: click 🔄 scan in the top bar")
            elif sid == "flash_app":
                ok = p.dfu_pid is not None and fw_ok and dfu_present
                if not dfu_present:
                    tip = t("Device must be in DFU mode (complete the previous "
                            "blank-flash step first)")
            elif sid == "switch_app":
                ok = p.dfu_pid is not None and dfu_present
                if not dfu_present:
                    tip = t("Device must be in DFU mode")
            elif sid == "wait_dfu":
                ok = p.dfu_pid is not None
            elif sid == "wait_app":
                ok = True
            elif sid.startswith("capture"):
                ok = self.sigrok is not None and app_present
                if self.sigrok is not None and not app_present:
                    tip = t("Device must be in APP mode (finish flashing APP and "
                            "waiting for APP first)")
            else:
                ok = True
            row.run_btn.setEnabled(not busy and ok)
            row.run_btn.setToolTip(tip if not ok else "")
        prog_tip = (t("Production final-check operation; proceed with caution") if prog_ok
                    else t("Connect the external programmer: click 🔄 scan in the top bar"))
        for btn in getattr(self, "programmer_aux_buttons", []):
            btn.setEnabled(not busy and prog_ok)
            btn.setToolTip(prog_tip)
        if hasattr(self, "reflash_btn"):
            self.reflash_btn.setEnabled(not busy and p.dfu_pid is not None and fw_ok)
        self.sampling_btn.setEnabled(
            not busy and self.sigrok is not None
            and self._detected_mode(p) == Mode.APP)
        self._update_switch_btn(p, allow=not busy)

    def _update_switch_btn(self, p: ProductProfile | None, *, allow: bool):
        """按当前设备模式设置 DFU<->APP 切换按钮的文案、方向提示与启用状态。"""
        if not hasattr(self, "switch_btn"):
            return
        if p is None or p.dfu_pid is None:
            self.switch_btn.setText("🔀 DFU ↔ APP " + t("Switch"))
            self.switch_btn.setToolTip(t("This product has no DFU (dfu_pid); cannot switch"))
            self.switch_btn.setEnabled(False)
            return
        mode = self._detected_mode(p)
        if mode == Mode.DFU:
            self.switch_btn.setText("🔀 DFU → APP")
            self.switch_btn.setToolTip(
                t("Currently DFU (flashing) mode; switch to APP (application) mode"))
            self.switch_btn.setEnabled(allow)
        elif mode == Mode.APP:
            self.switch_btn.setText("🔀 APP → DFU")
            self.switch_btn.setToolTip(
                t("Currently APP (application) mode; switch to DFU (flashing) mode"))
            self.switch_btn.setEnabled(allow)
        else:
            self.switch_btn.setText("🔀 DFU ↔ APP " + t("Switch"))
            self.switch_btn.setToolTip(t("No device detected; cannot switch"))
            self.switch_btn.setEnabled(False)

    # -------------------------------------------------------------- actions

    def _callbacks(self) -> pipeline_mod.PipelineCallbacks:
        return pipeline_mod.PipelineCallbacks(
            on_log=self.log_signal.emit,
            on_step=lambda sid, st: self.step_signal.emit(sid, st),
            on_user_prompt=self.prompt_signal.emit,
            on_user_prompt_clear=self.prompt_clear_signal.emit,
            on_manual_switch=self.manual_switch_signal.emit,
            on_finished=lambda ok, rep: self.finished_signal.emit(ok, rep))

    def _start(self, pl: pipeline_mod.Pipeline, *, reset_rows: bool):
        if self.pipeline is not None and self.pipeline.running:
            return
        self.pipeline = pl
        self._session_t0 = time.time()
        # 本次运行含 eFuse 写锁步骤：成功后自动复扫，让 eFuse 徽标翻成 🔒
        self._rescan_after = any(s.id == "blank:efuse" for s in pl.steps)
        if reset_rows:
            for row in self.step_rows.values():
                row.reset()
        else:
            for step in pl.steps:
                if step.id in self.step_rows:
                    self.step_rows[step.id].reset()
        self._set_banner("running", t("Running…"))
        pl.start()
        self._update_enablement()

    def _override_firmware(self) -> Path | None:
        override = self.fw_file_edit.text().strip()
        return Path(override) if override else None

    def _override_dfu(self) -> Path | None:
        override = self.dfu_file_edit.text().strip()
        return Path(override) if override else None

    def _override_efuse(self) -> Path | None:
        override = self.ekey_file_edit.text().strip()
        return Path(override) if override else None

    def run_full_test(self):
        p = self.profile
        if p is None or self.sigrok is None:
            return
        self.log_signal.emit(
            "===== " + t("Run Full Test: {name}").format(name=p.display_name) + " =====")
        self._start(pipeline_mod.Pipeline.full_test(
            p, self.sigrok, self._callbacks(), self._override_firmware(),
            cable_index=self.probe_cable, image_override=self._override_dfu(),
            efuse_key_override=self._override_efuse()),
            reset_rows=True)

    def run_single_step(self, step_id: str):
        p = self.profile
        if p is None:
            return
        row = self.step_rows.get(step_id)
        self.log_signal.emit(
            "===== " + t("Single step: {label}").format(
                label=row.label_text if row else step_id) + " =====")
        self._start(pipeline_mod.Pipeline.single_step(
            p, self.sigrok, self._callbacks(), step_id, self._override_firmware(),
            cable_index=self.probe_cable, image_override=self._override_dfu(),
            efuse_key_override=self._override_efuse()),
            reset_rows=False)

    def run_probe(self):
        """Manual 🔄 scan: probe the external JTAG programmer (read-only) and
        read the eFuse lock state on a daemon thread."""
        p = self.profile
        if p is None:
            return
        if p.programmer is None:
            self.log_signal.emit(
                t("This product has no programmer.toml; no external programmer capability."))
            return
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("⏳")
        self.log_signal.emit(
            "===== " + t("Scan external programmer: {name}").format(
                name=p.display_name) + " =====")
        cfg = p.programmer

        def worker():
            try:
                res = programmer.probe(cfg, self.log_signal.emit)
            except Exception as e:  # never let the worker die silently
                self.log_signal.emit("[probe] " + t("exception: {e}").format(e=e))
                res = programmer.ProbeResult(
                    False, None, None, "unknown", t("exception: {e}").format(e=e))
            self.probe_signal.emit(res)

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------- environment prep
    def _run_env_setup(self, action: str):
        """Deploy/remove the platform prerequisite (udev / WinUSB) off the UI
        thread; both operations pop a system auth dialog (pkexec / UAC)."""
        if not hasattr(self, "env_deploy_btn"):
            return
        self.env_deploy_btn.setEnabled(False)
        self.env_remove_btn.setEnabled(False)
        # env logs stay ASCII/English on purpose: they interleave with output
        # from Windows tools (pnputil/wdi) that we can't force into one locale.
        self.log_signal.emit(
            f"===== env setup: {action} ({env_setup.requirement()}) =====")

        def worker():
            try:
                fn = env_setup.deploy if action == "deploy" else env_setup.remove
                ok = fn(self.log_signal.emit)
            except Exception as e:  # never let the worker die silently
                self.log_signal.emit(f"[env] exception: {e}")
                ok = False
            self.env_signal.emit(ok, action)

        threading.Thread(target=worker, daemon=True).start()

    def _on_env_done(self, ok: bool, action: str):
        self.log_signal.emit(f"env setup {action}: {'OK' if ok else 'FAILED'}")
        self._refresh_env_buttons()
        if ok:
            self.refresh_device_status()      # permissions/driver may now differ

    def _refresh_env_buttons(self):
        """Enable/label deploy & cancel from the current prereq state.  On
        Linux the rule file is directly checkable; on Windows the WinUSB
        binding isn't cheaply queryable, so both stay enabled."""
        if not hasattr(self, "env_deploy_btn"):
            return
        st = env_setup.status()
        self.env_deploy_btn.setEnabled(True)
        self.env_remove_btn.setEnabled(True)
        if st == "deployed":
            self.env_deploy_btn.setText(t("Redeploy"))
            self.env_deploy_btn.setToolTip(t("Rule exists; rewrite to update"))
            self.env_remove_btn.setToolTip(t("Remove the deployed rule"))
        elif st == "absent":
            self.env_deploy_btn.setText(t("Deploy"))
            self.env_deploy_btn.setToolTip("")
            self.env_remove_btn.setEnabled(False)
            self.env_remove_btn.setToolTip(t("Not deployed; nothing to remove"))
        else:  # unknown (Windows)
            self.env_deploy_btn.setText(t("Deploy"))
            self.env_deploy_btn.setToolTip("")
            self.env_remove_btn.setToolTip("")

    def run_switch_mode(self):
        p = self.profile
        if p is None:
            return
        mode = self._detected_mode(p)
        if mode == Mode.DFU:
            to_mode, title = "app", "DFU → APP"
        elif mode == Mode.APP:
            to_mode, title = "dfu", "APP → DFU"
        else:
            QMessageBox.information(
                self, t("Cannot switch"), t("No device for this product detected."))
            return
        self.log_signal.emit(
            "===== " + t("Auxiliary: mode switch {title}").format(title=title) + " =====")
        self._start(pipeline_mod.Pipeline.switch_mode(
            p, self._callbacks(), to_mode=to_mode,
            cable_index=self.probe_cable), reset_rows=False)

    def run_reflash(self):
        p = self.profile
        if p is None:
            return
        fw = self._override_firmware() or p.app_firmware
        if fw is None or not fw.is_file():
            QMessageBox.warning(
                self, t("Firmware missing"),
                t("Firmware file not found: {path}").format(path=fw))
            return
        self.log_signal.emit(
            "===== " + t("Reflash (repair): {name}").format(name=p.display_name) + " =====")
        self._start(pipeline_mod.Pipeline.reflash(
            p, self._callbacks(), fw, cable_index=self.probe_cable),
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
            QMessageBox.warning(self, t("Parameter error"), str(e))
            return
        self.log_signal.emit(
            "===== " + t("Custom capture: {ch}ch@{rate}").format(
                ch=channels, rate=format_rate(rate)) + " =====")
        self._param_dialog.hide()   # step aside so the operator sees the log
        self._start(pipeline_mod.Pipeline.capture_only(
            p, self.sigrok, self._callbacks(), channels=channels,
            samplerate_hz=rate, samples=samples, voltage_threshold_v=volt,
            expected_rows=expected_rows), reset_rows=False)

    def cancel_pipeline(self):
        if self.pipeline is not None:
            self.pipeline.request_cancel()
            self.log_signal.emit(t("Stop requested…"))

    def _add_resource_row(self, grid, row: int, label: str, on_browse):
        """一行"资源路径覆盖"：标签 + QLineEdit（placeholder 后填档案路径）+ … 浏览。
        返回该行的 QLineEdit。"""
        grid.addWidget(QLabel(label), row, 0)
        edit = QLineEdit()
        edit.setPlaceholderText(t("Defaults to product profile"))
        btn = QPushButton("…"); btn.setFixedWidth(30)
        btn.clicked.connect(on_browse)
        grid.addWidget(edit, row, 1)
        grid.addWidget(btn, row, 2)
        return edit

    def select_fw_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t("Select app firmware"), "",
            t("Firmware/Bitstream (*.bin *.fs);;All files (*)"))
        if path:
            self.fw_file_edit.setText(path)
            self._update_enablement()

    def select_dfu_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t("Select DFU image"), "",
            t("Bitstream (*.bin *.fs);;All files (*)"))
        if path:
            self.dfu_file_edit.setText(path)
            self._update_enablement()

    def select_ekey_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t("Select eFuse key"), "",
            t("eFuse key (*.ekey);;All files (*)"))
        if path:
            self.ekey_file_edit.setText(path)
            self._update_enablement()

    def copy_report(self):
        QApplication.clipboard().setText(self.report_box.toPlainText())
        self.copy_report_btn.setText(t("Copied") + " ✔")
        QTimer.singleShot(
            1500, lambda: self.copy_report_btn.setText("📋 " + t("Copy Report")))

    # -------------------------------------------------------------- slots

    def _append_log(self, s: str):
        # stable forwarding slot: always targets the CURRENT log_box, which a
        # language switch replaces with a new widget (see _rebuild_ui).
        self.log_box.append(s)

    def _on_language_changed(self, _idx):
        code = self.lang_combo.currentData()
        if code is None or code == get_language():
            return
        if self.pipeline is not None and self.pipeline.running:
            # can't rebuild the UI mid-run; revert the selection and explain
            self.lang_combo.blockSignals(True)
            i = self.lang_combo.findData(get_language())
            if i >= 0:
                self.lang_combo.setCurrentIndex(i)
            self.lang_combo.blockSignals(False)
            QMessageBox.information(
                self, t("Cannot switch"),
                t("Cannot switch language while a test is running."))
            return
        set_language(code)
        QSettings().setValue("language", code)
        self._rebuild_ui()

    def _on_step(self, sid: str, status: StepStatus):
        row = self.step_rows.get(sid)
        if row is not None:
            row.set_status(status)
        # 人工切换弹窗随后续"等待设备"步骤的完成自动关闭（设备已就绪/失败）
        if sid.startswith("wait_") and status in (StepStatus.PASSED,
                                                  StepStatus.FAILED):
            self._close_switch_popup()
        if status == StepStatus.RUNNING:
            label = row.label_text if row else sid
            self.step_label.setText(t("Current step: {label}").format(label=label))
            if not self._prompt_text:
                self._set_banner("running", t("Running — {label}").format(label=label))

    def _on_prompt(self, text: str):
        self._prompt_text = text
        self._set_banner("waiting", f"⏳ {text}")

    def _on_prompt_clear(self):
        self._prompt_text = ""

    def _on_manual_switch(self, text: str):
        """模式切换只剩硬件操作时的弹窗提示（非模态：流水线继续等待目标模式
        设备出现，出现后 _on_step 自动关闭本弹窗）。"""
        self._close_switch_popup()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(t("Manual mode switch required"))
        box.setText(text)
        box.setStandardButtons(QMessageBox.Ok)
        box.setModal(False)
        box.show()
        self._switch_popup = box

    def _close_switch_popup(self):
        if self._switch_popup is not None:
            self._switch_popup.close()
            self._switch_popup = None

    def _on_probe(self, res):
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("🔄")
        self.programmer_present = res.programmer_present
        self.efuse_status = res.efuse
        self.probe_cable = res.cable_index
        if res.programmer_present:
            self.log_signal.emit(
                t("External programmer connected: {detail}").format(detail=res.detail))
        else:
            self.log_signal.emit(
                t("External programmer not detected: {detail}").format(detail=res.detail))
        self.refresh_device_status()   # 设备状态栏前缀的 eFuse 徽标 + 使能刷新

    def _on_finished(self, ok: bool, report: str):
        self._close_switch_popup()
        elapsed = time.time() - self._session_t0 if self._session_t0 else 0
        fail_steps = [r.label_text for r in self.step_rows.values()
                      if r.status == StepStatus.FAILED]
        if ok:
            self._set_banner("pass", f"PASS  ({elapsed:.1f}s)")
        else:
            summary = (t("; failed steps: {items}").format(items=", ".join(fail_steps))
                       if fail_steps else "")
            self._set_banner("fail", f"FAIL  ({elapsed:.1f}s){summary}")
        self._render_report(ok, report, elapsed)
        self.step_label.setText("")
        self.pipeline = None
        self._update_enablement()
        if ok and self._rescan_after:
            self.log_signal.emit(
                t("eFuse locked; auto re-scanning the external programmer to "
                  "refresh state…"))
            QTimer.singleShot(400, self.run_probe)
        self._rescan_after = False

    def _render_report(self, ok: bool, detail: str, elapsed: float):
        p = self.profile
        lines = [
            "==== " + t("SLogic Production Test Report") + " ====",
            t("Time: {ts}").format(ts=time.strftime('%Y-%m-%d %H:%M:%S')),
            (t("Product: {name} ({id})").format(name=p.display_name, id=p.id)
             if p else t("Product: -")),
            t("Device: {devs}").format(
                devs=', '.join(str(d) for d in self.detected) or t("not detected")),
            t("Overall: {result}  (elapsed {s:.1f}s)").format(
                result='PASS' if ok else 'FAIL', s=elapsed),
            "",
            "-- " + t("Step Status") + " --",
        ]
        for row in self.step_rows.values():
            _, _, text = STATUS_STYLE[row.status]
            dur = f" ({row.duration_s:.1f}s)" if row.duration_s else ""
            lines.append(f"[{t(text)}] {row.name.text()}{dur}")
        if detail:
            lines += ["", "-- " + t("Details") + " --", detail]
        lines += ["", t("To report an issue, copy this report and attach the "
                        "relevant run-log excerpts.")]
        self.report_box.setPlainText("\n".join(lines))


def main() -> None:
    app = QApplication(sys.argv)
    # QSettings keying for the persisted language choice
    app.setOrganizationName("Sipeed")
    app.setApplicationName("SLogicPT")
    saved = QSettings().value("language", "zh")
    set_language(saved if isinstance(saved, str) else "zh")
    font = app.font()
    font.setPointSize(font.pointSize() + 2)
    app.setFont(font)
    gui = ProductionTestGUI()
    gui.resize(1280, 780)
    gui.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
