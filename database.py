# database.py

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from settings import settings

DATABASE_URL = settings.DATABASE_URL

# Async engine with a tuned pool. pool_pre_ping avoids "server closed the connection"
# errors on idle connections to the remote Postgres; pool_recycle drops connections
# older than 30 min. Sizes are modest to keep the footprint light.
engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # Pool sized for webhook BURSTS — evening storms hit ~270 webhooks/min and each in-flight
    # request holds a session; 5+10 exhausted twice on 18-aug (whole app 500'd, incl. the UI).
    # 10+20 = max 30 conns; the box's Postgres has max_connections=200 (~143 used across ~22 apps),
    # so this stays a fair share. Connects direct to :5432 (SQLAlchemy pools; no PgBouncer).
    pool_size=20,
    max_overflow=30,
    # 20-aug: 10+20 s-a saturat DIN NOU și aplicația a devenit inaccesibilă (UI inclusiv). Tiparul e
    # de AMPLIFICARE, nu de trafic: pool plin → handler pică → întoarce 500 → Shopify REÎNCEARCĂ →
    # și mai multe webhook-uri. Pool-ul plin se auto-întreține. 20+30 = 50 conexiuni; cutia are
    # max_connections=200 cu ~158 folosite de ~22 de aplicații, deci rămâne o cotă onestă.
    #
    # `pool_timeout` scurt e la fel de important ca dimensiunea: cu 30s (implicit), fiecare cerere
    # blocată ținea clientul o jumătate de minut înainte să eșueze, iar Shopify oricum renunță mai
    # devreme. Eșuăm repede, ca să nu se adune coada.
    pool_timeout=10,
    pool_recycle=1800,
    echo=False,
)

# Creăm o sesiune asincronă
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()

# Funcția get_db devine din nou asincronă
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session