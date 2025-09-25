# /scripts/check_profiles.py

import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Importăm modelele și setările din aplicația principală
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models
from settings import settings

async def check_database():
    """
    Se conectează la baza de date și afișează toate profilele de expediție.
    """
    # Ne conectăm la baza de date folosind URL-ul din .env
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    AsyncSessionFactory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    print("--- Verificare Profile de Expediție în Baza de Date ---")
    
    async with AsyncSessionFactory() as session:
        # Executăm query-ul pentru a selecta toate înregistrările din tabelul ShipmentProfile
        result = await session.execute(select(models.ShipmentProfile))
        all_profiles = result.scalars().all()

        if not all_profiles:
            print(">>> Baza de date nu conține niciun profil de expediție.")
        else:
            print(f">>> Au fost găsite {len(all_profiles)} profile:")
            for profile in all_profiles:
                print(f"  - ID: {profile.id}, Nume: '{profile.name}', Cont: {profile.account_key}")
    
    await engine.dispose()
    print("-----------------------------------------------------")

if __name__ == "__main__":
    asyncio.run(check_database())