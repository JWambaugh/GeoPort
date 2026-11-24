# GeoPort v5 — Headless Ubuntu & Proxmox LXC Guide

GeoPort v5 was engineered specifically so the **exact same codebase and release binary** runs seamlessly on headless Ubuntu servers and unprivileged Proxmox LXC containers over Wi-Fi.

## Why GeoPort v5 Simplifies Proxmox LXC Setup

| Requirement | Legacy GeoPort v4 | GeoPort v5 |
|---|---|---|
| Root / `sudo` | Required | **Not required** |
| `/dev/net/tun` LXC passthrough (`lxc.cgroup2.devices.allow: c 10:200 rwm`) | Required | **Not required** (`UserspaceTun` runs 100% in Python memory) |
| `usbmuxd` daemon installed | Required unless patched | **Not required** for Wi-Fi |
| Separate `main-ubuntu.py` fork | Required | **Unified codebase** (`main.py`) |

---

## 1. Network Prerequisites

1. Attach your LXC's network interface (`eth0`) to the **same L2 LAN/VLAN** as your iPhone's Wi-Fi so mDNS multicast (`224.0.0.251:5353` / `ff02::fb:5353`) and TCP port **`62078`** (`lockdownd`) are reachable.
2. Ensure inbound TCP port **`54321`** (web UI) and UDP **`5353`** (mDNS `geoport.local`) are allowed on the LXC firewall if enabled.

---

## 2. One-Time Device Pairing (`<UDID>.plist`)

Because a headless LXC typically has no USB passthrough, pair your iPhone once on any Mac/PC (with `EnableWifiConnections` enabled — GeoPort v5 enables this automatically whenever a device is plugged into USB!) and copy its Lockdown pair record to `~/.pymobiledevice3/<UDID>.plist` on the LXC:

```bash
mkdir -p ~/.pymobiledevice3
# Copy <UDID>.plist from your Mac/PC into ~/.pymobiledevice3/<UDID>.plist
chmod 600 ~/.pymobiledevice3/*.plist
```

> **Tip**: Pair once on a Mac/Windows laptop running GeoPort v5 over USB — GeoPort v5 automatically enables Wi-Fi connections on the iPhone and lets you click **Export Pairing** right from the web UI!

---

## 3. Install & Run on Ubuntu 24.04+ / Debian 12+

### Option A: Python 3.13 Virtualenv (Recommended)
```bash
cd /git/geoport-v5
python3.13 -m venv venv
./venv/bin/pip install --upgrade pip wheel
./venv/bin/pip install -r requirements.txt

# Test interactively on port 54321
./venv/bin/python3 main.py --no-browser --port 54321
```

### Option B: Offline Install from Prebuilt Wheelhouse
```bash
# On build machine:
./packaging/build_wheels.sh

# On target LXC:
python3.13 -m venv venv
./venv/bin/pip install --no-index --find-links ./wheelhouse -r requirements.txt
```

### Option C: Standalone Release Binary
```bash
unzip GeoPort-ubuntu-v5.0.0.zip
cd GeoPort-ubuntu-v5.0.0
./GeoPort --no-browser --port 54321
```

---

## 4. Enable as a Persistent `systemd` Service

```bash
sudo cp /git/geoport-v5/packaging/systemd/geoport.service /etc/systemd/system/geoport.service
sudo systemctl daemon-reload
sudo systemctl enable --now geoport.service
sudo systemctl status geoport.service
```

Then open **`http://geoport.local:54321`** or **`http://<LXC-IP>:54321`** directly from Safari on your iPhone or any browser on your LAN!
