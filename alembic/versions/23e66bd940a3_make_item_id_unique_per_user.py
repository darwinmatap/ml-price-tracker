"""make item_id unique per user

Cambia el constraint de unicidad de item_id: de UNIQUE global a
UNIQUE(user_id, item_id). Dos usuarios distintos ahora pueden monitorear
el mismo producto de Mercado Libre de forma independiente; un mismo
usuario sigue sin poder duplicar su propio item_id.

Revision ID: 23e66bd940a3
Revises: da756af19115
Create Date: 2026-09-06 17:16:25.797993

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '23e66bd940a3'
down_revision: Union[str, Sequence[str], None] = 'da756af19115'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index('ix_products_item_id', table_name='products')
    op.create_unique_constraint('uq_products_user_id_item_id', 'products', ['user_id', 'item_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_products_user_id_item_id', 'products', type_='unique')
    op.create_index('ix_products_item_id', 'products', ['item_id'], unique=True)
