"""
Canonical event schemas for the Weasley location pipeline.

Raw events flow from the desktop app into SQS.
Enriched events flow from the location processor Lambda into SNS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RawLocationEvent:
    """Published by the desktop app to SQS — one per family member per poll."""

    person: str  # display name from family_members config
    device_name: str  # raw iCloud device name
    lat: float
    lon: float
    timestamp: int  # millisecond epoch from iCloud
    accuracy: Optional[float] = None
    battery_level: Optional[float] = None  # 0-1 float
    battery_status: Optional[str] = None  # e.g. "Charging"
    published_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RawLocationEvent:
        return cls(
            person=data["person"],
            device_name=data.get("device_name", ""),
            lat=data["lat"],
            lon=data["lon"],
            timestamp=data["timestamp"],
            accuracy=data.get("accuracy"),
            battery_level=data.get("battery_level"),
            battery_status=data.get("battery_status"),
            published_at=data.get(
                "published_at", datetime.now(timezone.utc).isoformat()
            ),
        )


@dataclass
class EnrichedLocationEvent:
    """Published by the location processor Lambda to SNS."""

    person: str
    lat: float
    lon: float
    location_label: str  # resolved place name or geocoded address
    previous_label: Optional[str]  # label from last known location, or None
    trigger: str  # "movement" | "heartbeat"
    timestamp: int  # original iCloud timestamp (ms epoch)
    accuracy: Optional[float] = None
    battery_level: Optional[float] = None
    battery_status: Optional[str] = None
    distance_moved_m: Optional[float] = None  # meters from previous location
    enriched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    TRIGGER_MOVEMENT = "movement"
    TRIGGER_HEARTBEAT = "heartbeat"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> EnrichedLocationEvent:
        return cls(
            person=data["person"],
            lat=data["lat"],
            lon=data["lon"],
            location_label=data["location_label"],
            previous_label=data.get("previous_label"),
            trigger=data["trigger"],
            timestamp=data["timestamp"],
            accuracy=data.get("accuracy"),
            battery_level=data.get("battery_level"),
            battery_status=data.get("battery_status"),
            distance_moved_m=data.get("distance_moved_m"),
            enriched_at=data.get("enriched_at", datetime.now(timezone.utc).isoformat()),
        )
