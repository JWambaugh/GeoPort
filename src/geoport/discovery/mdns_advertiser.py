# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""mDNS (`geoport.local`) HTTP service advertiser with smart LAN interface selection."""
from __future__ import annotations

import ipaddress
import socket
from contextlib import suppress
from typing import Optional

from geoport.config import APP_VERSION_NUMBER, logger

try:
    import ifaddr
except Exception:
    ifaddr = None

try:
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
except Exception:
    Zeroconf = None
    ServiceInfo = None
    IPVersion = None

_VIRTUAL_IFACE_PREFIXES = ('lo', 'docker', 'br-', 'veth', 'virbr', 'cni', 'flannel', 'tailscale', 'tun', 'tap', 'wg')


def detect_lan_ipv4() -> str:
    """Detect the primary LAN IPv4 address, ignoring loopback, link-local, and container bridge interfaces."""
    with suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                return ip
    if ifaddr is not None:
        with suppress(Exception):
            for adapter in ifaddr.get_adapters():
                if any(adapter.name.lower().startswith(p) for p in _VIRTUAL_IFACE_PREFIXES):
                    continue
                for ip_obj in adapter.ips:
                    if not isinstance(ip_obj.ip, str):
                        continue
                    addr = ipaddress.IPv4Address(ip_obj.ip)
                    if addr.is_loopback or addr.is_link_local:
                        continue
                    return str(addr)
    return '127.0.0.1'


class MDNSAdvertiser:
    """Publishes `geoport._http._tcp.local.` (`http://geoport.local:<port>`) on the LAN."""

    def __init__(self, port: int, hostname: str = 'geoport') -> None:
        self.port = int(port)
        self.hostname = hostname
        self.lan_ip = detect_lan_ipv4()
        self._zc = None
        self._info = None

    def start(self) -> Optional[str]:
        if Zeroconf is None or ServiceInfo is None or self.lan_ip.startswith('127.'):
            return None
        try:
            self._zc = Zeroconf(ip_version=IPVersion.V4Only)
            self._info = ServiceInfo(
                type_='_http._tcp.local.',
                name=f'{self.hostname}._http._tcp.local.',
                addresses=[socket.inet_aton(self.lan_ip)],
                port=self.port,
                properties={'version': APP_VERSION_NUMBER, 'app': 'GeoPort'},
                server=f'{self.hostname}.local.',
            )
            self._zc.register_service(self._info, allow_name_change=True)
            logger.info(
                f'Advertised mDNS service http://{self.hostname}.local:{self.port} -> {self.lan_ip}:{self.port}'
            )
            return self.lan_ip
        except Exception as exc:
            logger.debug(f'mDNS advertisement skipped (non-fatal): {exc}')
            self.stop()
            return None

    def stop(self) -> None:
        if self._zc is not None:
            with suppress(Exception):
                if self._info is not None:
                    self._zc.unregister_service(self._info)
            with suppress(Exception):
                self._zc.close()
        self._zc = None
        self._info = None
