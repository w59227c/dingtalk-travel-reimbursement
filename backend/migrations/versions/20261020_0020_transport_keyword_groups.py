"""Expose transport detection keywords in the editable category settings.

Revision ID: 20261020_0020
Revises: 20260915_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0020"
down_revision: str | None = "20260915_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RIDE_HAILING = (
    "网约车",
    "网络预约出租",
    "滴滴出行",
    "滴滴科技",
    "滴滴畅行",
    "滴滴快车",
    "滴滴专车",
    "曹操出行",
    "吉利优行电子科技",
    "t3出行",
    "首汽约车",
    "花小猪",
    "高德打车",
    "南京领行科技",
    "长燃智享",
    "享道出行",
    "郑州哈拜网络科技",
    "杭州携华网络科技",
)


def upgrade() -> None:
    op.add_column(
        "receipt_keyword_mappings", sa.Column("transport_type", sa.String(32), nullable=True)
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE receipt_keyword_mappings SET transport_type = 'taxi' "
            "WHERE category_id = 'local_transport' AND normalized_keyword IN ('出租车', '出租汽车')"
        )
    )
    mappings = sa.table(
        "receipt_keyword_mappings",
        sa.column("normalized_keyword", sa.String()),
        sa.column("category_id", sa.String()),
        sa.column("transport_type", sa.String()),
    )
    connection.execute(
        mappings.update()
        .where(
            mappings.c.category_id == "local_transport",
            mappings.c.normalized_keyword.in_(_RIDE_HAILING),
        )
        .values(transport_type="ride_hailing")
    )
    existing = set(
        connection.execute(
            sa.text("SELECT normalized_keyword FROM receipt_keyword_mappings")
        ).scalars()
    )
    # Existing business mappings win, including words previously moved to another category.
    rows = [
        {
            "keyword": word,
            "normalized_keyword": word,
            "category_id": "local_transport",
            "transport_type": "ride_hailing",
        }
        for word in _RIDE_HAILING
        if word not in existing
    ] + [
        {
            "keyword": word,
            "normalized_keyword": word,
            "category_id": "airfare",
            "transport_type": None,
        }
        for word in ("机场", "燃油附加费")
        if word not in existing
    ]
    if "客运服务" not in existing:
        rows.append(
            {
                "keyword": "客运服务",
                "normalized_keyword": "客运服务",
                "category_id": "local_transport",
                "transport_type": None,
            }
        )
    if rows:
        op.bulk_insert(
            sa.table(
                "receipt_keyword_mappings",
                sa.column("keyword", sa.String()),
                sa.column("normalized_keyword", sa.String()),
                sa.column("category_id", sa.String()),
                sa.column("transport_type", sa.String()),
            ),
            rows,
        )


def downgrade() -> None:
    raise RuntimeError(
        "Cannot downgrade: transport keyword groups contain editable business settings"
    )
