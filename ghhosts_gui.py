#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghhosts_gui.py - GitHub Hosts 自动更新工具 (图形界面版)

基于 tkinter (Python 标准库), 无需额外依赖。
复用 ghhosts.py 的全部核心逻辑, 仅重做展示层。

界面结构 (单页):
  ┌─────────────────────────────────────────────┐
  │ 状态栏: 管理员权限 / hosts 路径 / 记录数      │
  ├─────────────────────────────────────────────┤
  │ 域名状态表格 (域名 | IP | 校验结果 | 状态)    │
  ├─────────────────────────────────────────────┤
  │ [检查] [更新 hosts] [刷新DNS] [回滚] [计划任务]│
  ├─────────────────────────────────────────────┤
  │ 日志输出区                                    │
  └─────────────────────────────────────────────┘
"""

import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime

# 复用核心逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import ghhosts as core
except ImportError as e:
    # 打包后模块名可能不同
    core = None
    _import_err = e

# ---------------------------------------------------------------- 主题配色

CLR_BG = "#f5f6f8"
CLR_CARD = "#ffffff"
CLR_BORDER = "#dcdfe6"
CLR_TEXT = "#2c3038"
CLR_TEXT_DIM = "#7a8291"
CLR_PRIMARY = "#2d6cdf"
CLR_PRIMARY_HOVER = "#1f57bd"
CLR_OK = "#18a058"
CLR_WARN = "#e6a23c"
CLR_ERR = "#d03050"
CLR_BTN_BG = "#eef0f4"
CLR_BTN_HOVER = "#e2e5ec"

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_UI_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_MONO = ("Consolas", 9)


# ---------------------------------------------------------------- 主应用

class HostsApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GitHub Hosts 自动更新工具")
        self.root.geometry("900x680")
        self.root.minsize(780, 560)
        self.root.configure(bg=CLR_BG)

        self.entries = []          # [(ip, domain, note)]
        self.poisoned = []         # 被投毒的域名
        self.report = {}
        self.busy = False
        self.log_queue = queue.Queue()

        self._setup_style()
        self._build_ui()
        self._center_window()

        # 启动时自动检查
        self.root.after(300, self.on_check)
        # 日志轮询
        self._poll_log()

    # ------------------------------------------------------------ 样式

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=CLR_BG)
        style.configure("Card.TFrame", background=CLR_CARD,
                        relief="flat", borderwidth=1)
        style.configure("TLabel", background=CLR_BG, foreground=CLR_TEXT,
                        font=FONT_UI)
        style.configure("Card.TLabel", background=CLR_CARD,
                        foreground=CLR_TEXT, font=FONT_UI)
        style.configure("Title.TLabel", background=CLR_BG,
                        foreground=CLR_TEXT, font=FONT_TITLE)
        style.configure("Dim.TLabel", background=CLR_BG,
                        foreground=CLR_TEXT_DIM, font=("Microsoft YaHei UI", 9))

        style.configure("Treeview",
                        background=CLR_CARD, fieldbackground=CLR_CARD,
                        foreground=CLR_TEXT, rowheight=28,
                        font=FONT_UI, borderwidth=0)
        style.configure("Treeview.Heading",
                        background="#eceff4", foreground=CLR_TEXT,
                        font=FONT_UI_BOLD, relief="flat")
        style.map("Treeview",
                  background=[("selected", "#d6e4ff")],
                  foreground=[("selected", CLR_TEXT)])
        style.map("Treeview.Heading", background=[("active", "#e2e6ee")])

        style.configure("Primary.TButton", font=FONT_UI_BOLD,
                        background=CLR_PRIMARY, foreground="white",
                        borderwidth=0, focuscolor=CLR_PRIMARY, padding=(16, 8))
        style.map("Primary.TButton",
                  background=[("active", CLR_PRIMARY_HOVER),
                              ("disabled", "#a8bce6")],
                  foreground=[("disabled", "#eef2fa")])

        style.configure("Normal.TButton", font=FONT_UI,
                        background=CLR_BTN_BG, foreground=CLR_TEXT,
                        borderwidth=0, focuscolor=CLR_BTN_BG, padding=(14, 8))
        style.map("Normal.TButton",
                  background=[("active", CLR_BTN_HOVER),
                              ("disabled", "#f2f3f5")],
                  foreground=[("disabled", "#b8bec9")])

    def _center_window(self):
        self.root.update_idletasks()
        w, h = 900, 680
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = max(0, (sh - h) // 2 - 20)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self):
        # ---- 顶部标题栏 ----
        header = tk.Frame(self.root, bg=CLR_BG)
        header.pack(fill="x", padx=18, pady=(16, 10))

        tk.Label(header, text="GitHub Hosts 自动更新", bg=CLR_BG,
                 fg=CLR_TEXT, font=FONT_TITLE).pack(side="left")

        self.lbl_admin = tk.Label(header, text="", bg=CLR_BG,
                                  fg=CLR_TEXT_DIM, font=("Microsoft YaHei UI", 9))
        self.lbl_admin.pack(side="right")

        # ---- 信息卡片 ----
        info = tk.Frame(self.root, bg=CLR_CARD, highlightbackground=CLR_BORDER,
                        highlightthickness=1)
        info.pack(fill="x", padx=18, pady=(0, 10))
        inner = tk.Frame(info, bg=CLR_CARD)
        inner.pack(fill="x", padx=14, pady=10)

        self.lbl_hosts = tk.Label(inner, text="", bg=CLR_CARD,
                                  fg=CLR_TEXT_DIM, font=("Microsoft YaHei UI", 9),
                                  anchor="w", justify="left")
        self.lbl_hosts.pack(fill="x")

        self.lbl_summary = tk.Label(inner, text="准备就绪", bg=CLR_CARD,
                                    fg=CLR_TEXT, font=FONT_UI_BOLD,
                                    anchor="w", justify="left")
        self.lbl_summary.pack(fill="x", pady=(4, 0))

        # ---- 表格 ----
        table_card = tk.Frame(self.root, bg=CLR_CARD,
                              highlightbackground=CLR_BORDER, highlightthickness=1)
        table_card.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        cols = ("domain", "ip", "verify", "status")
        self.tree = ttk.Treeview(table_card, columns=cols, show="headings",
                                 selectmode="browse")
        self.tree.heading("domain", text="域名")
        self.tree.heading("ip", text="解析 IP")
        self.tree.heading("verify", text="校验结果")
        self.tree.heading("status", text="状态")
        self.tree.column("domain", width=250, anchor="w")
        self.tree.column("ip", width=160, anchor="w")
        self.tree.column("verify", width=160, anchor="w")
        self.tree.column("status", width=100, anchor="center")

        vsb = ttk.Scrollbar(table_card, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        vsb.pack(side="right", fill="y", pady=1, padx=(0, 1))

        self.tree.tag_configure("ok", foreground=CLR_OK)
        self.tree.tag_configure("bad", foreground=CLR_ERR)
        self.tree.tag_configure("warn", foreground=CLR_WARN)
        self.tree.tag_configure("dim", foreground=CLR_TEXT_DIM)

        # ---- 按钮区 ----
        btns = tk.Frame(self.root, bg=CLR_BG)
        btns.pack(fill="x", padx=18, pady=(0, 10))

        self.btn_check = ttk.Button(btns, text="检查", style="Normal.TButton",
                                    command=self.on_check)
        self.btn_check.pack(side="left", padx=(0, 8))

        self.btn_update = ttk.Button(btns, text="更新 hosts", style="Primary.TButton",
                                     command=self.on_update)
        self.btn_update.pack(side="left", padx=(0, 8))

        self.btn_flush = ttk.Button(btns, text="刷新 DNS", style="Normal.TButton",
                                    command=self.on_flush)
        self.btn_flush.pack(side="left", padx=(0, 8))

        self.btn_rollback = ttk.Button(btns, text="回滚", style="Normal.TButton",
                                       command=self.on_rollback)
        self.btn_rollback.pack(side="left", padx=(0, 8))

        self.btn_task = ttk.Button(btns, text="计划任务", style="Normal.TButton",
                                   command=self.on_task)
        self.btn_task.pack(side="left", padx=(0, 8))

        self.btn_export = ttk.Button(btns, text="导出报告", style="Normal.TButton",
                                     command=self.on_export)
        self.btn_export.pack(side="left")

        self.btn_status = ttk.Button(btns, text="查看已写入", style="Normal.TButton",
                                     command=self.on_status)
        self.btn_status.pack(side="right")

        # ---- 日志区 ----
        log_card = tk.Frame(self.root, bg=CLR_CARD,
                            highlightbackground=CLR_BORDER, highlightthickness=1)
        log_card.pack(fill="both", expand=False, padx=18, pady=(0, 16))

        log_head = tk.Frame(log_card, bg=CLR_CARD)
        log_head.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(log_head, text="运行日志", bg=CLR_CARD, fg=CLR_TEXT_DIM,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        tk.Button(log_head, text="清空", bg=CLR_CARD, fg=CLR_TEXT_DIM,
                  activebackground=CLR_CARD, relief="flat", bd=0, cursor="hand2",
                  font=("Microsoft YaHei UI", 8),
                  command=self._clear_log).pack(side="right")

        self.txt_log = tk.Text(log_card, height=9, bg="#fafbfc", fg=CLR_TEXT,
                               font=FONT_MONO, relief="flat", bd=0,
                               wrap="word", state="disabled",
                               highlightthickness=0)
        self.txt_log.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        self.txt_log.tag_configure("ok", foreground=CLR_OK)
        self.txt_log.tag_configure("err", foreground=CLR_ERR)
        self.txt_log.tag_configure("warn", foreground=CLR_WARN)
        self.txt_log.tag_configure("dim", foreground=CLR_TEXT_DIM)
        self.txt_log.tag_configure("info", foreground=CLR_TEXT)

    # ------------------------------------------------------------ 日志

    def log(self, msg, level="info"):
        self.log_queue.put((msg, level))

    def _poll_log(self):
        try:
            while True:
                msg, level = self.log_queue.get_nowait()
                self.txt_log.configure(state="normal")
                ts = datetime.now().strftime("%H:%M:%S")
                self.txt_log.insert("end", "[%s] " % ts, "dim")
                self.txt_log.insert("end", msg + "\n", level)
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def _clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------ 状态刷新

    def _refresh_admin_label(self):
        admin = core.is_admin() if core else False
        if admin:
            self.lbl_admin.configure(text="● 管理员权限", fg=CLR_OK)
        else:
            self.lbl_admin.configure(text="● 普通权限（更新时需提权）", fg=CLR_WARN)

    def _refresh_hosts_label(self):
        if not core:
            return
        content = core.read_hosts()
        if content is None:
            self.lbl_hosts.configure(text="hosts 路径: 读取失败")
            return
        _, has = core.extract_block(content)
        baks = []
        if os.path.isdir(core.BACKUP_DIR):
            baks = [f for f in os.listdir(core.BACKUP_DIR)
                    if f.startswith("hosts.")]
        self.lbl_hosts.configure(
            text="hosts: %s  |  %d 字节  |  已写入标记区块: %s  |  备份: %d 份"
                 % (core.HOSTS_PATH, len(content), "是" if has else "否", len(baks)))

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_check, self.btn_update, self.btn_flush,
                  self.btn_rollback, self.btn_task, self.btn_export,
                  self.btn_status):
            b.configure(state=state)
        self.root.configure(cursor="watch" if busy else "")

    def _fill_table(self, rows):
        """rows: [(domain, ip, verify, status_text, tag)]"""
        for i in self.tree.get_children():
            self.tree.delete(i)
        for domain, ip, verify, status, tag in rows:
            self.tree.insert("", "end", values=(domain, ip, verify, status),
                             tags=(tag,))

    # ------------------------------------------------------------ 检查

    def on_check(self):
        if self.busy:
            return
        if not core:
            messagebox.showerror("错误", "核心模块 ghhosts 导入失败:\n%s" % _import_err)
            return
        self._set_busy(True)
        self._refresh_admin_label()
        self._refresh_hosts_label()
        self.log("开始检查 GitHub 解析状态 ...", "info")
        self.log("校验规则: 保留地址拦截 + 已知网段白名单 + 多源 DNS 交叉验证", "dim")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            self.log("正在获取 GitHub 官方 IP 网段 ...", "info")
            cidrs, meta_src, meta_err = core.fetch_github_meta()
            if cidrs:
                self.log("官方网段: %d 条 (来源: %s)" % (len(cidrs), meta_src), "dim")
            else:
                self.log("官方网段获取失败(%s), 仅依赖多源校验" % meta_err, "warn")

            whitelist = list(core.KNOWN_GITHUB_CIDRS)
            if cidrs:
                whitelist = sorted(set(whitelist) | set(cidrs))

            RESERVED = ("0.", "127.", "10.", "169.254.", "255.", "224.",
                        "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                        "172.2", "172.30.", "172.31.")

            rows = []
            entries = []
            poisoned = []

            for domain in core.TARGET_DOMAINS:
                ip, detail, verdict = core.majority_ip(domain)

                if ip is None:
                    rows.append((domain, "-", "解析失败", "跳过", "warn"))
                    self.log("%s 解析失败, 跳过" % domain, "warn")
                    continue

                if ip.startswith(RESERVED):
                    rows.append((domain, ip, "保留地址", "拒绝", "bad"))
                    self.log("%s -> %s 保留地址, 拒绝" % (domain, ip), "err")
                    poisoned.append(domain)
                    continue

                if not core.ip_in_cidrs(ip, whitelist):
                    rows.append((domain, ip, "网段外", "投毒", "bad"))
                    self.log("%s -> %s 不在已知 GitHub 网段内, 判定投毒" % (domain, ip), "err")
                    poisoned.append(domain)
                    continue

                if verdict == "POISONED":
                    rows.append((domain, ip, "多源冲突", "投毒", "bad"))
                    self.log("%s 多源返回互不相同, 判定投毒" % domain, "err")
                    poisoned.append(domain)
                    continue

                if verdict != "CONSISTENT":
                    rows.append((domain, ip, verdict, "拒绝", "warn"))
                    self.log("%s -> %s 多源不一致, 拒绝" % (domain, ip), "warn")
                    continue

                rows.append((domain, ip, "白名单通过", "可用", "ok"))
                self.log("%s -> %s 校验通过" % (domain, ip), "ok")
                entries.append((ip, domain, "%s; 白名单校验通过" % verdict))

            self.entries = entries
            self.poisoned = poisoned
            self.report = {"meta_source": meta_src, "meta_cidr_count": len(cidrs)}

            self.root.after(0, lambda: self._fill_table(rows))

            def done():
                if entries:
                    self.lbl_summary.configure(
                        text="检查完成 — %d 个域名可用%s"
                             % (len(entries),
                                ("，%d 个检测到投毒" % len(poisoned)) if poisoned else ""),
                        fg=CLR_OK if not poisoned else CLR_WARN)
                else:
                    self.lbl_summary.configure(
                        text="检查完成 — 没有可用记录", fg=CLR_ERR)
                if poisoned:
                    self.log("检测到投毒域名: %s" % ", ".join(poisoned), "err")
                    self.log("hosts 无法解决投毒, 建议启用加密 DNS (DoH/DoT)", "warn")
                self.log("检查结束: %d 条待写入" % len(entries), "info")
                self._refresh_hosts_label()
                self._set_busy(False)
            self.root.after(0, done)
        except Exception as e:
            self.root.after(0, lambda: self._on_error("检查", e))

    def _on_error(self, action, exc):
        self.log("%s 出错: %s" % (action, exc), "err")
        self.lbl_summary.configure(text="%s 失败" % action, fg=CLR_ERR)
        self._set_busy(False)
        messagebox.showerror("错误", "%s 失败:\n%s" % (action, exc))

    # ------------------------------------------------------------ 更新

    def on_update(self):
        if self.busy:
            return
        if not self.entries:
            messagebox.showinfo("提示", "没有可写入的记录。\n请先点击「检查」。")
            return

        preview = "\n".join("  %-18s %s" % (ip, d) for ip, d, _ in self.entries)
        extra = ""
        if self.poisoned:
            extra = "\n\n以下域名检测到投毒，将被跳过:\n  " + ", ".join(self.poisoned)

        if not messagebox.askyesno(
                "确认更新 hosts",
                "将写入 %d 条记录:\n\n%s%s\n\n"
                "· 只修改独立标记区块，不影响你的自定义条目\n"
                "· 写入前会自动备份\n\n确认继续?"
                % (len(self.entries), preview, extra)):
            self.log("用户取消更新", "dim")
            return

        if not core.is_admin():
            self.log("当前非管理员权限，正在请求提权 ...", "warn")
            if self._relaunch_as_admin("--yes"):
                self.root.after(500, self.root.destroy)
            else:
                messagebox.showerror(
                    "权限不足",
                    "写入 hosts 需要管理员权限。\n\n"
                    "请右键 exe → 以管理员身份运行，\n"
                    "或在弹出的 UAC 对话框中选择「是」。")
            return

        self._set_busy(True)
        self.log("开始写入 hosts ...", "info")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _relaunch_as_admin(self, extra_arg=None):
        """以管理员身份重新启动自身"""
        import ctypes
        import subprocess
        try:
            if getattr(sys, "frozen", False):
                exe = sys.executable
                params = extra_arg or ""
            else:
                exe = sys.executable
                params = '"%s"' % os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "ghhosts_gui.py"))
                if extra_arg:
                    params += " " + extra_arg
            # 用 ghhosts 的控制台版做实际写入更稳妥
            if getattr(sys, "frozen", False):
                ok = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", exe, params, None, 1)
            else:
                ok = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", exe, params, None, 1)
            return ok > 32
        except Exception as e:
            self.log("提权失败: %s" % e, "err")
            return False

    def _update_worker(self):
        try:
            content = core.read_hosts()
            if content is None:
                raise RuntimeError("读取 hosts 失败")

            bak = core.backup_hosts()
            if bak:
                self.log("已备份: %s" % os.path.basename(bak), "ok")
            else:
                raise RuntimeError("备份失败，已中止以保护系统")

            block = core.build_block(self.entries,
                                     self.report.get("meta_source") or "public-dns")
            base, _ = core.strip_block(content)
            new_content = base.rstrip("\r\n") + "\r\n\r\n" + block + "\r\n"

            ok, err = core.write_hosts(new_content)
            if not ok:
                raise RuntimeError(err or "写入失败")
            self.log("hosts 写入成功", "ok")

            if core.flush_dns():
                self.log("DNS 缓存已刷新", "ok")
            else:
                self.log("DNS 缓存刷新失败，请手动执行 ipconfig /flushdns", "warn")

            # 验证
            import socket
            ok_cnt = fail_cnt = 0
            for d in core.TARGET_DOMAINS:
                if d in self.poisoned:
                    continue
                try:
                    ip = socket.gethostbyname(d)
                    self.log("验证 %s -> %s" % (d, ip), "ok")
                    ok_cnt += 1
                except Exception:
                    self.log("验证 %s 解析失败" % d, "err")
                    fail_cnt += 1

            def done():
                self.lbl_summary.configure(
                    text="更新完成 — %d 个域名验证通过%s"
                         % (ok_cnt, ("，%d 个失败" % fail_cnt) if fail_cnt else ""),
                    fg=CLR_OK if not fail_cnt else CLR_WARN)
                self._refresh_hosts_label()
                self._set_busy(False)
                messagebox.showinfo("完成",
                                    "hosts 更新成功。\n\n"
                                    "写入 %d 条记录，%d 个域名验证通过。\n"
                                    "备份已保存，如需还原可点击「回滚」。"
                                    % (len(self.entries), ok_cnt))
            self.root.after(0, done)
        except Exception as e:
            self.root.after(0, lambda: self._on_error("更新", e))

    # ------------------------------------------------------------ 其他动作

    def on_flush(self):
        if core.flush_dns():
            self.log("DNS 缓存已刷新", "ok")
            self.lbl_summary.configure(text="DNS 缓存已刷新", fg=CLR_OK)
        else:
            self.log("刷新失败，请尝试以管理员身份运行", "err")

    def on_rollback(self):
        bak = core.latest_backup()
        if not bak:
            messagebox.showinfo("提示", "未找到备份文件。\n备份目录:\n%s" % core.BACKUP_DIR)
            return
        if not messagebox.askyesno(
                "确认回滚",
                "将恢复 hosts 到上次备份:\n\n  %s\n\n"
                "这会撤销本工具写入的所有 GitHub 记录。\n确认继续?"
                % os.path.basename(bak)):
            return
        if not core.is_admin():
            messagebox.showerror("权限不足", "回滚 hosts 需要管理员权限。\n"
                                            "请以管理员身份运行本程序。")
            return
        try:
            import shutil
            shutil.copy2(bak, core.HOSTS_PATH)
            core.flush_dns()
            self.log("已回滚到: %s" % os.path.basename(bak), "ok")
            self.lbl_summary.configure(text="已回滚到上次备份", fg=CLR_OK)
            self._refresh_hosts_label()
            messagebox.showinfo("完成", "hosts 已恢复。")
        except Exception as e:
            self._on_error("回滚", e)

    def on_task(self):
        if not core.is_admin():
            messagebox.showerror("权限不足", "管理计划任务需要管理员权限。")
            return
        win = tk.Toplevel(self.root)
        win.title("计划任务")
        win.geometry("480x240")
        win.configure(bg=CLR_BG)
        win.transient(self.root)
        win.grab_set()
        self.root.update_idletasks()
        x = self.root.winfo_x() + (900 - 480) // 2
        y = self.root.winfo_y() + 160
        win.geometry("+%d+%d" % (x, y))

        tk.Label(win, text="自动更新计划任务", bg=CLR_BG, fg=CLR_TEXT,
                 font=FONT_UI_BOLD).pack(pady=(18, 6))
        tk.Label(win, text="注册后每周一 09:00 自动检查并更新 hosts。\n"
                           "任务名称: %s" % core.TASK_NAME,
                 bg=CLR_BG, fg=CLR_TEXT_DIM,
                 font=("Microsoft YaHei UI", 9)).pack()

        bf = tk.Frame(win, bg=CLR_BG)
        bf.pack(pady=20)

        def do_install():
            class A:
                pass
            rc = core.cmd_install_task(A())
            if rc == 0:
                self.log("计划任务已注册", "ok")
                messagebox.showinfo("完成", "计划任务已注册:\n每周一 09:00 自动更新")
            else:
                self.log("计划任务注册失败", "err")
                messagebox.showerror("失败", "注册失败，请检查权限。")
            win.destroy()

        def do_uninstall():
            class A:
                pass
            rc = core.cmd_uninstall_task(A())
            if rc == 0:
                self.log("计划任务已移除", "ok")
                messagebox.showinfo("完成", "计划任务已移除。")
            else:
                self.log("计划任务移除失败", "err")
                messagebox.showerror("失败", "移除失败（可能不存在）。")
            win.destroy()

        ttk.Button(bf, text="注册任务", style="Primary.TButton",
                   command=do_install).pack(side="left", padx=6)
        ttk.Button(bf, text="移除任务", style="Normal.TButton",
                   command=do_uninstall).pack(side="left", padx=6)
        ttk.Button(bf, text="关闭", style="Normal.TButton",
                   command=win.destroy).pack(side="left", padx=6)

    def on_export(self):
        if not self.entries and not self.poisoned:
            messagebox.showinfo("提示", "暂无数据，请先执行「检查」。")
            return
        path = filedialog.asksaveasfilename(
            title="导出检查报告",
            defaultextension=".json",
            initialfile="ghhosts-report-%s.json"
                        % datetime.now().strftime("%Y%m%d-%H%M%S"),
            filetypes=[("JSON 文件", "*.json"), ("文本文件", "*.txt")])
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("GitHub Hosts 检查报告\n")
                    f.write("时间: %s\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    f.write("=" * 50 + "\n")
                    for ip, d, note in self.entries:
                        f.write("%-18s %-32s %s\n" % (ip, d, note))
                    if self.poisoned:
                        f.write("\n检测到投毒(已跳过): %s\n" % ", ".join(self.poisoned))
            else:
                import json
                data = {
                    "time": datetime.now().isoformat(),
                    "hosts_path": core.HOSTS_PATH,
                    "entries": [{"ip": ip, "domain": d, "verify": n}
                                for ip, d, n in self.entries],
                    "poisoned": self.poisoned,
                    "meta": self.report,
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            self.log("报告已导出: %s" % path, "ok")
            messagebox.showinfo("完成", "报告已导出:\n%s" % path)
        except Exception as e:
            self._on_error("导出", e)

    def on_status(self):
        content = core.read_hosts()
        if content is None:
            messagebox.showerror("错误", "读取 hosts 失败")
            return
        block, has = core.extract_block(content)
        if not has:
            messagebox.showinfo("当前状态", "hosts 中未找到本工具写入的区块。")
            return
        win = tk.Toplevel(self.root)
        win.title("hosts 中已写入的内容")
        win.geometry("620x400")
        win.configure(bg=CLR_BG)
        win.transient(self.root)
        txt = tk.Text(win, bg=CLR_CARD, fg=CLR_TEXT, font=FONT_MONO,
                      relief="flat", wrap="none", padx=12, pady=12)
        txt.pack(fill="both", expand=True, padx=12, pady=12)
        txt.insert("1.0", block)
        txt.configure(state="disabled")


# ---------------------------------------------------------------- 入口

def main():
    if os.name != "nt":
        print("本程序为 Windows 专用。")
        return 3
    if core is None:
        print("无法导入核心模块 ghhosts.py: %s" % _import_err)
        return 1

    # 命令行透传: 带 --console 或任何已知命令行参数时, 走控制台模式
    argv = sys.argv[1:]
    console_flags = {"--console", "--check", "--rollback", "--status",
                     "--install-task", "--uninstall-task", "--silent",
                     "--yes", "--json", "--help", "-h"}
    if "--console" in argv or any(a in console_flags for a in argv):
        args = [a for a in argv if a != "--console"]
        sys.argv = [sys.argv[0]] + args
        return core.main()

    root = tk.Tk()
    try:
        # 高 DPI 适配
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = HostsApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
