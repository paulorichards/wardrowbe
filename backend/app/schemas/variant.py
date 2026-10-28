from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.utils.signed_urls import sign_image_url, sign_optional

# Fields a variant shares with its main item; edited on the main item and copied down.
IDENTITY_FIELDS = ("name", "brand", "type", "subtype")


class VariantSummary(BaseModel):
    """What a client needs to render a variant picker without a request per variant."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    variant_name: str | None = None
    primary_color: str | None = None
    colors: list[str] = Field(default_factory=list)
    material: str | None = None
    status: str
    is_archived: bool = False
    image_path: str
    thumbnail_path: str | None = None
    medium_path: str | None = None
    updated_at: datetime

    @computed_field
    @property
    def image_url(self) -> str:
        return sign_image_url(self.image_path)

    @computed_field
    @property
    def thumbnail_url(self) -> str | None:
        return sign_optional(self.thumbnail_path)

    @computed_field
    @property
    def medium_url(self) -> str | None:
        return sign_optional(self.medium_path)


class AttachRequest(BaseModel):
    parent_item_id: UUID
    variant_name: str = Field(min_length=1, max_length=100)
