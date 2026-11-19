from sqlalchemy import BigInteger, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SequenceCounter(Base):
    """Atomic per-year counters for server-generated numbers (e.g. job numbers).

    A row is incremented with INSERT ... ON CONFLICT DO UPDATE ... RETURNING,
    which takes a row lock held until the surrounding transaction ends, so two
    concurrent transactions can never receive the same value.
    """

    __tablename__ = "sequence_counters"
    __table_args__ = (
        CheckConstraint(
            "last_value > 0", name="ck_sequence_counters_last_value_positive"
        ),
    )

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
