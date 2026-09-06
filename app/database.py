"""
Configuración del motor de conexión a la base de datos.

DATABASE_URL se lee EXCLUSIVAMENTE desde la variable de entorno del mismo
nombre (ver .env.example). Nunca hardcodear credenciales o cadenas de
conexión en este archivo ni en ningún otro.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL no está definida. Configúrala como variable de entorno "
        "(ver .env.example) antes de iniciar la aplicación."
    )

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency de FastAPI: entrega una sesión de DB y la cierra al terminar."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
