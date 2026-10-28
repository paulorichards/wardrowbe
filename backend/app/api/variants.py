"""Item variants: create, attach and detach. See app/services/variant_service.py."""

import logging
from typing import Annotated
from uuid import UUID

from arq import create_pool
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.api.items import _read_image_or_413
from app.config import get_settings
from app.database import DbSession
from app.models.item import ClothingItem
from app.models.user import User
from app.schemas.item import ItemResponse
from app.schemas.variant import AttachRequest
from app.services.image_service import ImageService
from app.services.item_service import ItemService
from app.services.variant_service import (
    VariantRuleError,
    attach_variant,
    clear_user_suggestions,
    detach_variant,
    new_variant_row,
)
from app.utils.auth import get_current_user
from app.workers.queues import TAGGING_QUEUE
from app.workers.settings import get_redis_settings

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/items", tags=["Item variants"])


async def _owned_item(item_service: ItemService, item_id: UUID, user: User) -> ClothingItem:
    item = await item_service.get_by_id(item_id, user.id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


@router.post(
    "/{item_id}/variants", response_model=ItemResponse, status_code=status.HTTP_201_CREATED
)
async def create_variant(
    item_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
    image: UploadFile = File(...),
    variant_name: str = Form(..., min_length=1, max_length=100),
    colors: str | None = Form(None),
    primary_color: str | None = Form(None),
    material: str | None = Form(None),
    pattern: str | None = Form(None),
    notes: str | None = Form(None),
    favorite: bool = Form(False),
    skip_ai: bool = Form(False),
) -> ItemResponse:
    item_service = ItemService(db)
    parent = await _owned_item(item_service, item_id, current_user)
    if parent.parent_item_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A variant cannot have variants; add it to the main item",
        )

    image_service = ImageService()
    content = await _read_image_or_413(image)
    content_type = image.content_type or "application/octet-stream"
    if not image_service.validate_image(content, content_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image file. Supported formats: JPEG, PNG, WebP, HEIC",
        )

    try:
        image_hash = image_service.compute_phash(content, image.filename or "upload.jpg")
        existing = await item_service.find_duplicate_by_hash(current_user.id, image_hash)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Duplicate image detected. This item already exists in your wardrobe (ID: {existing.id})",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to compute image hash: {e}")

    try:
        image_paths = await image_service.process_and_store(
            user_id=current_user.id,
            image_data=content,
            original_filename=image.filename or "upload.jpg",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None

    variant = new_variant_row(
        parent,
        image_paths=image_paths,
        variant_name=variant_name.strip(),
        colors=colors.split(",") if colors else None,
        primary_color=primary_color,
        material=material,
        pattern=pattern,
        notes=notes,
        favorite=favorite,
    )
    db.add(variant)
    await db.flush()
    await db.refresh(parent, ["variant_rows"])
    await clear_user_suggestions(current_user.id)

    if settings.effective_ai_vision_enabled and not skip_ai:
        # Commit before enqueuing: the worker reads on its own connection.
        await db.commit()
        try:
            redis = await create_pool(get_redis_settings())
            try:
                full_image_path = str(image_service.get_image_path(image_paths["image_path"]))
                job = await redis.enqueue_job(
                    "tag_item_image",
                    str(variant.id),
                    full_image_path,
                    _queue_name=TAGGING_QUEUE,
                )
                variant.ai_job_id = job.job_id
                await db.commit()
            finally:
                await redis.aclose()
        except Exception as e:
            logger.error(f"Failed to queue AI tagging job: {e}")
        variant = await item_service.get_by_id(variant.id, current_user.id)
    else:
        variant = await item_service.mark_pending(variant, set_ready=True)

    return ItemResponse.model_validate(variant)


@router.post("/{item_id}/attach", response_model=ItemResponse)
async def attach_item(
    item_id: UUID,
    request: AttachRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> ItemResponse:
    item_service = ItemService(db)
    item = await _owned_item(item_service, item_id, current_user)
    parent = await _owned_item(item_service, request.parent_item_id, current_user)
    try:
        attach_variant(item, parent, request.variant_name.strip())
    except VariantRuleError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None
    await db.flush()
    await clear_user_suggestions(current_user.id)
    await db.refresh(parent, ["variant_rows"])
    return ItemResponse.model_validate(await item_service.get_by_id(item.id, current_user.id))


@router.post("/{item_id}/detach", response_model=ItemResponse)
async def detach_item(
    item_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> ItemResponse:
    item_service = ItemService(db)
    item = await _owned_item(item_service, item_id, current_user)
    parent_id = item.parent_item_id
    try:
        detach_variant(item)
    except VariantRuleError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None
    await db.flush()
    await clear_user_suggestions(current_user.id)
    parent = await item_service.get_by_id(parent_id, current_user.id)
    if parent:
        await db.refresh(parent, ["variant_rows"])
    return ItemResponse.model_validate(await item_service.get_by_id(item.id, current_user.id))
