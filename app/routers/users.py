from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import UserCreate, UserOut, UserUpdate
from ..security import hash_password, require_admin

router = APIRouter(prefix="/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])


def _get(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return user


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).order_by(User.id).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(data: UserCreate, is_admin: bool = False, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=409, detail="Usuário já existe")
    # Senha definida pelo admin é provisória: o usuário troca no primeiro acesso
    user = User(username=data.username, email=data.email, full_name=data.full_name,
                hashed_password=hash_password(data.password), is_admin=is_admin, must_change_password=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    return _get(db, user_id)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, data: UserUpdate, db: Session = Depends(get_db)):
    user = _get(db, user_id)
    for field in ("email", "full_name", "is_admin", "is_active"):
        if (value := getattr(data, field)) is not None:
            setattr(user, field, value)
    if data.password:
        user.hashed_password = hash_password(data.password)
        user.must_change_password = True
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = _get(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Você não pode excluir a própria conta")
    db.delete(user)
    db.commit()
