"""Create policies and policy_history.


 

Revision ID: 0001

Revises:

Create Date: 2026-10-26

"""


 

from alembic import op


 

revision = "0001"

down_revision = None

branch_labels = None

depends_on = None



 

def upgrade() -> None:

    op.execute(

        """

        CREATE TABLE policies (

            name         TEXT PRIMARY KEY,

            version      BIGINT      NOT NULL,

            algorithm    TEXT        NOT NULL,

            failure_mode TEXT        NOT NULL,

            -- Bands are always read and written together with their policy and

            -- never queried on their own, so one JSON column beats a child table.

            bands        JSONB       NOT NULL,

            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()

        )

        """

    )

    # Append-only: one row per create/update/delete, written in the same

    # transaction as the change, so history can't drift from reality. It also

    # remembers the highest version a name ever had, so a deleted and

    # re-created policy continues numbering instead of reusing old versions

    # (and with them, old Redis counters).

    op.execute(

        """

        CREATE TABLE policy_history (

            id           BIGSERIAL PRIMARY KEY,

            policy_name  TEXT        NOT NULL,

            version      BIGINT      NOT NULL,

            action       TEXT        NOT NULL CHECK (action IN ('CREATED', 'UPDATED', 'DELETED')),

            algorithm    TEXT        NOT NULL,

            failure_mode TEXT        NOT NULL,

            bands        JSONB       NOT NULL,

            changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()

        )

        """

    )

    op.execute("CREATE INDEX policy_history_by_name ON policy_history (policy_name, id DESC)")



 

def downgrade() -> None:

    op.execute("DROP TABLE policy_history")

    op.execute("DROP TABLE policies")


 