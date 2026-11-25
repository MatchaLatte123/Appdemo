"""AppDemo window for Adaptive AI Security."""
import os
import tkinter as tk

if __package__:
    from .adaptive_security_panel import AdaptiveSecurityPanel
else:
    from adaptive_security_panel import AdaptiveSecurityPanel

DEFAULT_PORT = 8767


def open_dashboard(parent):
    current = getattr(parent, "_security_dashboard", None)
    if current is not None and current.winfo_exists():
        current.lift()
        return
    parent._security_dashboard = SecurityDashboard(parent)


class SecurityDashboard(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("AppDemo · AI Security V3")
        self.geometry("980x690")
        self.minsize(690, 480)
        self.panel = AdaptiveSecurityPanel(self)
        self.panel.pack(fill="both", expand=True)
        try:
            self.port = int(os.environ.get("SMARTHOME_AI_SERVER_PORT", DEFAULT_PORT))
            if not 1 <= self.port <= 65535:
                raise ValueError
        except ValueError:
            self.panel.summary.configure(text="Cổng AI Server không hợp lệ.")
            self.refresh_timer = None
        else:
            self.refresh()

    def refresh(self):
        self.panel.refresh(self.port)
        self.refresh_timer = self.after(2000, self.refresh)

    def destroy(self):
        if self.refresh_timer is not None:
            self.after_cancel(self.refresh_timer)
        super().destroy()
