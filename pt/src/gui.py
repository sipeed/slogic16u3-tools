import sys
import subprocess
import os
import re
import threading
import usb.core
import time
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QComboBox, QMessageBox, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt5.QtCore import pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from logic_analyzer import extract_channels, detect_pwm_freq, check_pwm_duty

def parse_sample_rate_input(rate_str):
    m = re.match(r"^(\d+)([kKmM]?)$", rate_str.strip())
    if not m:
        raise ValueError("Sample rate must be a number optionally followed by K or M (e.g., 400M, 10K, 500000)")
    value = int(m.group(1))
    unit = m.group(2).upper()
    if unit == 'M':
        rate = value * 1_000_000
    elif unit == 'K':
        rate = value * 1_000
    else:
        rate = value
    return rate, value, unit if unit else ''

class LogicAnalyzerGUI(QWidget):
    log_signal = pyqtSignal(str)
    output_signal = pyqtSignal(str)
    output_html_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.cli_path = os.path.abspath("../../SLogic16U3-tools/cli/build/slogic_cli")
        if not os.path.isfile(self.cli_path):
            self.cli_path = ""
        self.init_ui()
        self.log_signal.connect(self.log_box.append)
        self.output_signal.connect(self.output_box.append)
        self.output_html_signal.connect(self.output_box.insertHtml)
        # Add timer for real-time device detection
        self.device_timer = QTimer(self)
        self.device_timer.timeout.connect(self.update_device_status)
        self.device_timer.start(1500)  # check every 1.5 seconds

    def init_ui(self):
        self.setWindowTitle("Logic Analyzer GUI")

        layout = QHBoxLayout()
        left_panel = QVBoxLayout()
        right_panel = QVBoxLayout()

        # Device status label (big font)
        self.device_status_label = QLabel()
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        self.device_status_label.setFont(font)
        left_panel.addWidget(self.device_status_label)

        # CLI path controls (inline)
        cli_path_layout = QHBoxLayout()
        self.cli_path_edit = QLineEdit(self.cli_path or os.path.abspath("../../SLogic16U3-tools/cli/build/slogic_cli"))
        self.cli_path_edit.setReadOnly(True)
        select_cli_btn = QPushButton("Select CLI")
        select_cli_btn.clicked.connect(self.select_cli)
        default_cli_btn = QPushButton("Default CLI")
        default_cli_btn.clicked.connect(self.set_default_cli)
        cli_path_layout.addWidget(QLabel("slogic_cli:"))
        cli_path_layout.addWidget(self.cli_path_edit)
        cli_path_layout.addWidget(select_cli_btn)
        cli_path_layout.addWidget(default_cli_btn)
        left_panel.addLayout(cli_path_layout)

        # Sample Rate (inline)
        sample_rate_layout = QHBoxLayout()
        sample_rate_label = QLabel("Sample Rate:")
        self.sample_rate_edit = QLineEdit("400M")
        self.sample_rate_edit.setPlaceholderText("e.g. 400M")
        sample_rate_layout.addWidget(sample_rate_label)
        sample_rate_layout.addWidget(self.sample_rate_edit)
        left_panel.addLayout(sample_rate_layout)

        # Channels (inline)
        channel_layout = QHBoxLayout()
        channel_label = QLabel("Channels:")
        self.channel_select = QComboBox()
        self.channel_select.addItems(['4', '8', '16'])
        self.channel_select.setCurrentText('8')
        channel_layout.addWidget(channel_label)
        channel_layout.addWidget(self.channel_select)
        left_panel.addLayout(channel_layout)

        # Volt Threshold (inline)
        volt_layout = QHBoxLayout()
        volt_label = QLabel("Volt Threshold (mV):")
        self.volt_threshold_edit = QLineEdit("1600")
        self.volt_threshold_edit.setPlaceholderText("e.g. 1600 (mV)")
        volt_layout.addWidget(volt_label)
        volt_layout.addWidget(self.volt_threshold_edit)
        left_panel.addLayout(volt_layout)

        # Sampling button
        self.sampling_button = QPushButton("SAMPLING")
        self.sampling_button.clicked.connect(self.run_sampling)
        left_panel.addWidget(self.sampling_button)

        # Expected values table
        self.expected_table = QTableWidget()
        self.expected_table.setColumnCount(2)
        self.expected_table.setHorizontalHeaderLabels(['Expected Freq (Hz)', 'Expected Duty (%)'])
        self.update_expected_table()
        left_panel.addWidget(QLabel("Expected Values:"))
        left_panel.addWidget(self.expected_table)
        self.channel_select.currentTextChanged.connect(self.update_expected_table)

        # --- OTA Block ---
        ota_layout = QHBoxLayout()
        self.ota_file_edit = QLineEdit()
        self.ota_file_edit.setPlaceholderText("Select firmware.bin")
        ota_file_btn = QPushButton("Select")
        ota_file_btn.clicked.connect(self.select_ota_file)
        self.ota_start_btn = QPushButton("OTA")
        self.ota_start_btn.clicked.connect(self.run_ota)
        ota_layout.addWidget(QLabel("OTA:"))
        ota_layout.addWidget(self.ota_file_edit)
        ota_layout.addWidget(ota_file_btn)
        ota_layout.addWidget(self.ota_start_btn)
        left_panel.addLayout(ota_layout)
        # --- End OTA Block ---

        # --- Flash Control Block ---
        flash_ctrl_layout = QHBoxLayout()
        self.lock_btn = QPushButton("Lock")
        self.lock_btn.clicked.connect(lambda: self.run_flash_cmd("lock"))
        self.flash_btn = QPushButton("Flash")
        self.flash_btn.clicked.connect(lambda: self.run_flash_cmd("flash"))
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.clicked.connect(lambda: self.run_flash_cmd("reset"))
        flash_ctrl_layout.addWidget(self.lock_btn)
        flash_ctrl_layout.addWidget(self.flash_btn)
        flash_ctrl_layout.addWidget(self.reset_btn)
        left_panel.addLayout(flash_ctrl_layout)
        # --- End Flash Control Block ---

        # Log box (with clear button)
        log_label_layout = QHBoxLayout()
        log_label_layout.addWidget(QLabel("Log:"))
        log_clear_btn = QPushButton("Clear")
        log_clear_btn.clicked.connect(self.log_box_clear)
        log_label_layout.addWidget(log_clear_btn)
        right_panel.addLayout(log_label_layout)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        right_panel.addWidget(self.log_box)

        # Output box (with clear button)
        output_label_layout = QHBoxLayout()
        output_label_layout.addWidget(QLabel("Output:"))
        output_clear_btn = QPushButton("Clear")
        output_clear_btn.clicked.connect(self.output_box_clear)
        output_label_layout.addWidget(output_clear_btn)
        right_panel.addLayout(output_label_layout)

        self.output_box = QTextEdit()
        self.output_box.setReadOnly(True)
        right_panel.addWidget(self.output_box)

        layout.addLayout(left_panel, 1)
        layout.addLayout(right_panel, 2)
        self.setLayout(layout)

        # Now all widgets exist, safe to call
        self.update_device_status()

    def select_cli(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select slogic_cli", "", "Executable Files (*)")
        if path:
            self.cli_path = path
            self.cli_path_edit.setText(self.cli_path)

    def set_default_cli(self):
        default_path = os.path.abspath("../../SLogic16U3-tools/cli/build/slogic_cli")
        self.cli_path = default_path
        self.cli_path_edit.setText(self.cli_path)

    def update_expected_table(self):
        num_channels = int(self.channel_select.currentText())
        self.expected_table.setRowCount(num_channels)
        for ch in range(num_channels):
            freq_item = QTableWidgetItem("10000000")  # default 10MHz
            duty_item = QTableWidgetItem("50")       # default 50%
            self.expected_table.setItem(ch, 0, freq_item)
            self.expected_table.setItem(ch, 1, duty_item)
        self.expected_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

    def run_sampling(self):
        try:
            if not self.cli_path or not os.path.isfile(self.cli_path):
                QMessageBox.warning(self, "slogic_cli Not Found", "slogic_cli not found! Please select the correct path.")
                return
            sample_rate_str = self.sample_rate_edit.text()
            sample_rate, rate_value, rate_unit = parse_sample_rate_input(sample_rate_str)
            num_channels = int(self.channel_select.currentText())
            volt_threshold = int(self.volt_threshold_edit.text())
            unit_str = rate_unit if rate_unit else ''
            filename = f"{num_channels}ch_{rate_value}{unit_str}_wave.bin"
            out_dir = os.path.abspath(".")
            file_path = os.path.join(out_dir, filename)
            if os.path.exists(file_path):
                os.remove(file_path)
            cmd = [
                self.cli_path,
                "--sr", str(sample_rate/10**6),  # in MHz
                "--ch", str(num_channels),
                "--volt", str(volt_threshold)
            ]
            self.log_box.append(f"Running: {' '.join(cmd)}")
            # Run in thread to avoid blocking GUI
            threading.Thread(target=self._run_sampling_thread, args=(cmd, file_path, filename, num_channels, sample_rate), daemon=True).start()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _run_sampling_thread(self, cmd, file_path, filename, num_channels, sample_rate):
        try:
            start = time.time()
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                self.log_signal.emit(line.rstrip())
            process.wait()
            elapsed = time.time() - start
            self.output_signal.emit(f"Sampling operation cost: {elapsed:.2f} s")
            if process.returncode != 0:
                self.log_signal.emit(f"slogic_cli failed with return code {process.returncode}")
                return
            # Check output file
            if not os.path.exists(file_path):
                out_dir = os.path.dirname(file_path)
                bin_files = [f for f in os.listdir(out_dir) if f.endswith("_wave.bin")]
                if not bin_files:
                    self.log_signal.emit("No output .bin file found.")
                    return
                filename = max(bin_files, key=lambda f: os.path.getctime(os.path.join(out_dir, f)))
                file_path = os.path.join(out_dir, filename)
            self.output_signal.emit(f"Parsing file: {filename}")
            with open(file_path, "rb") as f:
                raw = f.read()
            channels = extract_channels(raw, num_channels)
            self.output_signal.emit(f"Total samples: {len(channels[0])}")
            all_pass = True
            for ch in range(num_channels):
                samples = channels[ch][:1000]
                freq = detect_pwm_freq(samples, sample_rate)
                duty = check_pwm_duty(samples)
                freq_str = f"{freq/1e6:.6f}MHz" if freq else "N/A"
                duty_str = f"{duty*100:.2f}%" if duty is not None else "N/A"
                self.output_signal.emit(f"CH{ch}: PWM freq = {freq_str}, duty cycle = {duty_str}")

                expected_freq = float(self.expected_table.item(ch, 0).text())
                expected_duty = float(self.expected_table.item(ch, 1).text())
                freq_match = freq is not None and abs(freq - expected_freq) < expected_freq * 0.05
                duty_match = duty is not None and abs(duty*100 - expected_duty) < 5
                if not (freq_match and duty_match):
                    all_pass = False
                    self.output_signal.emit(f"  -> FAIL (Expected: {expected_freq}Hz, {expected_duty}%)")
            if all_pass:
                self.output_html_signal.emit('<br><span style="color:green;font-weight:bold;">PASS</span><br>')
            else:
                self.output_html_signal.emit('<br><span style="color:red;font-weight:bold;">FAIL</span><br>')
        except Exception as e:
            self.log_signal.emit(f"Error: {e}")

    def select_ota_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select firmware.bin", "", "BIN Files (*.bin)")
        if path:
            self.ota_file_edit.setText(path)

    def run_ota(self):
        firmware = self.ota_file_edit.text()
        if not firmware or not os.path.isfile(firmware):
            QMessageBox.warning(self, "No firmware", "Please select a valid firmware.bin file.")
            return
        ota_script = os.path.abspath("../ota/src/spi_flash.py")
        cmd = ["python3", ota_script, firmware]
        self.log_box.append(f"Running OTA: {' '.join(cmd)}")
        threading.Thread(target=self._run_ota_thread, args=(cmd,), daemon=True).start()

    def _run_ota_thread(self, cmd):
        try:
            start = time.time()
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                self.log_signal.emit(line.rstrip())
            process.wait()
            elapsed = time.time() - start
            self.output_signal.emit(f"OTA operation cost: {elapsed:.2f} s")
            if process.returncode != 0:
                self.log_signal.emit(f"OTA failed with code {process.returncode}")
        except Exception as e:
            self.log_signal.emit(f"OTA error: {e}")

    def log_box_clear(self):
        self.log_box.clear()

    def output_box_clear(self):
        self.output_box.clear()

    def update_device_status(self):
        # Scan for SLogic devices by VID/PID
        found = None
        try:
            if usb.core.find(idVendor=0x359f, idProduct=0x3031):
                found = "SLogic16U3"
            elif usb.core.find(idVendor=0x359f, idProduct=0x30f1):
                found = "SLogic16U3 OTA"
        except Exception:
            pass
            
        if found == "SLogic16U3":
            self.device_status_label.setText("Found A device: SLogic16U3")
            self.device_status_label.setStyleSheet("color: green;")
            self.sampling_button.setEnabled(True)
            self.ota_start_btn.setEnabled(False)
            self.ota_file_edit.setEnabled(False)
        elif found == "SLogic16U3 OTA":
            self.device_status_label.setText("Found A device: SLogic16U3 OTA")
            self.device_status_label.setStyleSheet("color: green;")
            self.sampling_button.setEnabled(False)
            self.ota_start_btn.setEnabled(True)
            self.ota_file_edit.setEnabled(True)
        else:
            self.device_status_label.setText("No SLogic16U3 device found")
            self.device_status_label.setStyleSheet("color: red;")
            self.sampling_button.setEnabled(False)
            self.ota_start_btn.setEnabled(False)
            self.ota_file_edit.setEnabled(False)

    def run_flash_cmd(self, action):
        # Fill in the actual command for each action
        if action == "lock":
            cmd = ["bash", "/home/sipeed007/gowin/scripts/efuse_lock.sh"]
        elif action == "flash":
            cmd = ["bash", "/home/sipeed007/gowin/scripts/gowin_flash.sh"]
        elif action == "reset":
            cmd = ["bash", "/home/sipeed007/gowin/scripts/usb_rst.sh"]
        else:
            return
        self.log_box.append(f"Running: {' '.join(cmd)}")
        threading.Thread(target=self._run_flash_cmd_thread, args=(cmd,), daemon=True).start()

    def _run_flash_cmd_thread(self, cmd):
        try:
            start = time.time()
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                self.log_signal.emit(line.rstrip())
            process.wait()
            elapsed = time.time() - start
            self.output_signal.emit(f"Flash operation ({' '.join(cmd)}) cost: {elapsed:.2f} s")
            if process.returncode != 0:
                self.log_signal.emit(f"Command failed with code {process.returncode}")
        except Exception as e:
            self.log_signal.emit(f"Command error: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    gui = LogicAnalyzerGUI()
    gui.show()
    sys.exit(app.exec_())