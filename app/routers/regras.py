import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import llm
from ..db import get_db
from ..models import Feedback, Regra, SearchHistory, User
from ..schemas import FeedbackIn, RegraIn, RegraOut, RegraUpdate
from ..security import get_current_user

router = APIRouter(tags=["regras"])


def regras_para(db: Session, user: User | None) -> list[str]:
    """Textos das regras ativas que valem para o usuário: as compartilhadas e as dele."""
    q = db.query(Regra).filter(Regra.ativa.is_(True))
    if user is None:
        q = q.filter(Regra.compartilhada.is_(True))
    else:
        q = q.filter(or_(Regra.compartilhada.is_(True), Regra.user_id == user.id))
    return [r.texto for r in q.order_by(Regra.id)]


def _out(r: Regra, autor: str, user: User) -> RegraOut:
    return RegraOut(id=r.id, texto=r.texto, compartilhada=r.compartilhada, ativa=r.ativa, origem=r.origem,
                    autor=autor, minha=r.user_id == user.id, created_at=r.created_at, updated_at=r.updated_at)


def _editavel(db: Session, regra_id: int, user: User) -> Regra:
    """A regra existe e o usuário pode alterá-la (autor ou admin)."""
    r = db.get(Regra, regra_id)
    if not r or (r.user_id != user.id and not user.is_admin and not r.compartilhada):
        raise HTTPException(status_code=404, detail="Regra não encontrada")
    if r.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Só o autor ou um admin pode alterar esta regra")
    return r


@router.get("/regras", response_model=list[RegraOut])
def listar(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Regras que o usuário vê: as compartilhadas (de todos) e as próprias privadas."""
    rows = (db.query(Regra, User.username).join(User, User.id == Regra.user_id)
            .filter(or_(Regra.compartilhada.is_(True), Regra.user_id == user.id))
            .order_by(Regra.id.desc()).all())
    return [_out(r, autor, user) for r, autor in rows]


@router.post("/regras", response_model=RegraOut, status_code=201)
def criar(data: RegraIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = Regra(user_id=user.id, texto=data.texto.strip(), compartilhada=data.compartilhada, origem="manual")
    db.add(r)
    db.commit()
    db.refresh(r)
    return _out(r, user.username, user)


@router.patch("/regras/{regra_id}", response_model=RegraOut)
def editar(regra_id: int, data: RegraUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = _editavel(db, regra_id, user)
    if data.texto is not None:
        r.texto = data.texto.strip()
    if data.compartilhada is not None:
        r.compartilhada = data.compartilhada
    if data.ativa is not None:
        r.ativa = data.ativa
    db.commit()
    db.refresh(r)
    return _out(r, db.get(User, r.user_id).username, user)


@router.delete("/regras/{regra_id}", status_code=204)
def excluir(regra_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.delete(_editavel(db, regra_id, user))
    db.commit()


@router.post("/historico/{historico_id}/feedback")
async def avaliar(historico_id: int, data: FeedbackIn, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Avaliação (1–5 + justificativa) do relatório da IA ou de um imóvel dele.
    Opcionalmente vira uma regra que a IA passa a seguir nas próximas buscas."""
    row = db.get(SearchHistory, historico_id)
    if not row or (row.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Busca não encontrada")
    relatorio = json.loads(row.relatorio_json) if row.relatorio_json else {}
    avaliacao_ia = next((r for r in relatorio.get("ranking", []) if r["id"] == data.imovel_id), None)
    imovel = None
    if data.imovel_id and row.results_json:
        imovel = next((im for im in json.loads(row.results_json)["imoveis"]
                       if f"{im['fonte']}:{im['id']}" == data.imovel_id), None)

    regra = None
    aviso = None
    if data.gerar_regra:
        texto = data.justificativa.strip()
        if llm.ativa():
            try:
                texto = await llm.regra_de_feedback(row.query, data.nota, data.justificativa, imovel, avaliacao_ia)
            except (llm.ErroIA, ValueError) as e:
                aviso = f"IA indisponível ({e}); a regra ficou com o texto da justificativa"
        regra = Regra(user_id=user.id, texto=texto, compartilhada=data.compartilhar, origem="feedback")
        db.add(regra)
        db.flush()
    fb = Feedback(user_id=user.id, historico_id=row.id, imovel_id=data.imovel_id, nota=data.nota,
                  justificativa=data.justificativa.strip(), regra_id=regra.id if regra else None)
    db.add(fb)
    db.commit()
    return {"feedback_id": fb.id, "regra": _out(regra, user.username, user) if regra else None, "aviso": aviso}


@router.get("/historico/{historico_id}/feedback")
def feedbacks(historico_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Avaliações já feitas nesta busca (para a tela mostrar o que já foi avaliado)."""
    row = db.get(SearchHistory, historico_id)
    if not row or (row.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Busca não encontrada")
    fbs = db.query(Feedback).filter(Feedback.historico_id == historico_id).order_by(Feedback.id).all()
    return [{"id": f.id, "imovel_id": f.imovel_id, "nota": f.nota, "justificativa": f.justificativa,
             "regra_id": f.regra_id, "data": f.created_at} for f in fbs]
