"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Multi-tenant migration checklist (verify every item):
  [ ] new table inherits TenantScopedMixin, tenant_id NOT NULL
  [ ] join tables also carry tenant_id (they are leak backdoors)
  [ ] unique constraints include tenant_id
  [ ] indexes lead with tenant_id
  [ ] data backfill goes through bypass_context and writes audit
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
# Text is required when autogenerate renders a with_variant(..., 'postgresql')
# column (e.g. JSON().with_variant(JSONB(astext_type=Text()), 'postgresql')).
# Alembic emits Text() but does not add the import itself, which leaves the
# generated migration with a NameError on upgrade. Harmless if unused.
from sqlalchemy import Text
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
