"""Domain models for GeoPort v5."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
@dataclass(slots=True)
class Coordinate:
    """Geographic coordinate in WGS84 decimal degrees."""
    latitude: float
    longitude: float
    def validate(self) -> None:
        if not (-90.0) <= self.latitude <= 90.0:
            raise ValueError(f'Latitude out of range [-90, 90]: {self.latitude}')
        else:
            if not (-180.0) <= self.longitude <= 180.0:
                raise ValueError(f'Longitude out of range [-180, 180]: {self.longitude}')
    def to_dict(self) -> dict[str, float]:
        return {'lat': self.latitude, 'lng': self.longitude}
@dataclass(slots=True)
class WifiEndpoint:
    """Discovered Wi-Fi endpoint for Lockdown TCP or RemotePairing."""
    wifiAddress: str
    wifiPort: int = 62078
    discovery: str = 'mobdev2'
    def to_dict(self) -> dict[str, Any]:
        return {'wifiAddress': self.wifiAddress, 'wifiPort': self.wifiPort, 'discovery': self.discovery}
@dataclass
class SessionSnapshot:
    """Current state of the active device tunnel and DVT location simulation."""
    connected: bool = False
    udid: Optional[str] = None
    connection_type: Optional[str] = None
    transport: Optional[str] = None
    ios_version: Optional[str] = None
    device_name: Optional[str] = None
    rsd_address: Optional[str] = None
    rsd_port: Optional[int] = None
    wifi_address: Optional[str] = None
    userspace_tunnel: bool = False
    simulating: bool = False
    current_coordinate: Optional[Coordinate] = None
    last_error: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    @property
    def rsd_data(self) -> Optional[list[str]]:
        if self.rsd_address and self.rsd_port is not None:
            return [self.rsd_address, str(self.rsd_port)]
        else:
            if self.connected and self.ios_version and self.udid:
                return [self.ios_version, self.udid]
            else:
                return None
    def to_dict(self) -> dict[str, Any]:
        return {'connected': self.connected, 'udid': self.udid, 'connection_type': self.connection_type, 'transport': self.transport, 'ios_version': self.ios_version, 'device_name': self.device_name, 'rsd_address': self.rsd_address, 'rsd_port': self.rsd_port, 'rsd_data': self.rsd_data, 'wifi_address': self.wifi_address, 'userspace_tunnel': self.userspace_tunnel, 'simulating': self.simulating, 'current_coordinate': self.current_coordinate.to_dict() if self.current_coordinate else None, 'last_error': self.last_error, **self.extra}