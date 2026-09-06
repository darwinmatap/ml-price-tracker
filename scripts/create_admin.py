"""
Script de bootstrap: crea un usuario admin.

Uso (manual, NUNCA vía la API ni automatizado en un deploy):

    python scripts/create_admin.py

Pide username y password de forma interactiva. La password se lee con
getpass (no queda visible en pantalla ni en el historial de la shell) y
se pide dos veces para detectar errores de tipeo.

A diferencia de POST /admin/users (que requiere ya tener un admin
autenticado), este script NO exige que no exista un admin previo:
requiere acceso directo al servidor, que ya es un límite de confianza
distinto al de la API. Esto es intencional — es el camino de recuperación
si el único admin activo queda inutilizable (cuenta desactivada, password
perdida, etc.): alguien con acceso al servidor corre este script y crea
un admin nuevo, sin depender de la API para recuperar acceso a la API.

Nota: reutiliza el mismo CryptContext (bcrypt) de app.auth para que el
hash quede en el formato exacto que el resto de la app ya sabe verificar.
"""

import getpass
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.auth import pwd_context  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import User, UserRole  # noqa: E402


def _prompt_username() -> str:
    username = input("Username del admin: ").strip()
    if not username:
        print("El username no puede estar vacío.", file=sys.stderr)
        sys.exit(1)
    return username


def _prompt_password() -> str:
    password = getpass.getpass("Password del admin: ")
    if not password:
        print("La password no puede estar vacía.", file=sys.stderr)
        sys.exit(1)

    confirmacion = getpass.getpass("Confirma la password: ")
    if password != confirmacion:
        print("Las contraseñas no coinciden.", file=sys.stderr)
        sys.exit(1)

    return password


def main() -> None:
    db = SessionLocal()
    try:
        username = _prompt_username()

        if db.execute(select(User).where(User.username == username)).scalar_one_or_none() is not None:
            print(f"Ya existe un usuario con username '{username}'.", file=sys.stderr)
            sys.exit(1)

        password = _prompt_password()

        admin = User(
            username=username,
            password_hash=pwd_context.hash(password),
            role=UserRole.ADMIN,
            is_active=True,
            debe_cambiar_password=False,
        )
        db.add(admin)
        db.commit()

        print(f"Usuario admin '{username}' creado correctamente.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
