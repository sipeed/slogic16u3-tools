import sys
import subprocess
import os
import re
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QComboBox, QMessageBox, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView
)
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
    def __init__(self):
        super().__init__()
        self.cli_path = os.path.abspath("../../SLogic16U3-tools/cli/build/slogic_cli")
        if not os.path.isfile(self.cli_path):
            self.cli_path = ""
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("Logic Analyzer GUI")

        layout = QHBoxLayout()
        left_panel = QVBoxLayout()
        right_panel = QVBoxLayout()

        # CLI path controls (inline)
        cli_path_layout = QHBoxLayout()
        self.cli_path_edit = QLineEdit(self.cli_path or os.path.abspath("../../SLogic15U3-tools/cli/build/slogic_cli"))
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
            # Parse sample rate and unit
            sample_rate_str = self.sample_rate_edit.text()
            sample_rate, rate_value, rate_unit = parse_sample_rate_input(sample_rate_str)
            num_channels = int(self.channel_select.currentText())
            volt_threshold = int(self.volt_threshold_edit.text())
            # Generate output file name
            unit_str = rate_unit if rate_unit else ''
            filename = f"{num_channels}ch_{rate_value}{unit_str}_wave.bin"
            out_dir = os.path.abspath(".")
            file_path = os.path.join(out_dir, filename)
            # Ensure the output file does not exist
            if os.path.exists(file_path):
                os.remove(file_path)
            # Compose slogic_cli command
            cmd = [
                self.cli_path,
                "--sr", str(sample_rate/10**6),  # in MHz
                "--ch", str(num_channels),
                "--volt", str(volt_threshold)
            ]
            self.log_box.append(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.log_box.append(result.stdout)
            if result.stderr:
                self.log_box.append(result.stderr)
            if result.returncode != 0:
                raise RuntimeError(f"slogic_cli failed with return code {result.returncode}")
            # Check output file
            if not os.path.exists(file_path):
                # Try to find the latest .bin file
                bin_files = [f for f in os.listdir(out_dir) if f.endswith("_wave.bin")]
                if not bin_files:
                    raise FileNotFoundError("No output .bin file found.")
                filename = max(bin_files, key=lambda f: os.path.getctime(os.path.join(out_dir, f)))
                file_path = os.path.join(out_dir, filename)
            self.output_box.append(f"Parsing file: {filename}")
            with open(file_path, "rb") as f:
                raw = f.read()
            channels = extract_channels(raw, num_channels)
            self.output_box.append(f"Total samples: {len(channels[0])}")
            all_pass = True
            for ch in range(num_channels):
                samples = channels[ch][:1000]
                freq = detect_pwm_freq(samples, sample_rate)
                duty = check_pwm_duty(samples)
                freq_str = f"{freq/1e6:.6f}MHz" if freq else "N/A"
                duty_str = f"{duty*100:.2f}%" if duty is not None else "N/A"
                self.output_box.append(f"CH{ch}: PWM freq = {freq_str}, duty cycle = {duty_str}")

                # Check against expected
                expected_freq = float(self.expected_table.item(ch, 0).text())
                expected_duty = float(self.expected_table.item(ch, 1).text())
                freq_match = freq is not None and abs(freq - expected_freq) < expected_freq * 0.05  # 5% tolerance
                duty_match = duty is not None and abs(duty*100 - expected_duty) < 5                 # 5% tolerance
                if not (freq_match and duty_match):
                    all_pass = False
                    self.output_box.append(f"  -> FAIL (Expected: {expected_freq}Hz, {expected_duty}%)")

            if all_pass:
                self.output_box.insertHtml('<br><span style="color:green;font-weight:bold;">PASS</span><br>')
            else:
                self.output_box.insertHtml('<br><span style="color:red;font-weight:bold;">FAIL</span><br>')
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def log_box_clear(self):
        self.log_box.clear()

    def output_box_clear(self):
        self.output_box.clear()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    gui = LogicAnalyzerGUI()
    gui.show()
    sys.exit(app.exec_())