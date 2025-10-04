from typing import Optional
import httpx
from .base import BaseCourier
from .dpd import DPDCourier
from .sameday import SamedayCourier
from .packeta import PacketaCourier  # NEW



_http_client = httpx.AsyncClient(timeout=45.0)

_courier_instances = {
    "dpd": DPDCourier(_http_client),
    "sameday": SamedayCourier(_http_client),
    "packeta": PacketaCourier(_http_client),     # NEW
    "zasilkovna": PacketaCourier(_http_client),  # alias
    "packetery": PacketaCourier(_http_client),   # alias
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

