# routes/labels.py

from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import Response, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from PyPDF2 import PdfMerger
import io
import logging
from datetime import datetime

from database import get_db
import models
from services.couriers import get_courier_service, common as courier_common

router = APIRouter(
    prefix="/labels",
    tags=["Labels"]
)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@router.get("/download/{awb}", response_class=Response, name="download_single_label")
async def download_label(awb: str, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(models.Shipment)
        .options(selectinload(models.Shipment.order).selectinload(models.Order.store))
        .where(models.Shipment.awb == awb)
    )
    shipment = (await db.execute(stmt)).scalar_one_or_none()
    if not shipment:
        raise HTTPException(status_code=404, detail="AWB-ul nu a fost găsit.")
    try:
        courier_service = get_courier_service(shipment.courier)
        account = await courier_common.get_courier_account_by_key(db, shipment.account_key)
        if not account or not account.credentials:
             raise HTTPException(status_code=404, detail=f"Credentialele pentru contul {shipment.account_key} nu au fost gasite.")

        pdf_content = await courier_service.get_label(
            awb=shipment.awb,
            creds=account.credentials,
            paper_size=shipment.paper_size
        )
        if not pdf_content:
            raise HTTPException(status_code=404, detail="Eticheta nu a putut fi generată de la curier.")

        # === MODIFICARE: Marcăm AWB-ul ca fiind printat ===
        if shipment.printed_at is None:
            shipment.printed_at = datetime.utcnow()
            await db.commit()
        # ===================================================

        return Response(content=pdf_content, media_type="application/pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Nu s-a putut descărca eticheta: {e}")

@router.post("/merge_for_print")
async def merge_labels_for_print(awbs: str = Form(...), db: AsyncSession = Depends(get_db)):
    awb_list = [awb.strip() for awb in awbs.split(',') if awb.strip()]
    if not awb_list:
        return JSONResponse(status_code=400, content={"detail": "Niciun AWB valid furnizat."})

    stmt = select(models.Shipment).where(models.Shipment.awb.in_(awb_list))
    shipments = (await db.execute(stmt)).scalars().all()
    
    shipment_map = {s.awb: s for s in shipments}
    merger = PdfMerger()
    successful_awbs = []
    failed_awbs = list(set(awb_list) - set(shipment_map.keys()))

    for awb, shipment in shipment_map.items():
        try:
            courier_service = get_courier_service(shipment.courier)
            account = await courier_common.get_courier_account_by_key(db, shipment.account_key)
            if not account or not account.credentials:
                failed_awbs.append(awb)
                continue
            
            pdf_content = await courier_service.get_label(
                awb=shipment.awb,
                creds=account.credentials,
                paper_size=shipment.paper_size
            )
            if pdf_content:
                merger.append(io.BytesIO(pdf_content))
                successful_awbs.append(awb) # Adăugăm AWB-ul la lista de succes
            else:
                failed_awbs.append(awb)
        except Exception as e:
            logger.error(f"Eroare la procesarea AWB {awb} pentru printare: {e}")
            failed_awbs.append(awb)
    
    if not merger.pages:
        return JSONResponse(status_code=404, content={"detail": f"Etichetele nu au putut fi descărcate. AWB-uri eșuate: {', '.join(failed_awbs)}"})

    # === MODIFICARE: Actualizăm statusul pentru AWB-urile printate cu succes ===
    if successful_awbs:
        update_stmt = (
            update(models.Shipment)
            .where(models.Shipment.awb.in_(successful_awbs), models.Shipment.printed_at.is_(None))
            .values(printed_at=datetime.utcnow())
        )
        await db.execute(update_stmt)
        await db.commit()
    # =======================================================================

    output_pdf = io.BytesIO()
    merger.write(output_pdf)
    merger.close()
    
    return Response(content=output_pdf.getvalue(), media_type="application/pdf")