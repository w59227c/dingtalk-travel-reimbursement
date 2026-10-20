from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ReceiptKeywordMapping(Base):
    __tablename__ = "receipt_keyword_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(100))
    normalized_keyword: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    category_id: Mapped[str] = mapped_column(String(64), index=True)
    transport_type: Mapped[str | None] = mapped_column(String(32))
