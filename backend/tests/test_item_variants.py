from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import ClothingItem, ItemStatus
from app.models.user import User
from app.services.suggestion_cache import has_cached, push_suggestions

API = "/api/v1/items"


async def make_item(
    db: AsyncSession,
    user: User,
    *,
    type: str = "watch",
    name: str | None = "Bulova Jet Star",
    parent: ClothingItem | None = None,
    variant_name: str | None = None,
    colors: list[str] | None = None,
    **extra,
) -> ClothingItem:
    item = ClothingItem(
        user_id=user.id,
        type=type,
        name=name,
        image_path=f"test/{uuid4()}.jpg",
        status=ItemStatus.ready,
        colors=colors or [],
        primary_color=(colors or [None])[0],
        parent_item_id=parent.id if parent else None,
        variant_name=variant_name,
        **extra,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


def jpeg_bytes(color=(100, 150, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
async def family(db_session: AsyncSession, test_user: User):
    parent = await make_item(
        db_session, test_user, variant_name="Steel bracelet", colors=["silver"]
    )
    child = await make_item(
        db_session, test_user, parent=parent, variant_name="Blue strap", colors=["blue"]
    )
    # The client shares this session; drop the collection cached before the child existed.
    ids = SimpleNamespace(id=parent.id), SimpleNamespace(id=child.id)
    await db_session.refresh(parent, ["variant_rows"])
    return ids


class TestListing:
    async def test_list_hides_children(self, client: AsyncClient, auth_headers, family):
        parent, child = family
        data = (await client.get(API, headers=auth_headers)).json()
        assert [i["id"] for i in data["items"]] == [str(parent.id)]
        assert data["total"] == 1

    async def test_include_variants_shows_children(self, client: AsyncClient, auth_headers, family):
        parent, child = family
        data = (
            await client.get(API, params={"include_variants": True}, headers=auth_headers)
        ).json()
        assert {i["id"] for i in data["items"]} == {str(parent.id), str(child.id)}
        assert data["total"] == 2

    async def test_color_filter_matches_through_child(
        self, client: AsyncClient, auth_headers, family
    ):
        parent, _ = family
        data = (await client.get(API, params={"colors": "blue"}, headers=auth_headers)).json()
        assert [i["id"] for i in data["items"]] == [str(parent.id)]

    async def test_color_filter_without_match_is_empty(
        self, client: AsyncClient, auth_headers, family
    ):
        data = (await client.get(API, params={"colors": "green"}, headers=auth_headers)).json()
        assert data["items"] == []

    async def test_response_carries_variants_and_count(
        self, client: AsyncClient, auth_headers, family
    ):
        parent, child = family
        [item] = (await client.get(API, headers=auth_headers)).json()["items"]
        assert item["variant_count"] == 1
        assert item["variant_name"] == "Steel bracelet"
        assert item["parent_item_id"] is None
        [summary] = item["variants"]
        assert summary["id"] == str(child.id)
        assert summary["variant_name"] == "Blue strap"
        assert summary["colors"] == ["blue"]
        assert summary["primary_color"] == "blue"
        assert summary["status"] == "ready"
        assert summary["is_archived"] is False
        assert summary["image_url"] and "updated_at" in summary
        assert {"material", "thumbnail_url", "medium_url"} <= summary.keys()

    async def test_child_response_points_at_parent_with_no_variants(
        self, client: AsyncClient, auth_headers, family
    ):
        parent, child = family
        data = (await client.get(f"{API}/{child.id}", headers=auth_headers)).json()
        assert data["parent_item_id"] == str(parent.id)
        assert data["variants"] == []
        assert data["variant_count"] == 0

    async def test_plain_item_has_no_variants(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        item = await make_item(db_session, test_user, type="shirt")
        data = (await client.get(f"{API}/{item.id}", headers=auth_headers)).json()
        assert data["variants"] == [] and data["variant_count"] == 0
        assert data["parent_item_id"] is None and data["variant_name"] is None


async def reload(db: AsyncSession, item_id) -> ClothingItem:
    db.expire_all()
    return (await db.execute(select(ClothingItem).where(ClothingItem.id == item_id))).scalar_one()


class TestSharedIdentity:
    async def test_patch_on_parent_propagates_identity(
        self, client: AsyncClient, auth_headers, db_session, family
    ):
        parent, child = family
        resp = await client.patch(
            f"{API}/{parent.id}",
            json={"name": "Bulova 1973", "brand": "Bulova", "subtype": "dress"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        kid = await reload(db_session, child.id)
        assert (kid.name, kid.brand, kid.subtype) == ("Bulova 1973", "Bulova", "dress")
        assert kid.colors == ["blue"]  # per-variant fields stay

    async def test_patch_on_parent_propagates_type(
        self, client: AsyncClient, auth_headers, db_session, family
    ):
        parent, child = family
        await client.patch(f"{API}/{parent.id}", json={"type": "bracelet"}, headers=auth_headers)
        assert (await reload(db_session, child.id)).type == "bracelet"

    async def test_identity_fields_ignored_on_child(
        self, client: AsyncClient, auth_headers, db_session, family
    ):
        parent, child = family
        resp = await client.patch(
            f"{API}/{child.id}",
            json={
                "name": "Hacked",
                "brand": "X",
                "type": "belt",
                "variant_name": "Green strap",
                "colors": ["green"],
                "primary_color": "green",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Bulova Jet Star" and body["type"] == "watch"
        assert body["brand"] is None
        assert body["variant_name"] == "Green strap"
        assert body["colors"] == ["green"]
        assert (await reload(db_session, parent.id)).name == "Bulova Jet Star"

    async def test_child_color_patch_clears_suggestion_cache(
        self, client: AsyncClient, auth_headers, family, test_user
    ):
        await push_suggestions(test_user.id, "casual", [{"x": 1}])
        assert await has_cached(test_user.id, "casual")
        await client.patch(
            f"{API}/{family[1].id}", json={"colors": ["green"]}, headers=auth_headers
        )
        assert not await has_cached(test_user.id, "casual")


class TestLifecycle:
    async def test_archive_parent_archives_children_and_restore_restores(
        self, client: AsyncClient, auth_headers, db_session, family
    ):
        parent, child = family
        resp = await client.post(
            f"{API}/{parent.id}/archive", json={"reason": "sold"}, headers=auth_headers
        )
        assert resp.status_code == 200
        kid = await reload(db_session, child.id)
        assert (
            kid.is_archived and kid.status == ItemStatus.archived and kid.archive_reason == "sold"
        )

        resp = await client.post(f"{API}/{parent.id}/restore", headers=auth_headers)
        assert resp.status_code == 200
        kid = await reload(db_session, child.id)
        assert not kid.is_archived and kid.status == ItemStatus.ready and kid.archived_at is None

    async def test_archive_child_leaves_parent(
        self, client: AsyncClient, auth_headers, db_session, family
    ):
        parent, child = family
        await client.post(f"{API}/{child.id}/archive", json={}, headers=auth_headers)
        assert (await reload(db_session, child.id)).is_archived
        assert not (await reload(db_session, parent.id)).is_archived

    async def test_delete_parent_deletes_children(
        self, client: AsyncClient, auth_headers, db_session, family
    ):
        parent, child = family
        resp = await client.delete(f"{API}/{parent.id}", headers=auth_headers)
        assert resp.status_code == 204
        db_session.expire_all()
        assert await db_session.get(ClothingItem, child.id) is None

    async def test_delete_child_leaves_parent(
        self, client: AsyncClient, auth_headers, db_session, family
    ):
        parent, child = family
        assert (await client.delete(f"{API}/{child.id}", headers=auth_headers)).status_code == 204
        assert (await reload(db_session, parent.id)).variants == []

    async def test_delete_parent_removes_children_image_files(
        self, client: AsyncClient, auth_headers, family
    ):
        parent, child = family
        with patch("app.api.items.ImageService") as service:
            await client.delete(f"{API}/{parent.id}", headers=auth_headers)
        assert service.return_value.delete_images.call_count == 2

    async def test_db_cascade_removes_children(self, db_session, family):
        parent, child = family
        await db_session.execute(text("DELETE FROM clothing_items WHERE id = :i"), {"i": parent.id})
        await db_session.commit()
        db_session.expire_all()
        assert await db_session.get(ClothingItem, child.id) is None

    async def test_bulk_select_all_delete_skips_children_without_errors(
        self, client: AsyncClient, auth_headers, family
    ):
        resp = await client.post(
            f"{API}/bulk/delete", json={"select_all": True}, headers=auth_headers
        )
        assert resp.json()["deleted"] == 1 and resp.json()["failed"] == 0


class TestCreateVariant:
    async def test_copies_identity_and_takes_own_colors(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        parent = await make_item(
            db_session,
            test_user,
            name="Bulova Jet Star",
            brand="Bulova",
            subtype="dress",
            formality="smart-casual",
            style=["classic"],
            season=["all-season"],
            wash_interval=30,
            colors=["silver"],
        )
        parent_id = parent.id
        await db_session.refresh(parent, ["variant_rows"])
        with patch("app.api.variants.create_pool", new_callable=AsyncMock) as pool:
            pool.return_value.enqueue_job.return_value.job_id = "job-1"
            resp = await client.post(
                f"{API}/{parent_id}/variants",
                files={"image": ("strap.jpg", jpeg_bytes((10, 20, 200)), "image/jpeg")},
                data={
                    "variant_name": "Blue leather strap",
                    "colors": "blue,brown",
                    "primary_color": "blue",
                    "material": "leather",
                    "skip_ai": "true",
                },
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["parent_item_id"] == str(parent_id)
        assert body["variant_name"] == "Blue leather strap"
        assert (body["name"], body["brand"], body["type"], body["subtype"]) == (
            "Bulova Jet Star",
            "Bulova",
            "watch",
            "dress",
        )
        assert body["formality"] == "smart-casual"
        assert body["style"] == ["classic"] and body["season"] == ["all-season"]
        assert body["wash_interval"] == 30
        assert body["colors"] == ["blue", "brown"] and body["primary_color"] == "blue"
        assert body["material"] == "leather"
        assert body["status"] == "ready"
        pool.return_value.enqueue_job.assert_not_called()

        listed = (await client.get(f"{API}/{parent_id}", headers=auth_headers)).json()
        assert listed["variant_count"] == 1 and listed["variants"][0]["id"] == body["id"]

    async def test_queues_ai_tagging_by_default_and_clears_cache(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        parent = await make_item(db_session, test_user)
        parent_id, user_id = parent.id, test_user.id
        await push_suggestions(user_id, "casual", [{"x": 1}])
        with patch("app.api.variants.create_pool", new_callable=AsyncMock) as pool:
            pool.return_value.enqueue_job.return_value.job_id = "job-1"
            resp = await client.post(
                f"{API}/{parent_id}/variants",
                files={"image": ("strap.jpg", jpeg_bytes((200, 20, 10)), "image/jpeg")},
                data={"variant_name": "Red strap"},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        pool.return_value.enqueue_job.assert_called_once()
        assert not await has_cached(user_id, "casual")

    async def test_variant_name_is_required(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        parent = await make_item(db_session, test_user)
        resp = await client.post(
            f"{API}/{parent.id}/variants",
            files={"image": ("s.jpg", jpeg_bytes(), "image/jpeg")},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_cannot_add_variant_to_a_variant(self, client: AsyncClient, auth_headers, family):
        resp = await client.post(
            f"{API}/{family[1].id}/variants",
            files={"image": ("s.jpg", jpeg_bytes((1, 2, 3)), "image/jpeg")},
            data={"variant_name": "Nested", "skip_ai": "true"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_unknown_parent_is_404(self, client: AsyncClient, auth_headers, test_user):
        resp = await client.post(
            f"{API}/{uuid4()}/variants",
            files={"image": ("s.jpg", jpeg_bytes(), "image/jpeg")},
            data={"variant_name": "x"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestAttachDetach:
    async def two_watches(self, db, user):
        main = await make_item(db, user, name="Bulova Jet Star", brand="Bulova", colors=["silver"])
        loose = await make_item(db, user, name="Strap thing", brand="Other", colors=["blue"])
        return main.id, loose.id

    async def test_attach_makes_variant_and_takes_name_and_brand(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        main_id, loose_id = await self.two_watches(db_session, test_user)
        resp = await client.post(
            f"{API}/{loose_id}/attach",
            json={"parent_item_id": str(main_id), "variant_name": "Blue leather strap"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["parent_item_id"] == str(main_id)
        assert body["variant_name"] == "Blue leather strap"
        assert (body["name"], body["brand"]) == ("Bulova Jet Star", "Bulova")
        assert body["colors"] == ["blue"]
        parent = (await client.get(f"{API}/{main_id}", headers=auth_headers)).json()
        assert parent["variant_count"] == 1
        listing = (await client.get(API, headers=auth_headers)).json()
        assert [i["id"] for i in listing["items"]] == [str(main_id)]

    async def test_attach_clears_suggestion_cache(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        user_id = test_user.id
        main_id, loose_id = await self.two_watches(db_session, test_user)
        await push_suggestions(user_id, "casual", [{"x": 1}])
        await client.post(
            f"{API}/{loose_id}/attach",
            json={"parent_item_id": str(main_id), "variant_name": "v"},
            headers=auth_headers,
        )
        assert not await has_cached(user_id, "casual")

    async def test_attach_type_mismatch(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        main = await make_item(db_session, test_user)
        belt = await make_item(db_session, test_user, type="belt")
        resp = await client.post(
            f"{API}/{belt.id}/attach",
            json={"parent_item_id": str(main.id), "variant_name": "v"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_attach_to_a_variant_is_rejected(
        self, client: AsyncClient, auth_headers, db_session, test_user, family
    ):
        loose = await make_item(db_session, test_user)
        resp = await client.post(
            f"{API}/{loose.id}/attach",
            json={"parent_item_id": str(family[1].id), "variant_name": "v"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_attach_item_that_has_variants_is_rejected(
        self, client: AsyncClient, auth_headers, db_session, test_user, family
    ):
        other = await make_item(db_session, test_user)
        resp = await client.post(
            f"{API}/{family[0].id}/attach",
            json={"parent_item_id": str(other.id), "variant_name": "v"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_attach_to_itself_is_rejected(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        item = await make_item(db_session, test_user)
        resp = await client.post(
            f"{API}/{item.id}/attach",
            json={"parent_item_id": str(item.id), "variant_name": "v"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_attach_other_users_item_is_404(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        stranger = User(
            id=uuid4(),
            external_id=f"x-{uuid4()}",
            email=f"{uuid4()}@example.com",
            display_name="Other",
            timezone="UTC",
            is_active=True,
            onboarding_completed=False,
            email_verified=True,
        )
        db_session.add(stranger)
        await db_session.commit()
        theirs = await make_item(db_session, stranger)
        mine = await make_item(db_session, test_user)
        for item_id, parent_id in ((mine.id, theirs.id), (theirs.id, mine.id)):
            resp = await client.post(
                f"{API}/{item_id}/attach",
                json={"parent_item_id": str(parent_id), "variant_name": "v"},
                headers=auth_headers,
            )
            assert resp.status_code == 404

    async def test_detach_makes_item_loose_again(
        self, client: AsyncClient, auth_headers, db_session, test_user, family
    ):
        parent, child = family
        user_id = test_user.id
        await push_suggestions(user_id, "casual", [{"x": 1}])
        resp = await client.post(f"{API}/{child.id}/detach", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["parent_item_id"] is None
        assert not await has_cached(user_id, "casual")
        listing = (await client.get(API, headers=auth_headers)).json()
        assert {i["id"] for i in listing["items"]} == {str(parent.id), str(child.id)}
        main = (await client.get(f"{API}/{parent.id}", headers=auth_headers)).json()
        assert main["variant_count"] == 0

    async def test_detach_a_non_variant_is_rejected(
        self, client: AsyncClient, auth_headers, family
    ):
        resp = await client.post(f"{API}/{family[0].id}/detach", headers=auth_headers)
        assert resp.status_code == 400


class TestOutfitsAndAnalytics:
    async def test_outfit_and_pairing_items_carry_variant_labels(
        self, client: AsyncClient, auth_headers, db_session, test_user, family
    ):
        from datetime import date

        from app.models.outfit import Outfit, OutfitItem, OutfitSource, OutfitStatus

        parent, child = family
        outfit = Outfit(
            user_id=test_user.id,
            occasion="casual",
            scheduled_for=date.today(),
            status=OutfitStatus.pending,
            source=OutfitSource.pairing,
            source_item_id=child.id,
        )
        outfit.items.append(OutfitItem(item_id=child.id, position=0))
        db_session.add(outfit)
        await db_session.commit()

        pairing = (await client.get("/api/v1/pairings", headers=auth_headers)).json()["pairings"][0]
        outfit_json = (await client.get("/api/v1/outfits", headers=auth_headers)).json()["outfits"][
            0
        ]
        for payload in (pairing, outfit_json):
            [entry] = payload["items"]
            assert entry["parent_item_id"] == str(parent.id)
            assert entry["variant_name"] == "Blue strap"

    async def test_analytics_total_counts_families(
        self, client: AsyncClient, auth_headers, family, db_session, test_user
    ):
        await make_item(db_session, test_user, type="shirt", name="Tee")
        body = (await client.get("/api/v1/analytics", headers=auth_headers)).json()
        assert body["wardrobe"]["total_items"] == 2  # watch family + shirt, not 3 rows

    async def test_color_distribution_counts_each_variant(
        self, client: AsyncClient, auth_headers, family
    ):
        body = (await client.get(f"{API}/colors", headers=auth_headers)).json()
        assert {c["color"] for c in body} >= {"silver", "blue"}
