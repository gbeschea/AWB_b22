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
    # Modest pool — Order Hub shares the fleet box's Postgres with ~22 other apps and
    # connects direct to :5432 (SQLAlchemy pools; avoids asyncpg-vs-PgBouncer issues).
    pool_size=5,
    max_overflow=10,
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