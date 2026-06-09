"""Desk Connect GTK4 / libadwaita application."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import autostart, link  # noqa: E402
from .client import ClientEngine  # noqa: E402
from .config import Settings  # noqa: E402
from .linux_input import UINPUT_PATHS  # noqa: E402
from .server import ServerEngine  # noqa: E402

APP_ID = "io.github.mohanviswagnamr.DeskConnect"


def _hotkey_label(codes: list[int]) -> str:
    names = {29: "Ctrl", 97: "Ctrl", 56: "Alt", 100: "AltGr", 42: "Shift",
             54: "Shift", 125: "Super", 31: "S"}
    return " + ".join(names.get(c, f"key{c}") for c in codes)


class DeskConnectWindow(Adw.ApplicationWindow):
    def __init__(self, app: "DeskConnectApp"):
        super().__init__(application=app, title="Desk Connect")
        self.app = app
        self.settings = app.settings
        self.set_default_size(540, 720)

        self._server: ServerEngine | None = None
        self._client: ClientEngine | None = None
        self._remote_active = False
        self._client_connected = False
        self._cable_present = False  # for edge-triggered auto-connect

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        self._toast = Adw.ToastOverlay()
        content = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        page = Adw.PreferencesPage()
        content.set_child(page)
        self._toast.set_child(content)
        toolbar.set_content(self._toast)
        self.set_content(toolbar)

        self._build_role_group(page)
        self._build_link_group(page)
        self._build_server_group(page)
        self._build_client_group(page)
        self._build_automation_group(page)
        self._build_status_group(page)

        self._apply_role_visibility()
        self._refresh_link_info()
        self._check_permissions()

        # Poll for the USB-C cable so we can auto-connect when it is plugged in.
        GLib.timeout_add_seconds(2, self._poll_cable)

        if app.autostart_requested and self.settings.auto_connect:
            GLib.idle_add(self._maybe_autostart)

    # -- UI construction ---------------------------------------------------
    def _build_role_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(
            title="Role",
            description="Pick which side this computer plays on the cable.",
        )
        self._role_row = Adw.ComboRow(title="This computer is the")
        model = Gtk.StringList()
        model.append("Server — shares its keyboard & mouse")
        model.append("Client — is controlled by the other computer")
        self._role_row.set_model(model)
        self._role_row.set_selected(0 if self.settings.role == "server" else 1)
        self._role_row.connect("notify::selected", self._on_role_changed)
        group.add(self._role_row)
        page.add(group)

    def _build_link_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(
            title="USB-C / Thunderbolt link",
            description=(
                "Connect the two machines with a Thunderbolt 3/4 or USB4 cable. "
                "Each side gets a private network interface — no Wi-Fi involved."
            ),
        )
        self._link_row = Adw.ActionRow(title="Detected cable interface")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self._refresh_link_info())
        self._link_row.add_suffix(refresh)
        group.add(self._link_row)
        page.add(group)

    def _build_server_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Server settings")
        self._port_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self._port_row.set_title("Listen port")
        self._port_row.set_value(self.settings.listen_port)
        group.add(self._port_row)

        self._hotkey_row = Adw.ActionRow(
            title="Switch hotkey",
            subtitle=_hotkey_label(self.settings.switch_hotkey)
            + " — flips control to/from the remote computer",
        )
        group.add(self._hotkey_row)
        page.add(group)
        self._server_group = group

    def _build_client_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(
            title="Client settings",
            description="Leave the address blank to auto-discover the server "
            "over the cable.",
        )
        self._peer_row = Adw.EntryRow(title="Server address (optional)")
        self._peer_row.set_text(self.settings.peer_address)
        group.add(self._peer_row)

        self._cport_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self._cport_row.set_title("Server port")
        self._cport_row.set_value(self.settings.listen_port)
        group.add(self._cport_row)
        page.add(group)
        self._client_group = group

    def _build_automation_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Automation")

        self._auto_row = Adw.SwitchRow(
            title="Auto-connect when cable is detected",
            subtitle="Start automatically as soon as the USB-C link appears",
        )
        self._auto_row.set_active(self.settings.auto_connect)
        self._auto_row.connect("notify::active", self._on_auto_changed)
        group.add(self._auto_row)

        self._login_row = Adw.SwitchRow(
            title="Start Desk Connect at login",
            subtitle="Launch in the background when you log in",
        )
        self._login_row.set_active(self.settings.start_at_login)
        self._login_row.connect("notify::active", self._on_login_changed)
        group.add(self._login_row)
        page.add(group)

    def _build_status_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup()

        self._start_btn = Gtk.Button(label="Start")
        self._start_btn.add_css_class("suggested-action")
        self._start_btn.add_css_class("pill")
        self._start_btn.set_halign(Gtk.Align.CENTER)
        self._start_btn.connect("clicked", self._on_start_clicked)

        # The on-screen alternative to the switch hotkey (server only).
        self._control_btn = Gtk.Button(label="Take control of other computer")
        self._control_btn.add_css_class("pill")
        self._control_btn.set_halign(Gtk.Align.CENTER)
        self._control_btn.set_sensitive(False)
        self._control_btn.connect("clicked", self._on_control_clicked)

        self._active_pill = Gtk.Label(label="")
        self._active_pill.add_css_class("title-2")

        self._status = Gtk.Label(label="Idle", wrap=True, xalign=0.5)
        self._status.add_css_class("dim-label")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12)
        box.append(self._start_btn)
        box.append(self._control_btn)
        box.append(self._active_pill)
        box.append(self._status)
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_child(box)
        group.add(row)
        page.add(group)

    # -- behaviour ---------------------------------------------------------
    def _is_server(self) -> bool:
        return self._role_row.get_selected() == 0

    def _apply_role_visibility(self) -> None:
        server = self._is_server()
        self._server_group.set_visible(server)
        self._client_group.set_visible(not server)
        self._control_btn.set_visible(server)

    def _on_role_changed(self, *_args) -> None:
        self._apply_role_visibility()

    def _on_auto_changed(self, *_args) -> None:
        self.settings.auto_connect = self._auto_row.get_active()
        self._persist_quiet()

    def _on_login_changed(self, *_args) -> None:
        want = self._login_row.get_active()
        if autostart.set_autostart(want):
            self.settings.start_at_login = want
            self._persist_quiet()
            self._toast.add_toast(Adw.Toast(
                title="Will start at login" if want else "Login start disabled"))
        else:
            self._toast.add_toast(Adw.Toast(title="Could not change login setting"))
            self._login_row.set_active(self.settings.start_at_login)

    def _refresh_link_info(self) -> None:
        cables = link.cable_interfaces()
        if not cables:
            self._link_row.set_subtitle(
                "No cable interface found yet. Plug in the cable and click refresh."
            )
            return
        lines = []
        for iface in cables[:4]:
            tag = " (link-local)" if iface.is_link_local else ""
            lines.append(f"{iface.name}: {iface.address}{tag}")
        best = cables[0]
        self._link_row.set_subtitle(
            "\n".join(lines)
            + f"\n\nClients can auto-discover this machine, or use: {best.address}"
        )

    def _check_permissions(self) -> None:
        if not any(os.path.exists(p) for p in UINPUT_PATHS):
            self._toast.add_toast(
                Adw.Toast(title="/dev/uinput is missing — run: sudo modprobe uinput")
            )

    # -- engine callbacks (always marshalled onto the GTK thread) ----------
    def _set_status(self, text: str) -> None:
        GLib.idle_add(self._status.set_text, text)

    def _set_active(self, active: bool) -> None:
        def update():
            self._remote_active = active
            self._active_pill.set_text("🔴 Controlling REMOTE" if active else "🟢 LOCAL")
            self._control_btn.set_label(
                "Return control to this computer" if active
                else "Take control of other computer")
            return False
        GLib.idle_add(update)

    def _set_connected(self, connected: bool) -> None:
        def update():
            self._active_pill.set_text("🔗 CONNECTED" if connected else "… offline")
            return False
        GLib.idle_add(update)

    def _on_client_change(self, addr: str) -> None:
        def update():
            self._client_connected = bool(addr)
            self._control_btn.set_sensitive(bool(addr))
            return False
        GLib.idle_add(update)

    # -- start/stop --------------------------------------------------------
    def _persist(self) -> None:
        self.settings.role = "server" if self._is_server() else "client"
        self.settings.listen_port = int(
            self._port_row.get_value() if self._is_server()
            else self._cport_row.get_value()
        )
        self.settings.peer_address = self._peer_row.get_text().strip()
        self.settings.auto_connect = self._auto_row.get_active()
        self._persist_quiet()

    def _persist_quiet(self) -> None:
        try:
            self.settings.save()
        except OSError:
            pass

    def _running(self) -> bool:
        return self._server is not None or self._client is not None

    def _on_start_clicked(self, _btn) -> None:
        if self._running():
            self._stop_engines()
        else:
            self._start_engines()

    def _start_engines(self) -> None:
        if self._running():
            return
        self._persist()
        if self._is_server():
            self._server = ServerEngine(
                self.settings, self._set_status, self._set_active,
                self._on_client_change,
            )
            self._server.start()
        else:
            self._client = ClientEngine(
                self.settings, self._set_status, self._set_connected,
            )
            self._client.start()
        self._start_btn.set_label("Stop")
        self._start_btn.remove_css_class("suggested-action")
        self._start_btn.add_css_class("destructive-action")
        self._set_controls_sensitive(False)

    def _stop_engines(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
        if self._client is not None:
            self._client.stop()
            self._client = None
        self._start_btn.set_label("Start")
        self._start_btn.remove_css_class("destructive-action")
        self._start_btn.add_css_class("suggested-action")
        self._active_pill.set_text("")
        self._control_btn.set_sensitive(False)
        self._client_connected = False
        self._set_controls_sensitive(True)

    def _on_control_clicked(self, _btn) -> None:
        if self._server is not None:
            self._server.request_toggle()

    def _set_controls_sensitive(self, sensitive: bool) -> None:
        for widget in (self._role_row, self._port_row, self._peer_row,
                       self._cport_row):
            widget.set_sensitive(sensitive)

    # -- cable auto-detection ---------------------------------------------
    def _poll_cable(self) -> bool:
        present = any(i.looks_like_cable for i in link.cable_interfaces())
        if present and not self._cable_present:
            self._refresh_link_info()
            if self.settings.auto_connect and not self._running():
                self._set_status("USB-C link detected — connecting…")
                self._start_engines()
        self._cable_present = present
        return True  # keep polling

    def _maybe_autostart(self) -> bool:
        if not self._running():
            self._start_engines()
        return False

    def close_engines(self) -> None:
        self._stop_engines()


class DeskConnectApp(Adw.Application):
    def __init__(self, autostart_requested: bool = False):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.settings = Settings.load()
        self.autostart_requested = autostart_requested
        self._window: DeskConnectWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = DeskConnectWindow(self)
            self.connect("shutdown", lambda *_: self._window.close_engines())
        self._window.present()


def main(autostart_requested: bool = False) -> int:
    app = DeskConnectApp(autostart_requested=autostart_requested)
    return app.run(None)
