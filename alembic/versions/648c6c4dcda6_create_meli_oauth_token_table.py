"""create meli_oauth_token table

Tabla de una sola fila con la conexión OAuth de la app con Mercado Libre
(no es por-usuario). Ver app/models.py:MeliOAuthToken y app/meli_oauth.py.

Revision ID: 648c6c4dcda6
Revises: 9715301407ea
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '648c6c4dcda6'
down_revision: Union[str, Sequence[str], None] = '9715301407ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('meli_oauth_tokens',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('access_token', sa.String(), nullable=False),
    sa.Column('refresh_token', sa.String(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('meli_oauth_tokens')
