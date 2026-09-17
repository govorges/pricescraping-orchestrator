"""ORM models.

The ``Vendor`` / ``QueryObject`` rows are the panel-managed source of truth that
the scheduler resolves into the *embedded snapshots* a worker job carries
(see ``pricescraping-worker/config/job.example.json``). ``PriceObservation``
mirrors the ``price_observation`` table proposed in the worker README so a
production TimescaleDB can host it unchanged.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orchestrator.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Vendor(Base):
    __tablename__ = "vendor"

    identifier: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    # full frogscraper-compatible vendor block: metadata/url/selectors/... minus the identifier
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def snapshot(self) -> dict:
        """The ``vendor`` block for a job payload."""
        return {"identifier": self.identifier, **(self.config or {})}


class Category(Base):
    """A named group of query objects — the scoped analogue of frogscraper's single
    global filtered-phrases list. Every query in the category gets these extra
    ``Exclude`` phrases OR'd onto its own, plus optional price-band guard rails,
    merged in at job-build time (see :meth:`QueryObject.snapshot`)."""

    __tablename__ = "category"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    extra_exclude: Mapped[list] = mapped_column(JSON, default=list)
    exclude_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    # optional guard rails: widen a query's own band inward, never outward
    minimum_value_floor: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_value_ceiling: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    queries: Mapped[list[QueryObject]] = relationship(back_populates="category")


class QueryObject(Base):
    __tablename__ = "query_object"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content: Mapped[str] = mapped_column(String(512), default="")
    include: Mapped[list] = mapped_column(JSON, default=list)
    include_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    exclude: Mapped[list] = mapped_column(JSON, default=list)
    exclude_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    minimum_value: Mapped[float] = mapped_column(Float, default=0.0)
    maximum_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("category.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    category: Mapped[Category | None] = relationship(back_populates="queries", lazy="joined")

    def effective_exclude(self) -> list[str]:
        own = list(self.exclude or [])
        if self.category:
            for phrase in self.category.extra_exclude or []:
                if phrase not in own:
                    own.append(phrase)
        return own

    def effective_band(self) -> tuple[float, float | None]:
        lo = self.minimum_value or 0.0
        hi = self.maximum_value
        if self.category:
            if self.category.minimum_value_floor is not None:
                lo = max(lo, self.category.minimum_value_floor)
            if self.category.maximum_value_ceiling is not None:
                hi = min(hi, self.category.maximum_value_ceiling) if hi is not None \
                    else self.category.maximum_value_ceiling
        return lo, hi

    def snapshot(self) -> dict:
        """The ``query_object`` block for a job payload — category exclude
        phrases and price-band guard rails already merged in."""
        lo, hi = self.effective_band()
        out: dict = {
            "id": self.id,
            "Content": self.content,
            "Include": list(self.include or []),
            "Exclude": self.effective_exclude(),
            "MinimumValue": lo,
        }
        if self.include_settings:
            out["IncludeSettings"] = self.include_settings
        exclude_settings = self.exclude_settings or (self.category.exclude_settings if self.category else None)
        if exclude_settings:
            out["ExcludeSettings"] = exclude_settings
        if hi is not None:
            out["MaximumValue"] = hi
        return out


class Schedule(Base):
    __tablename__ = "schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendor.identifier", ondelete="CASCADE"))
    query_object_id: Mapped[str] = mapped_column(
        ForeignKey("query_object.id", ondelete="CASCADE")
    )
    cron: Mapped[str] = mapped_column(String(128))
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    vendor: Mapped[Vendor] = relationship(lazy="joined")
    query_object: Mapped[QueryObject] = relationship(lazy="joined")


class CategorySchedule(Base):
    """A recurring rule scoped to a whole category: on its cron schedule, one job
    is assembled and enqueued for *every* query object in the category, against
    either one vendor or every enabled vendor (``vendor_id is None``)."""

    __tablename__ = "category_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    category_id: Mapped[str] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"))
    # None = every enabled vendor, resolved fresh on each tick
    vendor_id: Mapped[str | None] = mapped_column(
        ForeignKey("vendor.identifier", ondelete="CASCADE"), nullable=True
    )
    cron: Mapped[str] = mapped_column(String(128))
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    category: Mapped[Category] = relationship(lazy="joined")
    vendor: Mapped[Vendor | None] = relationship(lazy="joined")


class Job(Base):
    __tablename__ = "job"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule.id", ondelete="SET NULL"), nullable=True
    )
    category_schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("category_schedule.id", ondelete="SET NULL"), nullable=True
    )
    vendor_id: Mapped[str] = mapped_column(String(128), index=True)
    query_object_id: Mapped[str] = mapped_column(String(128), index=True)
    # which worker Node actually ran this job, from the reported result's
    # worker.node field -- None for jobs that never got a result (queued/dead).
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    scheduled_by: Mapped[str] = mapped_column(String(256), default="")
    msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    listings: Mapped[list[Listing]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    dropped_listings: Mapped[list[DroppedListing]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class PriceObservation(Base):
    __tablename__ = "price_observation"
    __table_args__ = (
        UniqueConstraint(
            "vendor_id", "query_object_id", "observed_date", "job_id", name="uq_price_obs"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_date: Mapped[date] = mapped_column(Date, index=True)
    vendor_id: Mapped[str] = mapped_column(String(128), index=True)
    query_object_id: Mapped[str] = mapped_column(String(128), index=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    price_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    listings_kept: Mapped[int] = mapped_column(Integer, default=0)


class Listing(Base):
    __tablename__ = "listing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.job_id", ondelete="CASCADE"), index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Float)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    # operator-set: hide a noisy/irrelevant listing from the panel without touching scrape data
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # "auction" | "buy_now" | "unknown" — see orchestrator.classify.classify_listing_type
    listing_type: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    tracked_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracked_listing.id", ondelete="SET NULL"), nullable=True, index=True
    )

    job: Mapped[Job] = relationship(back_populates="listings")


class DroppedListing(Base):
    """A listing the worker's selector found on the page but didn't keep --
    kept separate from :class:`Listing` (rather than a nullable-price row
    there) so it never leaks into the price-sorted ``/api/v1/listings``
    browsing endpoint, which assumes every row is a real, priced listing."""

    __tablename__ = "dropped_listing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.job_id", ondelete="CASCADE"), index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="dropped_listings")


class TrackedListing(Base):
    """One real-world listing, identified across scrapes by its canonicalised URL
    (session/tracking query args stripped — see :mod:`orchestrator.urlnorm`)."""

    __tablename__ = "tracked_listing"
    __table_args__ = (
        UniqueConstraint("vendor_id", "canonical_url", name="uq_tracked_listing"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[str] = mapped_column(String(128), index=True)
    query_object_id: Mapped[str] = mapped_column(String(128), index=True)
    canonical_url: Mapped[str] = mapped_column(Text, index=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)  # latest full URL, for "open"
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)
    listing_type: Mapped[str] = mapped_column(String(16), default="unknown")
    first_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    observations_count: Mapped[int] = mapped_column(Integer, default=0)
    # operator-set: hide a noisy/irrelevant listing from the panel
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    price_points: Mapped[list[ListingPricePoint]] = relationship(
        back_populates="tracked_listing", cascade="all, delete-orphan",
        order_by="ListingPricePoint.observed_at",
    )

    @property
    def price_change_pct(self) -> float | None:
        if self.first_price in (None, 0) or self.last_price is None:
            return None
        return round((self.last_price - self.first_price) / self.first_price * 100, 1)


class ListingPricePoint(Base):
    __tablename__ = "listing_price_point"
    __table_args__ = (
        UniqueConstraint("tracked_listing_id", "job_id", name="uq_listing_price_point"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tracked_listing_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_listing.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[float] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    tracked_listing: Mapped[TrackedListing] = relationship(back_populates="price_points")


class Node(Base):
    __tablename__ = "node"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="local")
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    replicas_desired: Mapped[int] = mapped_column(Integer, default=0)
    replicas_running: Mapped[int] = mapped_column(Integer, default=0)
    image: Mapped[str] = mapped_column(String(256), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
