"""Test env bootstrap. Must set required env vars BEFORE any app module (crypto loads
its key at import time; settings/database require DATABASE_URL) is imported."""
import base64
import os

os.environ.setdefault("AWB_B2_ENC_KEY", base64.b64encode(b"x" * 32).decode())
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/testdb")
os.environ.setdefault("SHOPIFY_API_SECRET", "test-shopify-secret")
os.environ.setdefault("SHOPIFY_API_KEY", "test-api-key")
os.environ.setdefault("SHOPIFY_APP_URL", "https://awb.example.com")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
