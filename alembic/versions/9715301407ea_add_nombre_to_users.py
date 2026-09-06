"""add nombre to users

Agrega la columna nombre (NOT NULL) a users. Aún no hay despliegue en
producción, así que hoy la tabla no debería tener filas — pero esta
migración queda segura de aplicar igual aunque las tuviera:

- Se agrega con un server_default TEMPORAL ('Sin nombre') para que el
  ALTER TABLE ADD COLUMN NOT NULL no falle contra filas existentes.
- Inmediatamente se retira ese default (alter_column server_default=None),
  para que la columna quede NOT NULL SIN default persistente — igual que
  la declara app/models.py. Cualquier usuario nuevo debe traer un nombre
  real explícito; el placeholder nunca debe usarse para altas nuevas.

Si en algún momento esto se corre contra una base con usuarios reales, las
filas existentes quedarán con nombre='Sin nombre' y deberían corregirse
a mano (un UPDATE puntual, no parte de esta migración).

Revision ID: 9715301407ea
Revises: 23e66bd940a3
Create Date: 2026-09-06 17:29:53.810619

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9715301407ea'
down_revision: Union[str, Sequence[str], None] = '23e66bd940a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('nombre', sa.String(), nullable=False, server_default='Sin nombre'),
    )
    op.alter_column('users', 'nombre', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'nombre')
