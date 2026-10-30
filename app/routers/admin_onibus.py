import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import onibus
from ..db import get_db
from ..models import DadoApp, User
from ..security import require_admin

router = APIRouter(prefix="/admin/onibus", tags=["admin"])

MAX_ZIP = 120 * 1024 * 1024  # o GTFS convencional tem ~20 MB


async def _ler(arquivo: UploadFile | None) -> bytes | None:
    if arquivo is None or not arquivo.filename:
        return None
    conteudo = await arquivo.read(MAX_ZIP + 1)
    if len(conteudo) > MAX_ZIP:
        raise HTTPException(status_code=413, detail=f"{arquivo.filename} passa de {MAX_ZIP // 1048576} MB")
    return conteudo


@router.get("")
def ver(admin: User = Depends(require_admin)):
    """Base de ônibus em uso: linhas, pontos, validade dos feeds e de onde veio."""
    return onibus.info()


@router.post("")
async def atualizar(convencional: UploadFile = File(..., description="gtfsfiles.zip do sistema convencional"),
                    suplementar: UploadFile | None = File(None, description="gtfsfiles.zip do sistema suplementar"),
                    admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Monta a base a partir dos GTFS enviados, guarda no banco e passa a usar na hora."""
    feeds = {"convencional": await _ler(convencional)}
    if sup := await _ler(suplementar):
        feeds["suplementar"] = sup
    try:
        # O processamento lê centenas de MB de stop_times: fora do loop de eventos
        indice = await asyncio.to_thread(onibus.montar_indice, feeds)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    row = db.get(DadoApp, onibus.CHAVE_DB) or DadoApp(chave=onibus.CHAVE_DB)
    row.valor = json.dumps(indice, ensure_ascii=False, separators=(",", ":"))
    row.atualizado_em = datetime.utcnow()
    row.atualizado_por = admin.username
    db.merge(row)
    db.commit()
    onibus.recarregar()
    return onibus.info()


@router.delete("")
def voltar_original(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Descarta a base enviada e volta ao arquivo original do app."""
    row = db.get(DadoApp, onibus.CHAVE_DB)
    if row:
        db.delete(row)
        db.commit()
    onibus.recarregar()
    return onibus.info()
