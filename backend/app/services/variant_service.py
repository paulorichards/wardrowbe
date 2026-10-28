"""Item variants: a variant is a clothing_items row whose parent_item_id points at its main item.

The wardrobe keeps one level only. Everything that already works per row (wear, wash, outfits,
learned pairs, colour stats) keeps working per variant; this module holds what is new: grouping,
shared identity, attaching and detaching, and cache invalidation. See docs/variantes.md in the
closet repo.
"""

from uuid import UUID

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import aliased

from app.models.item import ClothingItem, ItemStatus
from app.schemas.item import ItemUpdate
from app.schemas.variant import IDENTITY_FIELDS
from app.services.suggestion_cache import clear_suggestions
from app.utils.garment_vocabulary import OCCASIONS

# PATCH keys that change what an outfit looks like, so cached suggestions go stale.
COLOR_FIELDS = ("colors", "primary_color")


def variant_color_match(colors: list[str]):
    """A row matches a colour filter when it, or any of its variants, has one of the colours."""
    child = aliased(ClothingItem)
    return or_(
        ClothingItem.colors.overlap(colors),
        exists().where(and_(child.parent_item_id == ClothingItem.id, child.colors.overlap(colors))),
    )


def family_root_id(item: ClothingItem) -> UUID:
    return item.parent_item_id or item.id


async def clear_user_suggestions(user_id: UUID) -> None:
    """Drop cached suggestions for every occasion; an item can appear in any of them."""
    for occasion in OCCASIONS:
        await clear_suggestions(user_id, occasion)


def drop_identity_for_variant(item: ClothingItem, item_data: ItemUpdate) -> ItemUpdate:
    """On a variant, name/brand/type/subtype belong to the main item and are ignored."""
    if item.parent_item_id is None:
        return item_data
    kept = item_data.model_dump(exclude_unset=True)
    for field in IDENTITY_FIELDS:
        kept.pop(field, None)
    return ItemUpdate(**kept)


def propagate_identity(item: ClothingItem, update_data: dict) -> None:
    """Copy identity fields edited on a main item down to its variants (flushed by the caller)."""
    for variant in item.variants:
        for field in IDENTITY_FIELDS:
            if field in update_data:
                setattr(variant, field, update_data[field])


def touches_colors(update_data: dict) -> bool:
    tags = update_data.get("tags") or {}
    if not isinstance(tags, dict):
        tags = tags.model_dump(exclude_unset=True)
    return any(f in update_data for f in COLOR_FIELDS) or any(f in tags for f in COLOR_FIELDS)


class VariantRuleError(ValueError):
    """A request that would break the one-level, same-type, same-owner variant rules."""


def attach_variant(item: ClothingItem, parent: ClothingItem, variant_name: str) -> None:
    if item.id == parent.id:
        raise VariantRuleError("An item cannot be a variant of itself")
    if item.user_id != parent.user_id:
        raise VariantRuleError("Both items must belong to the same user")
    if parent.parent_item_id is not None:
        raise VariantRuleError("The target is itself a variant; attach to its main item")
    if item.variants:
        raise VariantRuleError("This item has variants of its own and cannot become one")
    if item.type != parent.type:
        raise VariantRuleError("A variant must have the same type as its main item")
    item.parent_item_id = parent.id
    item.variant_name = variant_name
    item.name = parent.name
    item.brand = parent.brand


def detach_variant(item: ClothingItem) -> None:
    if item.parent_item_id is None:
        raise VariantRuleError("This item is not a variant")
    item.parent_item_id = None


def new_variant_row(
    parent: ClothingItem,
    *,
    image_paths: dict[str, str],
    variant_name: str,
    colors: list[str] | None,
    primary_color: str | None,
    material: str | None,
    pattern: str | None,
    notes: str | None,
    favorite: bool,
) -> ClothingItem:
    """A new variant row: identity and the shared classification come from the main item."""
    tags: dict = {}
    for key, value in (
        ("colors", colors),
        ("primary_color", primary_color),
        ("material", material),
        ("pattern", pattern),
        ("style", parent.style),
        ("season", parent.season),
        ("formality", parent.formality),
    ):
        if value:
            tags[key] = list(value) if isinstance(value, list) else value
    return ClothingItem(
        user_id=parent.user_id,
        parent_item_id=parent.id,
        variant_name=variant_name,
        image_path=image_paths["image_path"],
        thumbnail_path=image_paths.get("thumbnail_path"),
        medium_path=image_paths.get("medium_path"),
        image_hash=image_paths.get("image_hash"),
        type=parent.type,
        subtype=parent.subtype,
        name=parent.name,
        brand=parent.brand,
        formality=parent.formality,
        style=list(parent.style or []),
        season=list(parent.season or []),
        wash_interval=parent.wash_interval,
        colors=colors or [],
        primary_color=primary_color,
        material=material,
        pattern=pattern,
        notes=notes,
        favorite=favorite,
        tags=tags,
        status=ItemStatus.processing,
    )
