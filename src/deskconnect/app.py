"""Desk Connect GTK4 / libadwaita application."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import link  # noqa: E402
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
        self.set_default_size(520, 600)

        self._server: ServerEngine | None = None
        self._client: ClientEngine | None = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

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
        self._build_status_group(page)

        self._apply_role_visibility()
        self._refresh_link_info()
        self._check_permissions()

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
        self._link_group = group

    def _build_server_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Server settings")
        self._port_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self._port_row.set_title("Listen port")
        self._port_row.set_value(self.settings.listen_port)
        group.add(self._port_row)

        self._hotkey_row = Adw.ActionRow(
            title="Switch hotkey",
            subtitle=_hotkey_label(self.settings.switch_hotkey),
        )
        group.add(self._hotkey_row)
        page.add(group)
        self._server_group = group

    def _build_client_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Client settings")
        self._peer_row = Adw.EntryRow(title="Server address (e.g. 169.254.x.x)")
        self._peer_row.set_text(self.settings.peer_address)
        group.add(self._peer_row)

        self._cport_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self._cport_row.set_title("Server port")
        self._cport_row.set_value(self.settings.listen_port)
        group.add(self._cport_row)
        page.add(group)
        self._client_group = group

    def _build_status_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup()
        self._start_btn = Gtk.Button(label="Start")
        self._start_btn.add_css_class("suggested-action")
        self._start_btn.add_css_class("pill")
        self._start_btn.set_halign(Gtk.Align.CENTER)
        self._start_btn.connect("clicked", self._on_start_clicked)

        self._status = Gtk.Label(label="Idle", wrap=True, xalign=0.5)
        self._status.add_css_class("dim-label")

        self._active_pill = Gtk.Label(label="")
        self._active_pill.add_css_class("title-2")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12)
        box.append(self._start_btn)
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

    def _on_role_changed(self, *_args) -> None:
        self._apply_role_visibility()

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
            + f"\n\nOn the client, enter this machine's address: {best.address}"
        )

    def _check_permissions(self) -> None:
        if not any(os.path.exists(p) for p in UINPUT_PATHS):
            self._toast.add_toast(
                Adw.Toast(title="/dev/uinput is missing — run: sudo modprobe uinput")
            )

    def _set_status(self, text: str) -> None:
        GLib.idle_add(self._status.set_text, text)

    def _set_active(self, active: bool) -> None:
        def update():
            self._active_pill.set_text("🔴 REMOTE" if active else "🟢 LOCAL")
        GLib.idle_add(update)

    def _set_connected(self, connected: bool) -> None:
        def update():
            self._active_pill.set_text("🔗 CONNECTED" if connected else "… offline")
        GLib.idle_add(update)

    def _persist(self) -> None:
        self.settings.role = "server" if self._is_server() else "client"
        self.settings.listen_port = int(
            self._port_row.get_value() if self._is_server()
            else self._cport_row.get_value()
        )
        self.settings.peer_address = self._peer_row.get_text().strip()
        try:
            self.settings.save()
        except OSError:
            pass

    def _running(self) -> bool:
        return self._server is not None or self._client is not None

    def _on_start_clicked(self, _btn) -> None:
        if self._running():
            self._stop_engines()
            return
        self._persist()
        if self._is_server():
            self._server = ServerEngine(
                self.settings, self._set_status, self._set_active,
                lambda addr: None,
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
        self._set_controls_sensitive(True)

    def _set_controls_sensitive(self, sensitive: bool) -> None:
        for widget in (self._role_row, self._port_row, self._peer_row,
                       self._cport_row):
            widget.set_sensitive(sensitive)

    def close_engines(self) -> None:
        self._stop_engines()


class DeskConnectApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.settings = Settings.load()
        self._window: DeskConnectWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = DeskConnectWindow(self)
            self.connect("shutdown", lambda *_: self._window.close_engines())
        self._window.present()


def main() -> int:
    app = DeskConnectApp()
    return app.run(None)
