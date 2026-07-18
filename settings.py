# settings.py
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from typing import List, Dict, Any, Optional
import json
from pathlib import Path

# Nu mai avem nevoie de modelul ShopifyStore aici, deoarece datele vin din DB

def json_config_settings_source(settings: BaseSettings) -> Dict[str, Any]:
    """Funcție goală, necesară pentru structura Pydantic."""
    return {}

class Settings(BaseSettings):
    DATABASE_URL: str
    
    # Hărțile statice sunt OK să fie aici
    COURIER_MAP: Dict[str, str] = {}
    PAYMENT_MAP: Optional[Dict[str, List[str]]] = None
    COURIER_STATUS_MAP: Optional[Dict[str, Any]] = None
    # =================================================================
    # AICI AM ADĂUGAT HARTA DE TRACKING
    # =================================================================
    COURIER_TRACKING_MAP: Dict[str, str] = {
        "DPD": "https://tracking.dpd.ro?shipmentNumber={awb}",
        "Sameday": "https://sameday.ro/track-awb/{awb}",
        "Econt": "https://www.econt.com/services/track-shipment/{awb}"
    }
    # =================================================================

    
    # Restul setărilor rămân neschimbate
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    SYNC_INTERVAL_ORDERS_MINUTES: int = 15
    SYNC_INTERVAL_COURIERS_MINUTES: int = 5
    CORS_ORIGINS: List[str] = ["*"]

    # ── Shopify OAuth (app install). Values come from env/.env; secrets live in the
    #    secret store, never in git. Set these before enabling the /auth install flow.
    SHOPIFY_API_KEY: Optional[str] = None       # Partner Dashboard client_id
    SHOPIFY_API_SECRET: Optional[str] = None    # Partner Dashboard client secret (= webhook HMAC secret)
    SHOPIFY_APP_URL: Optional[str] = None        # https://<app host>, must match shopify.app.toml application_url
    SHOPIFY_SCOPES: str = (
        "read_orders,write_orders,read_fulfillments,write_fulfillments,"
        "read_assigned_fulfillment_orders,write_assigned_fulfillment_orders,"
        "write_merchant_managed_fulfillment_orders,read_products"
    )
    SHOPIFY_API_VERSION: str = "2026-04"
    SESSION_SECRET: Optional[str] = None         # signs OAuth state nonce + session cookie

    print_batch_size: int = 250
    archive_retention_days: int = 7
    
    DPD_CREDS: Optional[Dict[str, Dict[str, str]]] = None
    SAMEDAY_CREDS: Optional[Dict[str, str]] = None
    ECONT_CREDS: Optional[Dict[str, str]] = None

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'
        # .env also holds operational vars read via os.environ (AWB_B2_ENC_KEY,
        # AWB_B2_CORS_ORIGINS, SHOPIFY_BILLING_TEST) — don't reject them here.
        extra = "ignore"

settings = Settings()

def load_json_config(file_path: str) -> Any:
    path = Path(file_path)
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

# Încărcăm DOAR fișierele de configurare statice
settings.PAYMENT_MAP = load_json_config('config/payment_map.json')
settings.COURIER_STATUS_MAP = load_json_config('config/courier_status_map.json')
settings.COURIER_MAP = load_json_config('config/courier_map.json')

settings.DPD_CREDS = load_json_config('config/dpd.json')
settings.SAMEDAY_CREDS = load_json_config('config/sameday.json')
settings.ECONT_CREDS = load_json_config('config/econt.json')