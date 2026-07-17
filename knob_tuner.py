# -*- coding: utf-8 -*-
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


BAUD_DEFAULT = 9600


class KnobTunerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("旋钮上位机调参工具")
        self.geometry("1180x560")
        self.minsize(1040, 520)

        self.serial_port = None
        self.reader_thread = None
        self.reader_running = False
        self.rx_queue = queue.Queue()
        self.fields = {}

        self._build_ui()
        self.refresh_ports()
        self.after(50, self.process_rx_queue)

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        conn = ttk.LabelFrame(root, text="连接设置", padding=8)
        conn.pack(fill=tk.X)
        for col in (1, 7):
            conn.columnconfigure(col, weight=1)

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value=str(BAUD_DEFAULT))

        ttk.Label(conn, text="串口").grid(row=0, column=0, sticky=tk.W)
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, width=22, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky=tk.W, padx=(6, 12))
        ttk.Button(conn, text="刷新串口", command=self.refresh_ports).grid(row=0, column=2, padx=(0, 12))

        ttk.Label(conn, text="波特率").grid(row=0, column=3, sticky=tk.W)
        ttk.Entry(conn, textvariable=self.baud_var, width=10).grid(row=0, column=4, sticky=tk.W, padx=(6, 12))

        self.connect_button = ttk.Button(conn, text="连接", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=5, sticky=tk.W)
        ttk.Button(conn, text="读取参数", command=self.print_config).grid(row=0, column=6, padx=(12, 0))
        ttk.Button(conn, text="退出 HID", command=lambda: self.send_command("EXIT_HID")).grid(row=0, column=7, sticky=tk.W, padx=(8, 0))

        note = ttk.Label(
            root,
            text="提示：CFG / RANGE / DETENTS 只在旋钮进入 TUNE 调参模式后生效；EXIT_HID 可以在任意模式发送。",
        )
        note.pack(anchor=tk.W, pady=(8, 0))

        body = ttk.Frame(root)
        body.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        right.rowconfigure(3, weight=1)

        cfg = ttk.LabelFrame(left, text="手感参数 CFG", padding=8)
        cfg.pack(fill=tk.BOTH, expand=True)
        cfg.columnconfigure(2, weight=1)
        self.add_field(cfg, 0, "width_rad", "0.052", "每个档位的角度，单位弧度；越小越灵敏。")
        self.add_field(cfg, 1, "width_deg", "", "每个档位的角度，单位度；填了 width_rad 时优先用弧度。")
        self.add_field(cfg, 2, "detent", "1.0", "档位吸附力度；0 表示基本没有吸附。")
        self.add_field(cfg, 3, "endstop", "1.0", "到最小/最大边界时的限位力度。")
        self.add_field(cfg, 4, "snap", "1.1", "跳到下一档的阈值比例；越小越容易跳档。")
        self.add_field(cfg, 5, "bias", "0", "跳档偏置；用于让正反方向手感略有差异。")
        self.add_field(cfg, 6, "pid_p", "1.0", "PID 比例项；影响回拉响应强度。")
        self.add_field(cfg, 7, "pid_i", "0.0", "PID 积分项；一般保持 0。")
        self.add_field(cfg, 8, "pid_d", "0.148", "PID 微分项；影响阻尼和稳定性。")
        self.add_field(cfg, 9, "pid_limit", "10", "PID 输出限制；限制电机最大控制量。")
        ttk.Button(cfg, text="应用手感参数", command=self.apply_cfg).grid(row=10, column=0, sticky=tk.W, pady=(8, 0))

        rng = ttk.LabelFrame(right, text="范围和当前数值 RANGE", padding=8)
        rng.grid(row=0, column=0, sticky="ew")
        rng.columnconfigure(2, weight=1)
        self.add_field(rng, 0, "min", "0", "允许的最小档位/数值。")
        self.add_field(rng, 1, "max", "100", "允许的最大档位/数值；max 小于 min 表示不限制。")
        self.add_field(rng, 2, "pos", "0", "当前档位/数值；发送后屏幕显示会同步。")
        ttk.Button(rng, text="应用范围数值", command=self.apply_range).grid(row=3, column=0, sticky=tk.W, pady=(8, 0))

        det = ttk.LabelFrame(right, text="特殊吸附档位 DETENTS", padding=8)
        det.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        det.columnconfigure(2, weight=1)
        self.add_field(det, 0, "detents", "", "只在指定档位产生吸附，例如 2,10,21,22；最多 5 个。")
        ttk.Button(det, text="应用吸附档位", command=self.apply_detents).grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Button(det, text="清空吸附档位", command=self.clear_detents).grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))

        manual = ttk.LabelFrame(right, text="手动命令", padding=8)
        manual.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        manual.columnconfigure(0, weight=1)
        self.manual_var = tk.StringVar(value="CFG width_rad=0.052 detent=1 endstop=1 snap=1.1 bias=0")
        ttk.Entry(manual, textvariable=self.manual_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(manual, text="发送", command=lambda: self.send_command(self.manual_var.get())).grid(row=0, column=1, padx=(8, 0))

        log_frame = ttk.LabelFrame(right, text="串口 LOG", padding=8)
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_actions = ttk.Frame(log_frame)
        log_actions.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(log_actions, text="清空 LOG", command=self.clear_log).pack(side=tk.LEFT)
        self.log = scrolledtext.ScrolledText(log_frame, height=9, wrap=tk.WORD)
        self.log.grid(row=1, column=0, sticky="nsew")

    def add_field(self, parent, row, name, default, desc):
        ttk.Label(parent, text=name, width=11).grid(row=row, column=0, sticky=tk.W, pady=3)
        var = tk.StringVar(value=default)
        ttk.Entry(parent, textvariable=var, width=14).grid(row=row, column=1, sticky=tk.W, padx=(6, 10), pady=3)
        ttk.Label(parent, text=desc, wraplength=330).grid(row=row, column=2, sticky=tk.W, pady=3)
        self.fields[name] = var

    def refresh_ports(self):
        if list_ports is None:
            messagebox.showerror("缺少依赖", "请先安装依赖：pip install -r tools\\knob_tuner\\requirements.txt")
            return
        ports = [port.device for port in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def toggle_connection(self):
        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if serial is None:
            messagebox.showerror("缺少依赖", "请先安装依赖：pip install -r tools\\knob_tuner\\requirements.txt")
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("未选择串口", "请先选择一个串口。")
            return
        try:
            baud = int(self.baud_var.get().strip())
            self.serial_port = serial.Serial(port, baud, timeout=0.1)
            time.sleep(0.2)
            self.reader_running = True
            self.reader_thread = threading.Thread(target=self.reader_loop, daemon=True)
            self.reader_thread.start()
            self.connect_button.config(text="断开")
            self.append_log(f"已连接 {port} @ {baud}")
        except Exception as exc:
            messagebox.showerror("连接失败", str(exc))

    def disconnect(self):
        self.reader_running = False
        if self.reader_thread:
            self.reader_thread.join(timeout=0.5)
            self.reader_thread = None
        if self.serial_port:
            try:
                self.serial_port.close()
            except Exception:
                pass
            self.serial_port = None
        self.connect_button.config(text="连接")
        self.append_log("已断开")

    def reader_loop(self):
        while self.reader_running and self.serial_port and self.serial_port.is_open:
            try:
                raw = self.serial_port.readline()
                if raw:
                    text = raw.decode(errors="replace").strip()
                    if text:
                        self.rx_queue.put(text)
            except Exception as exc:
                self.rx_queue.put(f"读取错误：{exc}")
                break

    def process_rx_queue(self):
        while True:
            try:
                line = self.rx_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(f"< {line}")
            self.sync_fields_from_cfg(line)
        self.after(50, self.process_rx_queue)

    def append_log(self, text):
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)

    def clear_log(self):
        self.log.delete("1.0", tk.END)

    def send_command(self, command):
        command = command.strip()
        if not command:
            return
        if not self.serial_port or not self.serial_port.is_open:
            messagebox.showwarning("未连接", "请先连接旋钮串口。")
            return
        self.serial_port.write((command + "\n").encode("utf-8"))
        self.append_log(f"> {command}")

    def field_value(self, name):
        return self.fields[name].get().strip()

    def apply_cfg(self):
        parts = []
        width_rad = self.field_value("width_rad")
        width_deg = self.field_value("width_deg")
        if width_rad:
            parts.append(f"width_rad={width_rad}")
        elif width_deg:
            parts.append(f"width_deg={width_deg}")
        for name in ("detent", "endstop", "snap", "bias", "pid_p", "pid_i", "pid_d", "pid_limit"):
            value = self.field_value(name)
            if value:
                parts.append(f"{name}={value}")
        if parts:
            self.send_command("CFG " + " ".join(parts))

    def apply_range(self):
        parts = []
        for name in ("min", "max", "pos"):
            value = self.field_value(name)
            if value:
                parts.append(f"{name}={value}")
        if parts:
            self.send_command("RANGE " + " ".join(parts))

    def apply_detents(self):
        value = self.field_value("detents")
        if value:
            self.send_command("DETENTS values=" + value.replace(" ", ""))

    def clear_detents(self):
        self.fields["detents"].set("")
        self.send_command("DETENTS clear")

    def print_config(self):
        self.send_command("PRINTCFG")

    def sync_fields_from_cfg(self, line):
        if not line.startswith("CFG "):
            return
        pairs = dict(re.findall(r"([A-Za-z_]+)=([^ ]+)", line))
        for name, var in self.fields.items():
            if name in pairs:
                value = pairs[name]
                if name == "detents" and value == "clear":
                    value = ""
                var.set(value)

    def destroy(self):
        self.disconnect()
        super().destroy()


if __name__ == "__main__":
    app = KnobTunerApp()
    app.mainloop()
