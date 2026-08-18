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
    pool_size=10,
    max_overflow=20,
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