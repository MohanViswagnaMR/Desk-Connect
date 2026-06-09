# Desk Connect

Use **one keyboard and mouse to control two computers**, connected by a
**USB‑C cable** — no Wi‑Fi, no router, no internet. Press a hotkey to flip
control between the local machine and the remote one. Built as a **Flatpak**
for **Fedora** and **Nobara** (works on X11 and Wayland).

```
 ┌───────────────┐        USB-C / Thunderbolt        ┌───────────────┐
 │   Computer A  │◀═══════════ cable ═══════════════▶│   Computer B  │
 │   (SERVER)    │   private 169.254.x.x link        │   (CLIENT)    │
 │  real kbd/    │ ── input events over TCP ───────▶ │  virtual kbd/ │
 │  mouse        │                                   │  mouse(uinput)│
 └───────────────┘                                   └───────────────┘
```

---

## How it works (the research)

The hard part of this project is **not** the keyboard/mouse sharing — that is a
well‑trodden problem (Synergy, Barrier, Input Leap, lan‑mouse). The hard part is
the constraint: **the transport must be a USB‑C wire, not Wi‑Fi.** Here is what
is actually possible and why Desk Connect takes the approach it does.

### 1. You cannot just plug two PCs together with a normal USB‑C cable

Both computers are USB **hosts**. A plain USB cable expects a host on one end
and a **device** (peripheral) on the other. Two hosts wired together do
nothing — there is no host‑to‑host data path. So a raw "send bytes down the
USB wire" approach is not available to ordinary user‑space software (and
certainly not to a sandboxed Flatpak). There are three real ways to get a data
link over USB‑C:

| Option | Hardware needed | What the OS sees |
| --- | --- | --- |
| **Thunderbolt 3/4 networking** | Both ports must be Thunderbolt | A virtual Ethernet interface (`thunderbolt-net`) |
| **USB4 networking** | Both ports must be USB4 | Same — a virtual Ethernet interface |
| **USB "data‑link" bridge cable** | A special cable with a bridge chip (e.g. RNDIS/CDC) | A USB Ethernet interface |

In **every** case, the cable presents itself to Linux as an ordinary
**network interface**. That is the key insight: *the operating system already
turns the USB‑C wire into a point‑to‑point network link for us.* On Linux the
`thunderbolt-net` kernel module (mainline since 4.15) handles Thunderbolt/USB4,
and NetworkManager auto‑assigns **link‑local IPv4 addresses** (`169.254.0.0/16`)
to each end. Reported throughput exceeds 1 GB/s, and latency on a direct cable
is well under a millisecond — far better than Wi‑Fi for input.

> So Desk Connect runs plain **TCP over that interface**. The bytes never touch
> Wi‑Fi or a router; they go straight down the cable. The app detects the
> link‑local interface for you and pre‑fills the address.

**Recommended hardware:** a Thunderbolt 3/4 or USB4 cable between two machines
that both have Thunderbolt/USB4 ports. If your ports are plain USB‑C (no
Thunderbolt/USB4), you need a USB host‑to‑host **bridge cable**; a generic
charge/data cable will not work for a host‑to‑host link.

### 2. Reading the real keyboard and mouse (server side)

On Linux every input device is a character device under `/dev/input/event*`
speaking the **evdev** protocol (`struct input_event`: type/code/value). Desk
Connect opens the keyboard and mouse there, and when you switch to the remote
machine it calls **`EVIOCGRAB`** to take *exclusive* ownership — so your local
desktop stops reacting while you are driving the other computer. Switch back and
the grab is released.

### 3. Injecting input on the other machine (client side)

The controlled machine creates a **virtual keyboard + mouse** through
**`/dev/uinput`** and replays the events it receives. Because uinput injects at
the *kernel* level — below X11 and below the Wayland compositor — this works
identically on **GNOME/Wayland, KDE/Wayland and X11**. That matters: Wayland
deliberately blocks the old X11 input‑injection tricks, and while the modern
`libei` + RemoteDesktop portal path exists (GNOME 45+/Xwayland 23.2+), uinput is
simpler, permission‑stable, and compositor‑agnostic. Linux input event codes are
universal kernel ABI, so an event captured on one machine is replayed verbatim
on the other with **no translation table**.

### 4. Why pure Python with no compiled dependencies

The evdev read/grab and uinput create/inject paths are implemented **directly
against the kernel ioctl ABI** (see `src/deskconnect/linux_input.py`) instead of
pulling in `python-evdev`. That means the Flatpak builds against nothing but the
GNOME runtime's PyGObject — no C compilation, no pip, no network at build time.

### Sources

- [Thunderbolt Networking on Linux — Christian Kellner](https://christian.kellner.me/2018/05/24/thunderbolt-networking-on-linux/)
- [Thunderbolt Networking Setup on Linux (gist)](https://gist.github.com/geosp/80fbd39e617b7d1d9421683df4ea224a)
- [Input Leap — open‑source KVM](https://github.com/input-leap/input-leap)
- [lan-mouse — input sharing & libei/uinput notes](https://github.com/feschber/lan-mouse)
- [Input emulation on Wayland via libei and the RemoteDesktop portal](https://github.com/rustdesk/rustdesk/discussions/4515)
- [Flatpak sandbox permissions & `device=input`](https://docs.flatpak.org/en/latest/sandbox-permissions.html)

---

## Architecture

```
src/deskconnect/
├── linux_input.py   evdev read + EVIOCGRAB grab; uinput create + inject (pure ioctl)
├── protocol.py      length-prefixed TCP framing; tiny type/code/value events
├── link.py          finds the USB-C/Thunderbolt link-local interface (getifaddrs)
├── server.py        capture engine: hotkey switch, grab, forward  (select loop)
├── client.py        inject engine: connect, receive, replay via uinput
├── config.py        persisted settings (XDG config dir)
├── app.py           GTK4 / libadwaita UI
└── __main__.py      GUI entry + a --headless CLI (run over SSH / for testing)
```

The **server** runs where the physical keyboard/mouse live. The **client** runs
on the machine you want to control.

---

## Install & run on Fedora / Nobara

### A. Build the Flatpak

```bash
sudo dnf install flatpak flatpak-builder
flatpak install -y flathub org.gnome.Platform//48 org.gnome.Sdk//48

# from the repo root:
flatpak-builder --user --install --force-clean build-dir \
    flatpak/io.github.mohanviswagnamr.DeskConnect.yml

flatpak run io.github.mohanviswagnamr.DeskConnect
```

### B. Grant input permissions (one time, both machines)

`/dev/uinput` and `/dev/input/event*` are root‑owned by default. Run the helper
(it installs a udev rule, loads the `uinput` module, and adds you to the `input`
group), then **log out and back in**:

```bash
./build-aux/setup-permissions.sh
```

The Flatpak ships with `--device=all` so the sandbox can reach those nodes once
the host permissions above are in place.

### C. Connect the cable

1. Join the two machines with a **Thunderbolt 3/4 or USB4 cable**.
2. On each, confirm a link‑local interface appeared:
   `ip -4 addr | grep 169.254` (or just read it from the app — click ⟳).

### D. Use it

- **Computer A (keyboard/mouse owner):** open Desk Connect → role **Server**.
- **Computer B (to be controlled):** open Desk Connect → role **Client**. Leave
  the address blank — it **auto-discovers** the server over the cable.
- To hand control to B, either press **Ctrl + Alt + S** on A *or* click
  **“Take control of other computer”**. Do it again to come back. The pill shows
  🟢 LOCAL or 🔴 Controlling REMOTE.

> **If the cursor doesn’t move after switching:** the server warns when it
> cannot read your keyboard/mouse. That almost always means the `input` group
> isn’t active yet — run `setup-permissions.sh` and **log out and back in**.

### Auto-connect & start at login

- **Auto-connect when cable is detected** (on by default): Desk Connect watches
  for the USB-C/Thunderbolt interface and starts itself the moment the cable is
  plugged in — the server begins advertising and the client finds it
  automatically.
- **Start Desk Connect at login**: registers an autostart entry through the XDG
  Background portal so it’s ready in the background after every boot. Combined
  with auto-connect, plugging the cable “just works”.

### Headless (no GUI, e.g. over SSH)

```bash
python3 -m deskconnect --headless --role server
python3 -m deskconnect --headless --role client --peer 169.254.10.20
```

---

## Staying off Wi-Fi — guaranteed

Keeping traffic on the cable is the whole point, so Wi-Fi is made *impossible*,
not merely avoided:

1. **The server's TCP socket binds only to the cable's `169.254.x.x` link-local
   address.** It is literally not listening on the Wi-Fi interface, so nothing
   can connect to it over Wi-Fi.
2. **The client binds its outgoing connection to its own cable address**, so the
   session leaves via the wire.
3. **If there is no cable interface, neither side starts** — there is no network
   fallback. You'll see "No USB-C / Thunderbolt cable link found".
4. **Discovery only chirps on the cable** (bound to the cable interface, sent to
   the link-local broadcast — which never crosses Wi-Fi) and **stops the instant
   a client connects**, so there is no continuous broadcast.

> If you ever saw it use Wi-Fi, you were almost certainly running an older build
> from a different branch — check `cat src/deskconnect/__init__.py` shows the
> latest version and that you built from the `claude/keen-ritchie-uubcvg` branch.

## Mice and touchpads

Plain mice send *relative* motion and pass straight through. Laptop **touchpads**
report only *absolute* finger coordinates at the raw level (libinput normally
synthesises the gestures), so Desk Connect synthesises them itself:

- **move** — single-finger motion → relative pointer motion (scaled to the pad);
- **tap to click** — a quick light tap → left click; **two-finger tap** → right
  click; **three-finger tap** → middle click;
- **two-finger drag** → scroll wheel;
- a physical **click-pad press** passes through as a normal left button.

## Limitations

- **Absolute tablets** (pen digitizers) are not yet mapped across screens.
- **Switching is hotkey‑based** (or the on-screen button), not screen‑edge based.
  Edge switching needs the global cursor position, which Wayland does not expose
  to clients; a hotkey is reliable everywhere.
- **No clipboard sharing yet** (Wayland clipboard bridging is a separate, larger
  feature).
- One client per server.

## Testing

`tests/` contains framing/ioctl unit checks plus loopback integration tests for
the capture→forward and receive→inject paths (they stub the kernel device layer,
so they run anywhere):

```bash
python3 tests/test_smoke.py
```

## License

GPL‑3.0‑or‑later. See [LICENSE](LICENSE).
