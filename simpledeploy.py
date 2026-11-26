#!/usr/bin/env python3
"""
SimpleDeploy - a small, serverless, cross-platform alternative to PDQ Deploy.

Copies an installer to Windows machines over SMB (ADMIN$ share), runs it
remotely as SYSTEM via a temporary service (PsExec-style), reports the exit
code, and cleans up. Runs from Windows, Linux or macOS.

Install:   pip install pypsexec smbprotocol sv-ttk   (sv-ttk = modern theme, optional)
Linux GUI: sudo apt install python3-tk
Run:       python3 simpledeploy.py

Targets need: SMB (TCP 445) reachable, ADMIN$ share enabled, admin credentials.
"""
import json
import queue
import shutil
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import smbclient
from pypsexec.client import Client

try:
    import sv_ttk  # modern Windows 11-style theme
except ImportError:
    sv_ttk = None

ACCENT = "Accent.TButton" if sv_ttk else "TButton"
ICONS = {"Queued": "○ Queued", "Connecting": "◔ Connecting", "Copying": "◑ Copying",
         "Installing": "◕ Installing", "Done": "● Done", "Failed": "✖ Failed", "Skipped": "– Skipped"}

CONFIG_DIR = Path.home() / ".simpledeploy"
PKG_FILE = CONFIG_DIR / "packages.json"
HOSTS_FILE = CONFIG_DIR / "hosts.txt"
REMOTE_LOCAL_DIR = r"C:\Windows\Temp\SimpleDeploy"  # same folder as ADMIN$\Temp\SimpleDeploy


# --------------------------------------------------------------------------
# Deployment logic
# --------------------------------------------------------------------------
def build_command(pkg, remote_path):
    ext = Path(pkg["file"]).suffix.lower()
    args = pkg.get("args", "").strip()
    if ext == ".msi":
        return "msiexec.exe", f'/i "{remote_path}" {args}'.strip()
    if ext == ".ps1":
        return "powershell.exe", f'-NoProfile -ExecutionPolicy Bypass -File "{remote_path}" {args}'.strip()
    if ext in (".bat", ".cmd"):
        return "cmd.exe", f'/c ""{remote_path}" {args}"'
    return remote_path, args


def parse_codes(text):
    try:
        return {int(x) for x in text.replace(" ", "").split(",") if x}
    except ValueError:
        return {0}


def deploy_one(host, pkg, user, pw, encrypt, timeout_min, report):
    """Deploy to one host. Returns (ok, exit_code, message)."""
    name = Path(pkg["file"]).name
    unc_dir = rf"\\{host}\ADMIN$\Temp\SimpleDeploy"
    unc_file = unc_dir + "\\" + name
    remote_path = REMOTE_LOCAL_DIR + "\\" + name
    copied = False
    try:
        report("Connecting")
        smbclient.register_session(host, username=user, password=pw,
                                   encrypt=encrypt, connection_timeout=15)
        report("Copying")
        smbclient.makedirs(unc_dir, exist_ok=True)
        with open(pkg["file"], "rb") as src, smbclient.open_file(unc_file, mode="wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        copied = True

        if not pkg.get("run", True):
            return True, None, f"Copied to {remote_path}"
            # (copy-only packages keep the file in place; cleanup skipped below)

        report("Installing")
        exe, args = build_command(pkg, remote_path)
        c = Client(host, username=user, password=pw, encrypt=encrypt)
        c.connect()
        warn = ""
        try:
            c.create_service()
            out, err, rc = c.run_executable(
                exe, arguments=args or None, use_system_account=True,
                timeout_seconds=int(timeout_min * 60))
        finally:
            # The runner service can stay locked for a moment after the installer
            # exits. Retry, and never let a cleanup problem hide the real result.
            for attempt in range(6):
                try:
                    c.remove_service()
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 5:
                        warn = f" [cleanup warning: {type(e).__name__}; PAExec files may remain in C:\\Windows]"
                    else:
                        time.sleep(2)
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        ok = rc in parse_codes(pkg.get("codes", "0,3010"))
        msg = "Success" if ok else "Failed"
        if rc == 3010:
            msg += " (reboot required)"
        if not ok and err:
            msg += ": " + err.decode(errors="replace").strip()[:200]
        return ok, rc, msg + warn
    except Exception as e:  # noqa: BLE001 - show any failure in the GUI
        return False, None, f"{type(e).__name__}: {e}"[:300]
    finally:
        if copied and pkg.get("run", True):
            try:
                smbclient.remove(unc_file)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# Package storage
# --------------------------------------------------------------------------
def load_packages():
    try:
        return json.loads(PKG_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def save_packages(pkgs):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PKG_FILE.write_text(json.dumps(pkgs, indent=2), encoding="utf-8")


class PackageDialog(tk.Toplevel):
    def __init__(self, parent, pkg=None):
        super().__init__(parent)
        self.title("Package")
        self.result = None
        self.transient(parent)
        self.grab_set()
        self.configure(padx=14, pady=12)
        pkg = pkg or {"name": "", "file": "", "args": "", "codes": "0,3010", "run": True}
        self.vars = {k: tk.StringVar(value=str(pkg.get(k, ""))) for k in ("name", "file", "args", "codes")}
        self.run = tk.BooleanVar(value=pkg.get("run", True))
        rows = [("Name", "name"), ("File", "file"), ("Silent arguments", "args"), ("Success exit codes", "codes")]
        for i, (label, key) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(self, textvariable=self.vars[key], width=52).grid(row=i, column=1, padx=8, pady=4)
        ttk.Button(self, text="Browse...", command=self.browse).grid(row=1, column=2, padx=4)
        ttk.Checkbutton(self, text="Run after copying (uncheck for copy-only)", variable=self.run
                        ).grid(row=4, column=1, sticky="w", padx=8)
        ttk.Label(self, foreground="gray",
                  text="Examples: /S   |   /quiet /norestart   |   /qn /norestart (for .msi)"
                  ).grid(row=5, column=1, sticky="w", padx=8)
        bar = ttk.Frame(self)
        bar.grid(row=6, column=0, columnspan=3, pady=10)
        ttk.Button(self, text="Save", command=self.ok, style=ACCENT).grid(in_=bar, row=0, column=0, padx=4)
        ttk.Button(self, text="Cancel", command=self.destroy).grid(in_=bar, row=0, column=1, padx=4)
        self.wait_window()

    def browse(self):
        f = filedialog.askopenfilename(parent=self)
        if f:
            self.vars["file"].set(f)
            if not self.vars["name"].get():
                self.vars["name"].set(Path(f).stem)

    def ok(self):
        d = {k: v.get().strip() for k, v in self.vars.items()}
        if not d["name"] or not Path(d["file"]).is_file():
            messagebox.showerror("Package", "Name and an existing file are required.", parent=self)
            return
        d["run"] = self.run.get()
        self.result = d
        self.destroy()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("SimpleDeploy")
        root.geometry("1080x700")
        root.minsize(900, 560)
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.packages = load_packages()
        self.running = False
        self.theme = "dark" if sv_ttk else "light"
        if sv_ttk:
            sv_ttk.set_theme(self.theme)
        ttk.Style().configure("Treeview", rowheight=28)
        self.build_ui()
        self.apply_colors()
        self.refresh_packages()
        try:
            self.hosts.insert("1.0", HOSTS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        root.after(100, self.poll)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 14))
        ttk.Label(head, text="SimpleDeploy", font=("Segoe UI", 20, "bold")).pack(side="left")
        ttk.Label(head, text="   push installers to Windows PCs", foreground="gray"
                  ).pack(side="left", pady=(10, 0))
        if sv_ttk:
            ttk.Button(head, text="◐  Theme", command=self.toggle_theme).pack(side="right")

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # Targets
        left = ttk.LabelFrame(body, text=" Targets ", padding=12)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 14))
        ttk.Label(left, text="One host or IP per line", foreground="gray").pack(anchor="w", pady=(0, 6))
        self.hosts = tk.Text(left, width=24, height=10, relief="flat", borderwidth=0,
                             padx=10, pady=8, font="TkFixedFont", highlightthickness=0)
        self.hosts.pack(fill="both", expand=True)
        ttk.Button(left, text="Load from file…", command=self.load_hosts).pack(fill="x", pady=(10, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        # Package
        pk = ttk.LabelFrame(right, text=" Package ", padding=12)
        pk.pack(fill="x")
        self.pkg_var = tk.StringVar()
        self.pkg_box = ttk.Combobox(pk, textvariable=self.pkg_var, state="readonly")
        self.pkg_box.grid(row=0, column=0, sticky="we")
        for i, (t, cmd) in enumerate([("＋ Add", self.add_pkg), ("Edit", self.edit_pkg), ("Delete", self.del_pkg)], 1):
            ttk.Button(pk, text=t, command=cmd).grid(row=0, column=i, padx=(8, 0))
        pk.columnconfigure(0, weight=1)

        # Credentials
        cr = ttk.LabelFrame(right, text=" Credentials & options ", padding=12)
        cr.pack(fill="x", pady=12)
        self.user = tk.StringVar()
        self.pw = tk.StringVar()
        self.workers = tk.IntVar(value=5)
        self.timeout = tk.IntVar(value=30)
        self.encrypt = tk.BooleanVar(value=True)
        cr.columnconfigure(1, weight=1)
        cr.columnconfigure(3, weight=1)
        ttk.Label(cr, text="User").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(cr, textvariable=self.user).grid(row=0, column=1, sticky="we", padx=(8, 16))
        ttk.Label(cr, text="Password").grid(row=0, column=2, sticky="w")
        ttk.Entry(cr, textvariable=self.pw, show="•").grid(row=0, column=3, sticky="we", padx=(8, 0))
        ttk.Label(cr, text="Parallel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(cr, from_=1, to=50, textvariable=self.workers, width=6).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(cr, text="Timeout (min)").grid(row=1, column=2, sticky="w")
        ttk.Spinbox(cr, from_=1, to=600, textvariable=self.timeout, width=6).grid(row=1, column=3, sticky="w", padx=8)
        ttk.Checkbutton(cr, text="SMB encryption (turn off for Windows 7 / Server 2008 R2)",
                        variable=self.encrypt).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(cr, text="Use DOMAIN\\user or user@domain. Password is never saved.",
                  foreground="gray").grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # Actions
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        self.go = ttk.Button(bar, text="▶  Deploy", command=self.deploy, style=ACCENT)
        self.go.pack(side="left")
        self.cancel = ttk.Button(bar, text="Stop queued", command=self.stop.set, state="disabled")
        self.cancel.pack(side="left", padx=8)
        self.summary = ttk.Label(bar, text="")
        self.summary.pack(side="right")
        self.progress = ttk.Progressbar(right, mode="determinate")
        self.progress.pack(fill="x", pady=(10, 8))

        # Results
        box = ttk.Frame(right)
        box.pack(fill="both", expand=True)
        cols = ("host", "status", "code", "detail")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=12)
        for c, w, st in (("host", 170, False), ("status", 120, False), ("code", 60, False), ("detail", 420, True)):
            self.tree.heading(c, text=c.capitalize(), anchor="w")
            self.tree.column(c, width=w, anchor="w", stretch=st)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Copying: Ctrl+C, right-click menu, double-click for the full message
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Copy error / detail", command=lambda: self.copy_rows(self.tree.selection(), True))
        self.menu.add_command(label="Copy row", command=lambda: self.copy_rows(self.tree.selection()))
        self.menu.add_command(label="Copy all results", command=lambda: self.copy_rows(self.tree.get_children()))
        self.menu.add_separator()
        self.menu.add_command(label="Show full message…", command=self.show_detail)
        self.tree.bind("<Control-c>", lambda e: self.copy_rows(self.tree.selection(), True))
        self.tree.bind("<Double-1>", self.show_detail)
        for btn in ("<Button-3>", "<Button-2>"):  # right-click (Windows/Linux, macOS)
            self.tree.bind(btn, self.popup_menu)

    # --- copying results
    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # keeps the clipboard content after the window closes

    def copy_rows(self, iids, detail_only=False):
        lines = []
        for i in iids:
            v = self.tree.item(i, "values")
            lines.append(str(v[3]) if detail_only else "\t".join(str(x) for x in v))
        if lines:
            self.copy_text("\n".join(lines))

    def popup_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    def show_detail(self, event=None):
        row = self.tree.identify_row(event.y) if event is not None else (self.tree.selection() or [""])[0]
        if not row:
            return
        host, status, code, detail = self.tree.item(row, "values")
        win = tk.Toplevel(self.root)
        win.title(f"{host}: {status}")
        win.geometry("720x320")
        win.configure(padx=12, pady=12)
        dark = self.theme == "dark"
        txt = tk.Text(win, wrap="word", relief="flat", borderwidth=0, padx=10, pady=8,
                      background="#2b2b2b" if dark else "#ffffff",
                      foreground="#f2f2f2" if dark else "#1f1f1f",
                      insertbackground="#f2f2f2" if dark else "#1f1f1f",
                      selectbackground="#0067c0", highlightthickness=0)
        full = f"Host: {host}\nStatus: {status}\nExit code: {code}\n\n{detail}"
        txt.insert("1.0", full)
        txt.configure(state="disabled")  # selectable and copyable, not editable
        txt.pack(fill="both", expand=True)
        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="Copy", style=ACCENT, command=lambda: self.copy_text(full)).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="left", padx=8)
        txt.focus_set()

    def apply_colors(self):
        dark = self.theme == "dark"
        bg, fg = ("#2b2b2b", "#f2f2f2") if dark else ("#ffffff", "#1f1f1f")
        self.hosts.configure(background=bg, foreground=fg, insertbackground=fg, selectbackground="#0067c0")
        self.tree.tag_configure("ok", foreground="#4ade80" if dark else "#1a7f37")
        self.tree.tag_configure("fail", foreground="#f87171" if dark else "#cf222e")
        self.tree.tag_configure("run", foreground="#60a5fa" if dark else "#0a58ca")

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        sv_ttk.set_theme(self.theme)
        ttk.Style().configure("Treeview", rowheight=28)
        self.apply_colors()

    # --- packages
    def refresh_packages(self):
        names = [p["name"] for p in self.packages]
        self.pkg_box["values"] = names
        if names and self.pkg_var.get() not in names:
            self.pkg_var.set(names[0])

    def current_pkg(self):
        return next((p for p in self.packages if p["name"] == self.pkg_var.get()), None)

    def add_pkg(self):
        d = PackageDialog(self.root)
        if d.result:
            self.packages.append(d.result)
            save_packages(self.packages)
            self.refresh_packages()
            self.pkg_var.set(d.result["name"])

    def edit_pkg(self):
        p = self.current_pkg()
        if p:
            d = PackageDialog(self.root, p)
            if d.result:
                self.packages[self.packages.index(p)] = d.result
                save_packages(self.packages)
                self.refresh_packages()
                self.pkg_var.set(d.result["name"])

    def del_pkg(self):
        p = self.current_pkg()
        if p and messagebox.askyesno("Delete", f"Delete package '{p['name']}'?"):
            self.packages.remove(p)
            save_packages(self.packages)
            self.pkg_var.set("")
            self.refresh_packages()

    def load_hosts(self):
        f = filedialog.askopenfilename()
        if f:
            self.hosts.delete("1.0", "end")
            self.hosts.insert("1.0", Path(f).read_text(encoding="utf-8", errors="replace"))

    # --- deployment
    def host_list(self):
        seen, out = set(), []
        for line in self.hosts.get("1.0", "end").splitlines():
            h = line.strip()
            if h and not h.startswith("#") and h.lower() not in seen:
                seen.add(h.lower())
                out.append(h)
        return out

    def deploy(self):
        pkg, hosts = self.current_pkg(), self.host_list()
        if not pkg or not hosts or not self.user.get() or not self.pw.get():
            messagebox.showwarning("Deploy", "Select a package, add hosts, and enter credentials.")
            return
        if not Path(pkg["file"]).is_file():
            messagebox.showerror("Deploy", f"File not found:\n{pkg['file']}")
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HOSTS_FILE.write_text("\n".join(hosts), encoding="utf-8")
        self.tree.delete(*self.tree.get_children())
        for h in hosts:
            self.tree.insert("", "end", iid=h, values=(h, ICONS["Queued"], "", ""))
        self.stop.clear()
        self.running = True
        self.go.config(state="disabled")
        self.cancel.config(state="normal")
        self.counts = {"ok": 0, "fail": 0, "skip": 0, "total": len(hosts)}
        self.update_summary()
        args = (hosts, pkg, self.user.get(), self.pw.get(), self.encrypt.get(),
                self.timeout.get(), max(1, self.workers.get()))
        threading.Thread(target=self.run_all, args=args, daemon=True).start()

    def run_all(self, hosts, pkg, user, pw, encrypt, timeout, workers):
        smbclient.reset_connection_cache()

        def job(host):
            if self.stop.is_set():
                self.q.put((host, "Skipped", "", "Stopped before start", None))
                return
            rep = lambda s: self.q.put((host, s, "", "", None))  # noqa: E731
            ok, rc, msg = deploy_one(host, pkg, user, pw, encrypt, timeout, rep)
            self.q.put((host, "Done" if ok else "Failed", "" if rc is None else rc, msg, ok))

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(job, hosts))
        self.q.put(None)

    def update_summary(self):
        c = self.counts
        done = c["ok"] + c["fail"] + c["skip"]
        text = f"{done}/{c['total']} done  ·  {c['ok']} ok  ·  {c['fail']} failed"
        if c["skip"]:
            text += f"  ·  {c['skip']} skipped"
        self.summary.config(text=text)
        self.progress.config(maximum=max(1, c["total"]), value=done)

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self.running = False
                    self.go.config(state="normal")
                    self.cancel.config(state="disabled")
                    continue
                host, status, code, detail, ok = item
                if ok is None:
                    tag = ("run",) if status in ("Connecting", "Copying", "Installing") else ()
                else:
                    tag = ("ok",) if ok else ("fail",)
                if self.tree.exists(host):
                    cur = self.tree.item(host, "values")
                    self.tree.item(host, values=(host, ICONS.get(status, status), code, detail or cur[3]), tags=tag)
                if ok is True:
                    self.counts["ok"] += 1
                elif ok is False:
                    self.counts["fail"] += 1
                elif status == "Skipped":
                    self.counts["skip"] += 1
                self.update_summary()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


if __name__ == "__main__":
    try:  # crisp text on high-DPI Windows screens
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()