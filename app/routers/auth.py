from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from ..config import CADASTRO
from ..db import get_db
from ..models import User
from ..schemas import Token, UserCreate, UserOut, UserUpdate
from ..security import create_access_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/config")
def config():
    """Informações públicas para a tela de login."""
    return {"cadastro": CADASTRO}


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(data: UserCreate, db: Session = Depends(get_db)):
    if CADASTRO == "fechado":
        raise HTTPException(status_code=403, detail="Cadastro fechado. Peça a um administrador para criar sua conta.")
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=409, detail="Usuário já existe")
    user = User(username=data.username, email=data.email, full_name=data.full_name,
                hashed_password=hash_password(data.password), is_active=CADASTRO == "aberto")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Conta aguardando aprovação ou desativada por um administrador")
    return Token(access_token=create_access_token(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(data: UserUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if data.is_admin is not None or data.is_active is not None:
        raise HTTPException(status_code=403, detail="Somente administradores alteram is_admin/is_active")
    if data.email is not None:
        user.email = data.email
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.password:
        if not data.senha_atual or not verify_password(data.senha_atual, user.hashed_password):
            raise HTTPException(status_code=400, detail="Senha atual incorreta")
        if verify_password(data.password, user.hashed_password):
            raise HTTPException(status_code=400, detail="A nova senha precisa ser diferente da atual")
        user.hashed_password = hash_password(data.password)
        user.must_change_password = False
    db.commit()
    db.refresh(user)
    return user
