"""Google Play Developer API MCP Server.

Provides tools for managing Google Play app deployment, store listings, and in-app products:

Deployment:
- deploy_internal: Upload AAB and deploy to internal testing track
- get_app_info: Get app track information

Store Listing:
- get_store_listing: Get current store listing (title, descriptions)
- update_store_listing: Update store listing text
- upload_store_image: Upload a single image (icon, feature graphic, screenshot)
- batch_upload_store_images: Upload all images from a directory in one edit
- list_store_images: List uploaded images
- delete_store_image: Delete a single image
- delete_all_store_images: Delete all images of a given type

In-App Products:
- create_inapp_product: Create or update an in-app product
- activate_inapp_product: Activate a draft in-app product
- deactivate_inapp_product: Deactivate an in-app product
- list_inapp_products: List all in-app products
- batch_create_inapp_products: Create multiple products at once
- batch_activate_inapp_products: Activate multiple products at once

Subscriptions:
- list_subscriptions: List all subscription products
- create_subscription: Create a subscription with base plans
- update_subscription: Update subscription listings
- delete_subscription: Delete a subscription (no subscribers only)
- activate_base_plan: Activate a draft base plan
- deactivate_base_plan: Deactivate a base plan
- create_free_trial_offer: Create a free trial offer on a base plan
- activate_offer: Activate a draft offer
- deactivate_offer: Deactivate an offer
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("google-play")

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]


def _get_service():
    """Create Google Play Developer API service from environment variables."""
    key_file = os.environ.get("GOOGLE_PLAY_KEY_FILE")
    if not key_file:
        raise ValueError(
            "GOOGLE_PLAY_KEY_FILE environment variable is not set. "
            "Please set it to the path of your service account JSON key file."
        )
    if not os.path.exists(key_file):
        raise ValueError(f"Service account key file not found: {key_file}")

    credentials = service_account.Credentials.from_service_account_file(
        key_file, scopes=SCOPES
    )
    return build("androidpublisher", "v3", credentials=credentials)


def _get_package_name() -> str:
    """Get the package name from environment variable."""
    package_name = os.environ.get("GOOGLE_PLAY_PACKAGE_NAME")
    if not package_name:
        raise ValueError(
            "GOOGLE_PLAY_PACKAGE_NAME environment variable is not set. "
            "Please set it to your app's package name (e.g., com.example.app)."
        )
    return package_name


def _convert_region_prices(service, package_name: str, price_micros: int, currency_code: str = "USD") -> dict:
    """Convert a price in any currency to all regional prices."""
    units = price_micros // 1_000_000
    nanos = (price_micros % 1_000_000) * 1000

    result = service.monetization().convertRegionPrices(
        packageName=package_name,
        body={
            "price": {
                "currencyCode": currency_code,
                "units": str(units),
                "nanos": nanos,
            }
        },
    ).execute()

    return result


def _commit_edit(service, package_name: str, edit_id: str):
    """Commit an edit, handling draft apps that require a track update.

    Draft apps on Google Play require a track release with status "draft"
    to be present in the same edit when committing. This function always
    re-applies the internal track with draft status before committing.
    """
    # For draft apps: always re-apply internal track with draft status
    track = service.edits().tracks().get(
        packageName=package_name,
        editId=edit_id,
        track="internal",
    ).execute()

    releases = track.get("releases", [])
    if releases:
        # Only keep the latest release as draft (API allows only one draft)
        latest = releases[0]
        latest["status"] = "draft"
        releases = [latest]
    else:
        releases = [{"status": "draft"}]

    service.edits().tracks().update(
        packageName=package_name,
        editId=edit_id,
        track="internal",
        body={"track": "internal", "releases": releases},
    ).execute()

    # Now commit with the track update included
    service.edits().commit(packageName=package_name, editId=edit_id).execute()


@mcp.tool()
def deploy_internal(
    aab_path: str,
    release_notes_ko: str = "",
    release_notes_en: str = "",
    status: str = "draft",
) -> str:
    """Deploy an Android App Bundle to the internal testing track.

    IMPORTANT: This will upload your app bundle to Google Play.
    Make sure you have the correct bundle file before proceeding.

    Args:
        aab_path: Path to the .aab file to upload.
        release_notes_ko: Release notes in Korean (optional).
        release_notes_en: Release notes in English (optional).
        status: Release status - "draft" for unpublished apps, "completed" for
                published apps. Default is "draft".

    Returns:
        A message indicating success with version code and edit ID.
    """
    service = _get_service()
    package_name = _get_package_name()

    if not os.path.exists(aab_path):
        raise ValueError(f"AAB file not found: {aab_path}")

    # 1. Create edit
    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        # 2. Upload bundle
        media = MediaFileUpload(aab_path, mimetype="application/octet-stream")
        bundle = service.edits().bundles().upload(
            packageName=package_name,
            editId=edit_id,
            media_body=media,
        ).execute()
        version_code = bundle["versionCode"]

        # 3. Set track
        release_notes = []
        if release_notes_ko:
            release_notes.append({"language": "ko-KR", "text": release_notes_ko})
        if release_notes_en:
            release_notes.append({"language": "en-US", "text": release_notes_en})

        track_body = {
            "track": "internal",
            "releases": [{
                "versionCodes": [str(version_code)],
                "status": status,
            }],
        }
        if release_notes:
            track_body["releases"][0]["releaseNotes"] = release_notes

        service.edits().tracks().update(
            packageName=package_name,
            editId=edit_id,
            track="internal",
            body=track_body,
        ).execute()

        # 4. Commit
        service.edits().commit(packageName=package_name, editId=edit_id).execute()

        return (
            f"Successfully deployed to internal testing track.\n"
            f"Version code: {version_code}\n"
            f"Status: {status}\n"
            f"Edit ID: {edit_id}"
        )

    except Exception as e:
        # Delete edit on failure
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def deploy_track(
    aab_path: str,
    track: str = "internal",
    release_notes_ko: str = "",
    release_notes_en: str = "",
    status: str = "draft",
) -> str:
    """Deploy an Android App Bundle to any testing track.

    Args:
        aab_path: Path to the .aab file to upload.
        track: Target track - "internal", "alpha" (closed testing),
               "beta" (open testing), or "production".
        release_notes_ko: Release notes in Korean (optional).
        release_notes_en: Release notes in English (optional).
        status: Release status - "draft" for unpublished apps, "completed" for
                published apps. Default is "draft".

    Returns:
        A message indicating success with version code and edit ID.
    """
    valid_tracks = ("internal", "alpha", "beta", "production")
    if track not in valid_tracks:
        raise ValueError(f"Invalid track: {track}. Must be one of {valid_tracks}")

    service = _get_service()
    package_name = _get_package_name()

    if not os.path.exists(aab_path):
        raise ValueError(f"AAB file not found: {aab_path}")

    track_display = {
        "internal": "internal testing",
        "alpha": "closed testing",
        "beta": "open testing",
        "production": "production",
    }

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        media = MediaFileUpload(aab_path, mimetype="application/octet-stream")
        bundle = service.edits().bundles().upload(
            packageName=package_name,
            editId=edit_id,
            media_body=media,
        ).execute()
        version_code = bundle["versionCode"]

        release_notes = []
        if release_notes_ko:
            release_notes.append({"language": "ko-KR", "text": release_notes_ko})
        if release_notes_en:
            release_notes.append({"language": "en-US", "text": release_notes_en})

        track_body = {
            "track": track,
            "releases": [{
                "versionCodes": [str(version_code)],
                "status": status,
            }],
        }
        if release_notes:
            track_body["releases"][0]["releaseNotes"] = release_notes

        service.edits().tracks().update(
            packageName=package_name,
            editId=edit_id,
            track=track,
            body=track_body,
        ).execute()

        service.edits().commit(packageName=package_name, editId=edit_id).execute()

        return (
            f"Successfully deployed to {track_display[track]} track.\n"
            f"Version code: {version_code}\n"
            f"Status: {status}\n"
            f"Edit ID: {edit_id}"
        )

    except Exception as e:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def deploy_production(
    aab_path: str,
    release_notes_ko: str = "",
    release_notes_en: str = "",
    status: str = "completed",
) -> str:
    """Deploy an Android App Bundle to the PRODUCTION track.

    CRITICAL: You MUST ask for explicit user confirmation before calling this
    tool. Production deployment publishes the app to ALL users on Google Play
    and is difficult to reverse. Always show the user the version, release
    notes, and status before proceeding, and wait for their approval.

    Args:
        aab_path: Path to the .aab file to upload.
        release_notes_ko: Release notes in Korean (optional).
        release_notes_en: Release notes in English (optional).
        status: Release status - "completed" to publish immediately,
                "halted" to pause rollout, "inProgress" for staged rollout.
                Default is "completed".

    Returns:
        A message indicating success with version code and edit ID.
    """
    service = _get_service()
    package_name = _get_package_name()

    if not os.path.exists(aab_path):
        raise ValueError(f"AAB file not found: {aab_path}")

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        media = MediaFileUpload(aab_path, mimetype="application/octet-stream")
        bundle = service.edits().bundles().upload(
            packageName=package_name,
            editId=edit_id,
            media_body=media,
        ).execute()
        version_code = bundle["versionCode"]

        release_notes = []
        if release_notes_ko:
            release_notes.append({"language": "ko-KR", "text": release_notes_ko})
        if release_notes_en:
            release_notes.append({"language": "en-US", "text": release_notes_en})

        release = {
            "versionCodes": [str(version_code)],
            "status": status,
        }
        if release_notes:
            release["releaseNotes"] = release_notes

        track_body = {
            "track": "production",
            "releases": [release],
        }

        service.edits().tracks().update(
            packageName=package_name,
            editId=edit_id,
            track="production",
            body=track_body,
        ).execute()

        service.edits().commit(packageName=package_name, editId=edit_id).execute()

        return (
            f"Successfully deployed to PRODUCTION track.\n"
            f"Version code: {version_code}\n"
            f"Status: {status}\n"
            f"Edit ID: {edit_id}"
        )

    except Exception as e:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def create_inapp_product(
    sku: str,
    localizations: str,
    price: float,
    currency_code: str = "USD",
    purchase_option_id: str = "default",
) -> str:
    """Create or update an in-app product (managed product).

    Uses the new monetization API with automatic regional price conversion.
    If the product already exists, it will be updated.

    IMPORTANT: The app must have BILLING permission and Play Billing Library
    in an uploaded bundle before products can be created.

    Args:
        sku: Product ID (e.g., "rezi_until_exam"). Only lowercase, numbers, underscores.
        localizations: JSON array of localizations. Each entry needs "language", "title", "description".
            Example: [{"language": "en-US", "title": "Rezi - Until Exam", "description": "Full access"},
                       {"language": "ro", "title": "Rezidențiat", "description": "Acces complet"}]
        price: Price amount in the specified currency (e.g., 1670 for 1670 RON).
        currency_code: ISO currency code (e.g., "RON", "USD", "EUR", "TRY"). Default "USD".
        purchase_option_id: Purchase option ID. Default "default".

    Returns:
        A message indicating success with product details.
    """
    service = _get_service()
    package_name = _get_package_name()

    price_micros = int(price * 1_000_000)

    # Convert prices
    converted = _convert_region_prices(service, package_name, price_micros, currency_code)
    regions_version = converted["regionVersion"]["version"]

    # Build regional configs
    regional_configs = []
    for region_code, price_data in converted["convertedRegionPrices"].items():
        p = price_data["price"]
        regional_configs.append({
            "regionCode": region_code,
            "price": {
                "currencyCode": p["currencyCode"],
                "units": p.get("units", "0"),
                "nanos": p.get("nanos", 0),
            },
            "availability": "AVAILABLE",
        })

    # New regions config
    other = converted.get("convertedOtherRegionsPrice", {})
    new_regions_config = {
        "availability": "AVAILABLE",
        "usdPrice": other.get("usdPrice"),
        "eurPrice": other.get("eurPrice"),
    }

    # Listings from localizations JSON
    locales = json.loads(localizations)
    listings = [
        {"languageCode": loc["language"], "title": loc["title"], "description": loc["description"]}
        for loc in locales
    ]

    body = {
        "packageName": package_name,
        "productId": sku,
        "listings": listings,
        "purchaseOptions": [{
            "purchaseOptionId": purchase_option_id,
            "buyOption": {"legacyCompatible": True},
            "regionalPricingAndAvailabilityConfigs": regional_configs,
            "newRegionsConfig": new_regions_config,
        }],
    }

    # Patch with allowMissing=True for upsert behavior
    request = service.monetization().onetimeproducts().patch(
        packageName=package_name,
        productId=sku,
        body=body,
        allowMissing=True,
        updateMask="listings,purchaseOptions",
    )

    # Add regionsVersion.version parameter manually
    sep = "&" if "?" in request.uri else "?"
    request.uri += f"{sep}regionsVersion.version={regions_version}"

    result = request.execute()

    return (
        f"Successfully created/updated in-app product.\n"
        f"SKU: {sku}\n"
        f"Price: {price} {currency_code}\n"
        f"Localizations: {len(listings)}\n"
        f"Regions: {len(regional_configs)}\n"
        f"Status: Product is in DRAFT state. Use activate_inapp_product to activate."
    )


@mcp.tool()
def activate_inapp_product(sku: str, purchase_option_id: str = "default") -> str:
    """Activate a draft in-app product to make it available for purchase.

    Args:
        sku: Product ID to activate (e.g., "rezi_until_exam").
        purchase_option_id: Purchase option ID. Default "default".

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    service.monetization().onetimeproducts().purchaseOptions().batchUpdateStates(
        packageName=package_name,
        productId=sku,
        body={
            "requests": [{
                "activatePurchaseOptionRequest": {
                    "packageName": package_name,
                    "productId": sku,
                    "purchaseOptionId": purchase_option_id,
                }
            }]
        },
    ).execute()

    return f"Successfully activated in-app product: {sku}"


@mcp.tool()
def deactivate_inapp_product(sku: str, purchase_option_id: str = "default") -> str:
    """Deactivate an active in-app product.

    Args:
        sku: Product ID to deactivate (e.g., "rezi_until_exam").
        purchase_option_id: Purchase option ID. Default "default".

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    service.monetization().onetimeproducts().purchaseOptions().batchUpdateStates(
        packageName=package_name,
        productId=sku,
        body={
            "requests": [{
                "deactivatePurchaseOptionRequest": {
                    "packageName": package_name,
                    "productId": sku,
                    "purchaseOptionId": purchase_option_id,
                }
            }]
        },
    ).execute()

    return f"Successfully deactivated in-app product: {sku}"


@mcp.tool()
def list_inapp_products() -> str:
    """List all one-time in-app products for the app.

    Returns:
        JSON formatted list of all in-app products with their details.
    """
    service = _get_service()
    package_name = _get_package_name()

    result = service.monetization().onetimeproducts().list(
        packageName=package_name,
    ).execute()

    products = result.get("onetimeProducts", [])

    if not products:
        return "No in-app products found."

    output = [f"Found {len(products)} in-app product(s):\n"]

    for product in products:
        product_id = product.get("productId", "unknown")
        listings = product.get("listings", [])
        title = "No title"
        for listing in listings:
            if listing.get("languageCode") == "en-US":
                title = listing.get("title", title)
                break
        if title == "No title" and listings:
            title = listings[0].get("title", title)

        # Get purchase options for status
        options = product.get("purchaseOptions", [])
        status = "UNKNOWN"
        price_info = ""
        for opt in options:
            if "state" in opt:
                status = opt["state"]
            configs = opt.get("regionalPricingAndAvailabilityConfigs", [])
            for cfg in configs:
                if cfg.get("regionCode") == "US":
                    price = cfg.get("price", {})
                    units = price.get("units", "0")
                    nanos = price.get("nanos", 0)
                    price_val = int(units) + nanos / 1_000_000_000
                    price_info = f"${price_val:.2f} USD"
                    break

        output.append(f"- {product_id}: {title} ({price_info}) [{status}]")

    return "\n".join(output)


@mcp.tool()
def list_subscriptions() -> str:
    """List all subscription products for the app.

    Returns:
        JSON formatted list of all subscription products.
    """
    service = _get_service()
    package_name = _get_package_name()

    result = service.monetization().subscriptions().list(
        packageName=package_name,
    ).execute()

    subscriptions = result.get("subscriptions", [])

    if not subscriptions:
        return "No subscription products found."

    output = [f"Found {len(subscriptions)} subscription(s):\n"]

    for sub in subscriptions:
        product_id = sub.get("productId", "unknown")
        listings = sub.get("listings", [])
        title = "No title"
        for listing in listings:
            if listing.get("languageCode") == "en-US":
                title = listing.get("title", title)
                break

        output.append(f"- {product_id}: {title}")

    return "\n".join(output)


@mcp.tool()
def create_subscription(
    product_id: str,
    localizations: str,
    base_plans: str,
    tax_category: str = "SOFTWARE",
) -> str:
    """Create a subscription product with base plans.

    Creates the subscription, its base plans (in DRAFT state), and regional pricing.
    After creation, use activate_base_plan to make each base plan available.

    Args:
        product_id: Subscription product ID (e.g., "rezi"). Lowercase, numbers, underscores.
        localizations: JSON array of listings. Each needs "language", "title", and optionally "benefits" (array of up to 4 strings) and "description" (max 80 chars).
            Example: [{"language": "en-US", "title": "Rezi", "benefits": ["Full access"]},
                       {"language": "ro", "title": "Rezidențiat", "benefits": ["Acces complet"]}]
        base_plans: JSON array of base plan definitions. Each needs:
            - "id": base plan ID (e.g., "weekly", "monthly")
            - "period": ISO 8601 duration (P1W, P1M, P3M, P6M, P1Y)
            - "price": price amount in the specified currency
            - "currency_code": ISO currency code (e.g., "RON", "EUR", "TRY")
            - "legacy_compatible" (optional, bool): set true on ONE plan for older billing library compat
            Example: [{"id": "monthly", "period": "P1M", "price": 210, "currency_code": "RON", "legacy_compatible": true}]
        tax_category: Tax category. Default "SOFTWARE". Use "SOFTWARE" for digital app sales.

    Returns:
        A message indicating success with subscription details.
    """
    service = _get_service()
    package_name = _get_package_name()

    locales = json.loads(localizations)
    plans = json.loads(base_plans)

    # Build listings
    listings = []
    for loc in locales:
        listing = {"languageCode": loc["language"], "title": loc["title"]}
        if "benefits" in loc:
            listing["benefits"] = loc["benefits"]
        if "description" in loc:
            listing["description"] = loc["description"]
        listings.append(listing)

    # Build base plans with regional pricing
    base_plan_objects = []
    regions_version = None

    for plan in plans:
        price_micros = int(plan["price"] * 1_000_000)
        currency_code = plan.get("currency_code", "USD")

        converted = _convert_region_prices(service, package_name, price_micros, currency_code)
        regions_version = converted["regionVersion"]["version"]

        regional_configs = []
        for region_code, price_data in converted["convertedRegionPrices"].items():
            p = price_data["price"]
            regional_configs.append({
                "regionCode": region_code,
                "newSubscriberAvailability": True,
                "price": {
                    "currencyCode": p["currencyCode"],
                    "units": p.get("units", "0"),
                    "nanos": p.get("nanos", 0),
                },
            })

        other = converted.get("convertedOtherRegionsPrice", {})
        other_regions_config = {
            "usdPrice": other.get("usdPrice"),
            "eurPrice": other.get("eurPrice"),
            "newSubscriberAvailability": True,
        }

        base_plan = {
            "basePlanId": plan["id"],
            "regionalConfigs": regional_configs,
            "otherRegionsConfig": other_regions_config,
            "autoRenewingBasePlanType": {
                "billingPeriodDuration": plan["period"],
                "resubscribeState": "RESUBSCRIBE_STATE_ACTIVE",
            },
        }

        if plan.get("legacy_compatible"):
            base_plan["autoRenewingBasePlanType"]["legacyCompatible"] = True

        base_plan_objects.append(base_plan)

    body = {
        "packageName": package_name,
        "productId": product_id,
        "listings": listings,
        "basePlans": base_plan_objects,
        "taxAndComplianceSettings": {
            "eeaWithdrawalRightType": "EEA_WITHDRAWAL_RIGHT_TYPE_UNSPECIFIED",
            "taxRateInfoByRegionCode": {},
        },
    }

    result = service.monetization().subscriptions().create(
        packageName=package_name,
        productId=product_id,
        body=body,
        **({f"regionsVersion.version": regions_version} if regions_version else {}),
    ).execute()

    plan_ids = [p["id"] for p in plans]
    return (
        f"Successfully created subscription.\n"
        f"Product ID: {product_id}\n"
        f"Listings: {len(listings)}\n"
        f"Base plans (DRAFT): {', '.join(plan_ids)}\n"
        f"Use activate_base_plan to activate each base plan."
    )


@mcp.tool()
def update_subscription(
    product_id: str,
    localizations: str = "",
    tax_category: str = "",
) -> str:
    """Update an existing subscription's listings or settings.

    Args:
        product_id: Subscription product ID (e.g., "rezi").
        localizations: JSON array of listings to update (same format as create_subscription). Leave empty to skip.
        tax_category: Tax category to set. Leave empty to skip.

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    update_fields = []
    body = {
        "packageName": package_name,
        "productId": product_id,
    }

    if localizations:
        locales = json.loads(localizations)
        listings = []
        for loc in locales:
            listing = {"languageCode": loc["language"], "title": loc["title"]}
            if "benefits" in loc:
                listing["benefits"] = loc["benefits"]
            if "description" in loc:
                listing["description"] = loc["description"]
            listings.append(listing)
        body["listings"] = listings
        update_fields.append("listings")

    if not update_fields:
        return "Nothing to update. Provide localizations to update."

    # Get regions version
    converted = _convert_region_prices(service, package_name, 1_000_000, "USD")
    regions_version = converted["regionVersion"]["version"]

    result = service.monetization().subscriptions().patch(
        packageName=package_name,
        productId=product_id,
        body=body,
        updateMask=",".join(update_fields),
        **({f"regionsVersion.version": regions_version}),
    ).execute()

    return (
        f"Successfully updated subscription '{product_id}'.\n"
        f"Updated fields: {', '.join(update_fields)}"
    )


@mcp.tool()
def delete_subscription(product_id: str) -> str:
    """Delete a subscription product.

    WARNING: This permanently deletes the subscription. Only works if the subscription
    has never had any subscribers. Once a subscription has had subscribers, it cannot
    be deleted — only archived/deactivated.

    Args:
        product_id: Subscription product ID to delete (e.g., "rezi").

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    service.monetization().subscriptions().delete(
        packageName=package_name,
        productId=product_id,
    ).execute()

    return f"Successfully deleted subscription: {product_id}"


@mcp.tool()
def activate_base_plan(product_id: str, base_plan_id: str) -> str:
    """Activate a draft base plan to make it available for new subscribers.

    Args:
        product_id: Parent subscription product ID (e.g., "rezi").
        base_plan_id: Base plan ID to activate (e.g., "monthly", "weekly").

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    service.monetization().subscriptions().basePlans().activate(
        packageName=package_name,
        productId=product_id,
        basePlanId=base_plan_id,
        body={},
    ).execute()

    return f"Successfully activated base plan '{base_plan_id}' on subscription '{product_id}'."


@mcp.tool()
def deactivate_base_plan(product_id: str, base_plan_id: str) -> str:
    """Deactivate a base plan so it's no longer available to new subscribers.

    Existing subscribers keep their plan until it expires.

    Args:
        product_id: Parent subscription product ID (e.g., "rezi").
        base_plan_id: Base plan ID to deactivate (e.g., "monthly").

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    service.monetization().subscriptions().basePlans().deactivate(
        packageName=package_name,
        productId=product_id,
        basePlanId=base_plan_id,
        body={},
    ).execute()

    return f"Successfully deactivated base plan '{base_plan_id}' on subscription '{product_id}'."


@mcp.tool()
def create_free_trial_offer(
    product_id: str,
    base_plan_id: str,
    offer_id: str,
    free_trial_duration: str,
) -> str:
    """Create a free trial offer on a base plan.

    Creates the offer in DRAFT state. Use activate_offer to make it live.

    Args:
        product_id: Parent subscription product ID (e.g., "rezi").
        base_plan_id: Base plan ID to attach the offer to (e.g., "monthly").
        offer_id: Unique offer ID (e.g., "monthly-free-trial").
        free_trial_duration: ISO 8601 duration for the free trial (e.g., "P3D" for 3 days, "P7D" for 7 days).

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    # Get regions version
    converted = _convert_region_prices(service, package_name, 1_000_000, "USD")
    regions_version = converted["regionVersion"]["version"]

    # Build regional configs — make available in all regions
    regional_configs = []
    for region_code in converted["convertedRegionPrices"]:
        regional_configs.append({
            "regionCode": region_code,
            "newSubscriberAvailability": True,
        })

    body = {
        "packageName": package_name,
        "productId": product_id,
        "basePlanId": base_plan_id,
        "offerId": offer_id,
        "phases": [{
            "duration": free_trial_duration,
            "recurrenceCount": 1,
            "regionalConfigs": [
                {"regionCode": rc["regionCode"], "free": {}}
                for rc in regional_configs
            ],
            "otherRegionsConfig": {"otherRegionsNewSubscriberAvailability": True, "free": {}},
        }],
        "targeting": {
            "acquisitionRule": {
                "scope": {"thisSubscription": {}},
            },
        },
        "regionalConfigs": regional_configs,
        "otherRegionsConfig": {"otherRegionsNewSubscriberAvailability": True},
    }

    result = service.monetization().subscriptions().basePlans().offers().create(
        packageName=package_name,
        productId=product_id,
        basePlanId=base_plan_id,
        offerId=offer_id,
        body=body,
        **({f"regionsVersion.version": regions_version}),
    ).execute()

    return (
        f"Successfully created free trial offer.\n"
        f"Subscription: {product_id}, Base plan: {base_plan_id}\n"
        f"Offer ID: {offer_id}, Duration: {free_trial_duration}\n"
        f"Eligibility: Never had this subscription\n"
        f"Status: DRAFT — use activate_offer to make it live."
    )


@mcp.tool()
def activate_offer(product_id: str, base_plan_id: str, offer_id: str) -> str:
    """Activate a draft subscription offer.

    Args:
        product_id: Parent subscription product ID (e.g., "rezi").
        base_plan_id: Base plan ID (e.g., "monthly").
        offer_id: Offer ID to activate (e.g., "monthly-free-trial").

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    service.monetization().subscriptions().basePlans().offers().activate(
        packageName=package_name,
        productId=product_id,
        basePlanId=base_plan_id,
        offerId=offer_id,
        body={},
    ).execute()

    return f"Successfully activated offer '{offer_id}' on {product_id}/{base_plan_id}."


@mcp.tool()
def deactivate_offer(product_id: str, base_plan_id: str, offer_id: str) -> str:
    """Deactivate a subscription offer.

    Args:
        product_id: Parent subscription product ID (e.g., "rezi").
        base_plan_id: Base plan ID (e.g., "monthly").
        offer_id: Offer ID to deactivate (e.g., "monthly-free-trial").

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    service.monetization().subscriptions().basePlans().offers().deactivate(
        packageName=package_name,
        productId=product_id,
        basePlanId=base_plan_id,
        offerId=offer_id,
        body={},
    ).execute()

    return f"Successfully deactivated offer '{offer_id}' on {product_id}/{base_plan_id}."


@mcp.tool()
def get_app_info() -> str:
    """Get basic app information from Google Play.

    Returns:
        App details including current version and track information.
    """
    service = _get_service()
    package_name = _get_package_name()

    # Create a read-only edit to query app info
    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        # Get track info
        tracks_result = service.edits().tracks().list(
            packageName=package_name,
            editId=edit_id,
        ).execute()

        output = [f"Package: {package_name}\n", "Tracks:"]

        for track in tracks_result.get("tracks", []):
            track_name = track.get("track", "unknown")
            releases = track.get("releases", [])
            if releases:
                latest = releases[0]
                version_codes = latest.get("versionCodes", [])
                status = latest.get("status", "unknown")
                output.append(
                    f"  - {track_name}: version {version_codes}, status: {status}"
                )
            else:
                output.append(f"  - {track_name}: no releases")

        return "\n".join(output)

    finally:
        # Delete the edit
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass


@mcp.tool()
def batch_create_inapp_products(products_json: str) -> str:
    """Create multiple in-app products from a JSON array.

    Each product in the array should have: sku, localizations, price, currency_code.
    Optionally: purchase_option_id (default: "default").

    IMPORTANT: The app must have BILLING permission and Play Billing Library
    in an uploaded bundle before products can be created.

    Args:
        products_json: JSON array of product definitions.
            Example: [
              {"sku": "rezi_until_exam", "price": 1670, "currency_code": "RON",
               "localizations": [
                 {"language": "en-US", "title": "Rezi - Until Exam", "description": "Full access"},
                 {"language": "ro", "title": "Rezidențiat", "description": "Acces complet"}
               ]},
              ...
            ]

    Returns:
        Summary of results for each product.
    """
    products = json.loads(products_json)
    results = []

    for i, product in enumerate(products, 1):
        try:
            service = _get_service()
            package_name = _get_package_name()

            sku = product["sku"]
            prod_price = product["price"]
            prod_currency = product.get("currency_code", "USD")
            price_micros = int(prod_price * 1_000_000)

            converted = _convert_region_prices(service, package_name, price_micros, prod_currency)
            regions_version = converted["regionVersion"]["version"]

            regional_configs = []
            for region_code, price_data in converted["convertedRegionPrices"].items():
                p = price_data["price"]
                regional_configs.append({
                    "regionCode": region_code,
                    "price": {
                        "currencyCode": p["currencyCode"],
                        "units": p.get("units", "0"),
                        "nanos": p.get("nanos", 0),
                    },
                    "availability": "AVAILABLE",
                })

            other = converted.get("convertedOtherRegionsPrice", {})
            new_regions_config = {
                "availability": "AVAILABLE",
                "usdPrice": other.get("usdPrice"),
                "eurPrice": other.get("eurPrice"),
            }

            listings = [
                {"languageCode": loc["language"], "title": loc["title"], "description": loc["description"]}
                for loc in product["localizations"]
            ]

            opt_id = product.get("purchase_option_id", "default")

            body = {
                "packageName": package_name,
                "productId": sku,
                "listings": listings,
                "purchaseOptions": [{
                    "purchaseOptionId": opt_id,
                    "buyOption": {"legacyCompatible": True},
                    "regionalPricingAndAvailabilityConfigs": regional_configs,
                    "newRegionsConfig": new_regions_config,
                }],
            }

            request = service.monetization().onetimeproducts().patch(
                packageName=package_name,
                productId=sku,
                body=body,
                allowMissing=True,
                updateMask="listings,purchaseOptions",
            )
            sep = "&" if "?" in request.uri else "?"
            request.uri += f"{sep}regionsVersion.version={regions_version}"
            request.execute()

            results.append(f"[{i}/{len(products)}] OK: {sku} ({prod_price} {prod_currency})")

        except Exception as e:
            results.append(f"[{i}/{len(products)}] FAIL: {product.get('sku', 'unknown')} - {e}")

    return "\n".join(results)


@mcp.tool()
def get_store_listing(language: str = "ko-KR") -> str:
    """Get current store listing for the app.

    Args:
        language: Language code (e.g., "ko-KR", "en-US"). Default is "ko-KR".

    Returns:
        Current store listing information.
    """
    service = _get_service()
    package_name = _get_package_name()

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        listing = service.edits().listings().get(
            packageName=package_name,
            editId=edit_id,
            language=language,
        ).execute()

        return (
            f"Store Listing ({language}):\n"
            f"  Title: {listing.get('title', 'N/A')}\n"
            f"  Short Description: {listing.get('shortDescription', 'N/A')}\n"
            f"  Full Description: {listing.get('fullDescription', 'N/A')}"
        )

    except Exception as e:
        if "404" in str(e):
            return f"No store listing found for language: {language}"
        raise e

    finally:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass


@mcp.tool()
def update_store_listing(
    language: str,
    title: str = "",
    short_description: str = "",
    full_description: str = "",
) -> str:
    """Update store listing for the app.

    Args:
        language: Language code (e.g., "ko-KR", "en-US").
        title: App title (max 30 characters). Leave empty to keep current.
        short_description: Short description (max 80 characters). Leave empty to keep current.
        full_description: Full description (max 4000 characters). Leave empty to keep current.

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        # Get current listing first
        try:
            current = service.edits().listings().get(
                packageName=package_name,
                editId=edit_id,
                language=language,
            ).execute()
        except Exception:
            current = {}

        # Build update body, keeping current values if not provided
        body = {
            "language": language,
            "title": title if title else current.get("title", ""),
            "shortDescription": short_description if short_description else current.get("shortDescription", ""),
            "fullDescription": full_description if full_description else current.get("fullDescription", ""),
        }

        service.edits().listings().update(
            packageName=package_name,
            editId=edit_id,
            language=language,
            body=body,
        ).execute()

        # Commit the edit (handles draft app requirements)
        _commit_edit(service, package_name, edit_id)

        return (
            f"Successfully updated store listing for {language}.\n"
            f"  Title: {body['title']}\n"
            f"  Short Description: {body['shortDescription']}\n"
            f"  Full Description: {body['fullDescription'][:100]}..."
        )

    except Exception as e:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def upload_store_image(
    image_path: str,
    image_type: str,
    language: str = "ko-KR",
) -> str:
    """Upload an image to the store listing.

    Args:
        image_path: Path to the image file (PNG or JPEG).
        image_type: Type of image. One of:
            - "icon": App icon (512x512 PNG, 1 required)
            - "featureGraphic": Feature graphic (1024x500, 1 required)
            - "phoneScreenshots": Phone screenshot (8 required, min 320px,
              max 3840px per side, aspect ratio max 2:1,
              recommended 1080x2340 portrait)
            - "sevenInchScreenshots": 7-inch tablet screenshot (up to 8,
              recommended 1200x1920 portrait)
            - "tenInchScreenshots": 10-inch tablet screenshot (up to 8,
              recommended 1600x2560 portrait)
            - "tvBanner": TV banner (1280x720)
            - "tvScreenshots": TV screenshot
            - "wearScreenshots": Wear OS screenshot
        language: Language code (e.g., "ko-KR", "en-US"). Each language
            needs its own set of screenshots. Icon and featureGraphic are
            also per-language.

    Returns:
        A message indicating success with image details.
    """
    service = _get_service()
    package_name = _get_package_name()

    if not os.path.exists(image_path):
        raise ValueError(f"Image file not found: {image_path}")

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        # Determine mime type
        ext = os.path.splitext(image_path)[1].lower()
        if ext == ".png":
            mime_type = "image/png"
        elif ext in [".jpg", ".jpeg"]:
            mime_type = "image/jpeg"
        else:
            raise ValueError(f"Unsupported image format: {ext}. Use PNG or JPEG.")

        media = MediaFileUpload(image_path, mimetype=mime_type)

        result = service.edits().images().upload(
            packageName=package_name,
            editId=edit_id,
            language=language,
            imageType=image_type,
            media_body=media,
        ).execute()

        # Commit the edit (handles draft app requirements)
        _commit_edit(service, package_name, edit_id)

        image_info = result.get("image", {})
        return (
            f"Successfully uploaded {image_type} for {language}.\n"
            f"  ID: {image_info.get('id', 'N/A')}\n"
            f"  SHA256: {image_info.get('sha256', 'N/A')[:16]}..."
        )

    except Exception as e:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def list_store_images(language: str = "ko-KR", image_type: str = "phoneScreenshots") -> str:
    """List uploaded images for the store listing.

    Args:
        language: Language code (e.g., "ko-KR", "en-US").
        image_type: Type of image to list (e.g., "phoneScreenshots", "icon").

    Returns:
        List of uploaded images.
    """
    service = _get_service()
    package_name = _get_package_name()

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        result = service.edits().images().list(
            packageName=package_name,
            editId=edit_id,
            language=language,
            imageType=image_type,
        ).execute()

        images = result.get("images", [])

        if not images:
            return f"No {image_type} images found for {language}."

        output = [f"Found {len(images)} {image_type} image(s) for {language}:"]
        for img in images:
            output.append(f"  - ID: {img.get('id', 'N/A')}, SHA256: {img.get('sha256', 'N/A')[:16]}...")

        return "\n".join(output)

    finally:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass


@mcp.tool()
def delete_store_image(image_id: str, language: str, image_type: str) -> str:
    """Delete an image from the store listing.

    Args:
        image_id: ID of the image to delete.
        language: Language code (e.g., "ko-KR", "en-US").
        image_type: Type of image (e.g., "phoneScreenshots", "icon").

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        service.edits().images().delete(
            packageName=package_name,
            editId=edit_id,
            language=language,
            imageType=image_type,
            imageId=image_id,
        ).execute()

        # Commit the edit (handles draft app requirements)
        _commit_edit(service, package_name, edit_id)

        return f"Successfully deleted {image_type} image {image_id} for {language}."

    except Exception as e:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def batch_upload_store_images(
    directory: str,
    image_type: str,
    language: str = "ko-KR",
    clear_existing: bool = False,
) -> str:
    """Upload all images from a directory to the store listing in a single edit.

    Much faster than uploading one by one since it uses a single API edit
    transaction for all images.

    Args:
        directory: Path to the directory containing image files (PNG or JPEG).
            Files are uploaded in alphabetical order.
        image_type: Type of image. One of:
            - "icon": App icon (512x512 PNG, only 1 allowed)
            - "featureGraphic": Feature graphic (1024x500, only 1 allowed)
            - "phoneScreenshots": Phone screenshots (2-8 images, min 320px,
              max 3840px, aspect ratio max 2:1)
            - "sevenInchScreenshots": 7-inch tablet screenshots (up to 8,
              recommended 1200x1920 portrait)
            - "tenInchScreenshots": 10-inch tablet screenshots (up to 8,
              recommended 1600x2560 portrait)
            - "tvBanner": TV banner (1280x720)
            - "tvScreenshots": TV screenshots
            - "wearScreenshots": Wear OS screenshots
        language: Language code (e.g., "ko-KR", "en-US"). Each language
            needs its own set of screenshots.
        clear_existing: If True, delete all existing images of this type
            before uploading. Default is False.

    Returns:
        Summary of upload results.
    """
    import glob as glob_mod

    service = _get_service()
    package_name = _get_package_name()

    if not os.path.isdir(directory):
        raise ValueError(f"Directory not found: {directory}")

    # Find image files
    files = sorted(
        f for f in glob_mod.glob(os.path.join(directory, "*"))
        if os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg")
    )

    if not files:
        raise ValueError(f"No PNG/JPEG images found in: {directory}")

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        results = []

        # Optionally clear existing images
        if clear_existing:
            try:
                existing = service.edits().images().list(
                    packageName=package_name,
                    editId=edit_id,
                    language=language,
                    imageType=image_type,
                ).execute()
                for img in existing.get("images", []):
                    service.edits().images().delete(
                        packageName=package_name,
                        editId=edit_id,
                        language=language,
                        imageType=image_type,
                        imageId=img["id"],
                    ).execute()
                    results.append(f"Deleted existing: {img['id']}")
            except Exception:
                pass

        # Upload all images
        for i, filepath in enumerate(files, 1):
            ext = os.path.splitext(filepath)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            media = MediaFileUpload(filepath, mimetype=mime)

            result = service.edits().images().upload(
                packageName=package_name,
                editId=edit_id,
                language=language,
                imageType=image_type,
                media_body=media,
            ).execute()

            img_id = result.get("image", {}).get("id", "N/A")
            filename = os.path.basename(filepath)
            results.append(f"[{i}/{len(files)}] Uploaded: {filename} (ID: {img_id})")

        # Commit
        _commit_edit(service, package_name, edit_id)

        return "\n".join([
            f"Successfully uploaded {len(files)} {image_type} for {language}.",
            *results,
        ])

    except Exception as e:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def delete_all_store_images(language: str, image_type: str) -> str:
    """Delete all images of a given type from the store listing.

    Args:
        language: Language code (e.g., "ko-KR", "en-US").
        image_type: Type of image (e.g., "phoneScreenshots", "icon").

    Returns:
        A message indicating how many images were deleted.
    """
    service = _get_service()
    package_name = _get_package_name()

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
        result = service.edits().images().list(
            packageName=package_name,
            editId=edit_id,
            language=language,
            imageType=image_type,
        ).execute()

        images = result.get("images", [])
        if not images:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
            return f"No {image_type} images found for {language}."

        for img in images:
            service.edits().images().delete(
                packageName=package_name,
                editId=edit_id,
                language=language,
                imageType=image_type,
                imageId=img["id"],
            ).execute()

        _commit_edit(service, package_name, edit_id)

        return f"Successfully deleted {len(images)} {image_type} image(s) for {language}."

    except Exception as e:
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass
        raise e


@mcp.tool()
def batch_activate_inapp_products(skus_json: str) -> str:
    """Activate multiple in-app products.

    Args:
        skus_json: JSON array of product IDs to activate.
            Example: ["gems_12", "gems_66", "gems_136"]

    Returns:
        Summary of results for each product.
    """
    skus = json.loads(skus_json)
    service = _get_service()
    package_name = _get_package_name()
    results = []

    for i, sku in enumerate(skus, 1):
        try:
            purchase_option_id = "default"

            service.monetization().onetimeproducts().purchaseOptions().batchUpdateStates(
                packageName=package_name,
                productId=sku,
                body={
                    "requests": [{
                        "activatePurchaseOptionRequest": {
                            "packageName": package_name,
                            "productId": sku,
                            "purchaseOptionId": purchase_option_id,
                        }
                    }]
                },
            ).execute()

            results.append(f"[{i}/{len(skus)}] OK: {sku} activated")

        except Exception as e:
            results.append(f"[{i}/{len(skus)}] FAIL: {sku} - {e}")

    return "\n".join(results)


if __name__ == "__main__":
    mcp.run()
