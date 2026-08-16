from typing import Optional
import httpx
from .base import BaseCourier
from .dpd import DPDCourier
from .sameday import SamedayCourier
from .packeta import PacketaCourier  # NEW
from .econt import EcontCourier
from .fancourier import FanCourier
from .gls import GLSCourier
from .testcourier import TestCourier
from .xconnector import XConnectorCourier



_http_client = httpx.AsyncClient(timeout=45.0)

_fancourier = FanCourier(_http_client)

_courier_instances = {
    "dpd": DPDCourier(_http_client),
    "sameday": SamedayCourier(_http_client),
    "packeta": PacketaCourier(_http_client),     # NEW
    "zasilkovna": PacketaCourier(_http_client),  # alias
    "packetery": PacketaCourier(_http_client),   # alias
    "econt": EcontCourier(_http_client),
    "fancourier": _fancourier,
    "fan": _fancourier,  # alias
    "gls": GLSCourier(_http_client),
    # Puntea xConnector — AWB + facturi prin API-ul xconnector.app (paritate cu cronul).
    "xconnector": XConnectorCourier(_http_client),
    # Test mode's sandbox courier (see testcourier.py) — never contacts anyone.
    "test": TestCourier(_http_client),
}

_courier_instances.update({
    "packeta": PacketaCourier(_http_client),
})

def get_courier_service(courier_key: str) -> Optional[BaseCourier]:
    if not courier_key:
        return None
    key = courier_key.lower()
    for k, instance in _courier_instances.items():
        if k in key:
            return instance
    return None

