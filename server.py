"""Google Play Developer API MCP Server.

Provides tools for managing Google Play app deployment, store listings,
in-app products, and subscriptions.

Deployment:
- deploy_internal: Upload AAB and deploy to internal testing track
- deploy_track: Upload AAB and deploy to any track
- deploy_production: Upload AAB and deploy to production (requires confirmation)
- get_app_info: Get app track information

Store Listing:
- get_store_listing: Get current store listing
- update_store_listing: Update store listing text
- upload_store_image: Upload a single image
- batch_upload_store_images: Upload all images from a directory
- list_store_images: List uploaded images
- delete_store_image: Delete a single image
- delete_all_store_images: Delete all images of a given type

In-App Products:
- create_inapp_product: Create or update a one-time product
- activate_inapp_product: Activate a draft product
- deactivate_inapp_product: Deactivate a product
- list_inapp_products: List all one-time products
- batch_create_inapp_products: Create multiple products at once
- batch_activate_inapp_products: Activate multiple products at once

Subscriptions:
- list_subscriptions: List all subscriptions
- create_subscription: Create a subscription with base plans
- update_subscription: Update subscription listings
- delete_subscription: Delete a subscription
- activate_base_plan: Activate a draft base plan
- deactivate_base_plan: Deactivate a base plan
- create_free_trial_offer: Create a free trial offer
- activate_offer: Activate a draft offer
- deactivate_offer: Deactivate an offer
"""

import json
import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("google-play")

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------

# Google Play regional price caps (whole currency units).
# The API rejects prices above these. Add more as discovered from API errors.
_REGION_MAX_PRICE_UNITS = {
    "KR": 570_000,       # KRW ₩570,000
    "CL": 350_000,       # CLP 350,000
    "VN": 12_500_000,    # VND ₫12,500,000
}

# Map currency codes to their primary region code (for price verification).
_CURRENCY_TO_REGION = {
    "RON": "RO", "EUR": "DE", "USD": "US", "TRY": "TR", "GBP": "GB",
    "PHP": "PH", "INR": "IN", "ARS": "AR", "BRL": "BR", "JPY": "JP",
    "KRW": "KR", "PLN": "PL", "CZK": "CZ", "HUF": "HU", "SEK": "SE",
    "NOK": "NO", "DKK": "DK", "CHF": "CH", "CAD": "CA", "AUD": "AU",
    "MXN": "MX", "CLP": "CL", "COP": "CO", "PEN": "PE", "EGP": "EG",
    "ZAR": "ZA", "NGN": "NG", "SAR": "SA", "AED": "AE", "ILS": "IL",
    "TWD": "TW", "HKD": "HK", "SGD": "SG", "MYR": "MY", "THB": "TH",
    "IDR": "ID", "VND": "VN", "UAH": "UA", "BGN": "BG", "HRK": "HR",
}

# US age rating tiers for in-app products and subscriptions.
_AGE_RATING_MAP = {
    "EVERYONE": "PRODUCT_AGE_RATING_TIER_EVERYONE",
    "13+": "PRODUCT_AGE_RATING_TIER_THIRTEEN_AND_ABOVE",
    "16+": "PRODUCT_AGE_RATING_TIER_SIXTEEN_AND_ABOVE",
    "18+": "PRODUCT_AGE_RATING_TIER_EIGHTEEN_AND_ABOVE",
}


def _age_rating_settings(age_rating: str) -> dict:
    """Build taxAndComplianceSettings with US age rating."""
    tier = _AGE_RATING_MAP.get(age_rating, _AGE_RATING_MAP["EVERYONE"])
    return {
        "regionalProductAgeRatingInfos": [
            {"regionCode": "US", "productAgeRatingTier": tier},
        ],
    }


def _price_to_units_nanos(price: float) -> tuple[int, int]:
    """Convert a decimal price to (units, nanos) using Decimal for precision."""
    d = Decimal(str(price))
    units = int(d)
    nanos = int((d - units) * 1_000_000_000)
    return units, nanos


def _format_money(price_obj: dict) -> str:
    """Format a Google Money object as a human-readable string."""
    if not price_obj:
        return "N/A"
    units = int(price_obj.get("units", "0"))
    nanos = price_obj.get("nanos", 0)
    currency = price_obj.get("currencyCode", "???")
    if nanos == 0:
        return f"{units:,} {currency}"
    value = units + nanos / 1_000_000_000
    return f"{value:,.2f} {currency}"


def _cap_price(region_code: str, units: int, nanos: int) -> tuple[int, int, bool]:
    """Cap price at regional maximum if exceeded. Returns (units, nanos, was_capped)."""
    max_units = _REGION_MAX_PRICE_UNITS.get(region_code)
    if max_units is not None and units > max_units:
        return max_units, 0, True
    return units, nanos, False


def _convert_prices(service, package_name: str, price: float, currency_code: str) -> dict:
    """Convert a price to all regions via the Google Play API.

    Returns dict with 'regionsVersion' and 'convertedRegionPrices'.
    """
    units, nanos = _price_to_units_nanos(price)

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

    return {
        "regionsVersion": result["regionVersion"]["version"],
        "convertedRegionPrices": result.get("convertedRegionPrices", {}),
    }


def _build_regional_configs(converted_prices: dict) -> tuple[list, list[str]]:
    """Build one-time product regional pricing configs with capping.

    Returns (regional_configs, cap_warnings).
    """
    regional_configs = []
    cap_warnings = []
    seen_regions = set()

    for region_code, price_data in converted_prices.items():
        if region_code in seen_regions:
            continue
        seen_regions.add(region_code)
        p = price_data["price"]
        units = int(p.get("units", "0"))
        nanos = p.get("nanos", 0)

        units, nanos, was_capped = _cap_price(region_code, units, nanos)
        if was_capped:
            cap_warnings.append(
                f"  {region_code}: capped to {units:,} {p['currencyCode']} "
                f"(limit: {_REGION_MAX_PRICE_UNITS[region_code]:,})"
            )

        regional_configs.append({
            "regionCode": region_code,
            "price": {
                "currencyCode": p["currencyCode"],
                "units": str(units),
                "nanos": nanos,
            },
            "availability": "AVAILABLE",
        })

    return regional_configs, cap_warnings


def _build_subscription_regional_configs(converted_prices: dict) -> tuple[list, list[str]]:
    """Build subscription regional pricing configs with capping.

    Returns (regional_configs, cap_warnings).
    """
    regional_configs = []
    cap_warnings = []
    seen_regions = set()

    for region_code, price_data in converted_prices.items():
        if region_code in seen_regions:
            continue
        seen_regions.add(region_code)
        p = price_data["price"]
        units = int(p.get("units", "0"))
        nanos = p.get("nanos", 0)

        units, nanos, was_capped = _cap_price(region_code, units, nanos)
        if was_capped:
            cap_warnings.append(
                f"  {region_code}: capped to {units:,} {p['currencyCode']} "
                f"(limit: {_REGION_MAX_PRICE_UNITS[region_code]:,})"
            )

        regional_configs.append({
            "regionCode": region_code,
            "newSubscriberAvailability": True,
            "price": {
                "currencyCode": p["currencyCode"],
                "units": str(units),
                "nanos": nanos,
            },
        })

    return regional_configs, cap_warnings


def _extract_verification_prices(result: dict, target_region: str | None) -> str:
    """Extract key prices from one-time product API result for verification."""
    lines = []
    for opt in result.get("purchaseOptions", []):
        for cfg in opt.get("regionalPricingAndAvailabilityConfigs", []):
            region = cfg.get("regionCode", "")
            price_str = _format_money(cfg.get("price", {}))
            if region == target_region:
                lines.insert(0, f"  {region} (target): {price_str}")
            elif region == "US":
                lines.append(f"  US: {price_str}")
            elif region == "DE":
                lines.append(f"  DE (EUR): {price_str}")
    return "\n".join(lines[:5])


def _extract_subscription_verification(result: dict, target_region: str | None) -> str:
    """Extract key prices from subscription API result for verification."""
    lines = []
    for plan in result.get("basePlans", []):
        plan_id = plan.get("basePlanId", "?")
        for cfg in plan.get("regionalConfigs", []):
            region = cfg.get("regionCode", "")
            price_str = _format_money(cfg.get("price", {}))
            if region == target_region:
                lines.append(f"  {plan_id} / {region} (target): {price_str}")
            elif region == "US":
                lines.append(f"  {plan_id} / US: {price_str}")
    return "\n".join(lines[:10])


# ---------------------------------------------------------------------------
# Edit helpers (for deployment/store listing)
# ---------------------------------------------------------------------------


def _commit_edit(service, package_name: str, edit_id: str):
    """Commit an edit, handling draft apps that require a track update."""
    track = service.edits().tracks().get(
        packageName=package_name,
        editId=edit_id,
        track="internal",
    ).execute()

    releases = track.get("releases", [])
    if releases:
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

    service.edits().commit(packageName=package_name, editId=edit_id).execute()


# ---------------------------------------------------------------------------
# Deployment tools
# ---------------------------------------------------------------------------


@mcp.tool()
def deploy_internal(
    aab_path: str,
    release_notes_en: str = "",
    release_notes_ko: str = "",
    status: str = "draft",
) -> str:
    """Deploy an Android App Bundle to the internal testing track.

    Args:
        aab_path: Path to the .aab file to upload.
        release_notes_en: Release notes in English (optional).
        release_notes_ko: Release notes in Korean (optional).
        status: Release status - "draft" for unpublished apps, "completed" for
                published apps. Default is "draft".

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
        if release_notes_en:
            release_notes.append({"language": "en-US", "text": release_notes_en})
        if release_notes_ko:
            release_notes.append({"language": "ko-KR", "text": release_notes_ko})

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

        service.edits().commit(packageName=package_name, editId=edit_id).execute()

        return (
            f"Successfully deployed to internal testing track.\n"
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
def deploy_track(
    aab_path: str,
    track: str = "internal",
    release_notes_en: str = "",
    release_notes_ko: str = "",
    status: str = "draft",
) -> str:
    """Deploy an Android App Bundle to any testing track.

    Args:
        aab_path: Path to the .aab file to upload.
        track: Target track - "internal", "alpha", "beta", or "production".
        release_notes_en: Release notes in English (optional).
        release_notes_ko: Release notes in Korean (optional).
        status: Release status - "draft" or "completed". Default is "draft".

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
        if release_notes_en:
            release_notes.append({"language": "en-US", "text": release_notes_en})
        if release_notes_ko:
            release_notes.append({"language": "ko-KR", "text": release_notes_ko})

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
    release_notes_en: str = "",
    release_notes_ko: str = "",
    status: str = "completed",
) -> str:
    """Deploy an Android App Bundle to the PRODUCTION track.

    CRITICAL: You MUST ask for explicit user confirmation before calling this.
    Production deployment publishes the app to ALL users on Google Play.

    Args:
        aab_path: Path to the .aab file to upload.
        release_notes_en: Release notes in English (optional).
        release_notes_ko: Release notes in Korean (optional).
        status: Release status - "completed", "halted", or "inProgress".

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
        if release_notes_en:
            release_notes.append({"language": "en-US", "text": release_notes_en})
        if release_notes_ko:
            release_notes.append({"language": "ko-KR", "text": release_notes_ko})

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
def get_app_info() -> str:
    """Get basic app information from Google Play.

    Returns:
        App details including current version and track information.
    """
    service = _get_service()
    package_name = _get_package_name()

    edit = service.edits().insert(packageName=package_name, body={}).execute()
    edit_id = edit["id"]

    try:
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
        try:
            service.edits().delete(packageName=package_name, editId=edit_id).execute()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# One-time products
# ---------------------------------------------------------------------------


@mcp.tool()
def create_inapp_product(
    sku: str,
    localizations: str,
    price: float,
    currency_code: str = "USD",
    purchase_option_id: str = "default",
    age_rating: str = "EVERYONE",
) -> str:
    """Create or update a one-time in-app product.

    Sets explicit regional prices for ALL regions (170+), with automatic
    capping for regions with price limits (e.g., KR max 570,000 KRW).
    Does NOT use newRegionsConfig to avoid derived-price validation issues.

    The price is the BUYER-FACING price (what the customer pays, including tax).
    This is the same price you would enter in the Google Play Console.
    The target region (determined from currency_code) gets this exact price;
    all other regions get Google's exchange-rate-converted equivalents.

    IMPORTANT: The app must have BILLING permission and Play Billing Library
    in an uploaded bundle before products can be created.

    Args:
        sku: Product ID (e.g., "rezi_until_exam"). Only lowercase, numbers, underscores.
        localizations: JSON array of localizations. Each entry needs "language", "title", "description".
            Example: [{"language": "en-US", "title": "Rezi - Until Exam", "description": "Full access"},
                       {"language": "ro", "title": "Rezidentiat", "description": "Acces complet"}]
        price: Buyer-facing price in the specified currency (e.g., 1998 for 1998 RON).
            This is what the customer pays (same as Google Play Console).
        currency_code: ISO currency code (e.g., "RON", "USD", "EUR", "TRY"). Default "USD".
        purchase_option_id: Purchase option ID. Default "default".
        age_rating: US age rating. One of "EVERYONE", "13+", "16+", "18+". Default "EVERYONE".

    Returns:
        Product details including key regional prices for verification.
    """
    service = _get_service()
    package_name = _get_package_name()

    # Convert to all regional prices
    info = _convert_prices(service, package_name, price, currency_code)
    regions_version = info["regionsVersion"]

    # Build explicit per-region configs with capping
    regional_configs, cap_warnings = _build_regional_configs(info["convertedRegionPrices"])

    # Override the target region with the exact user-specified price
    target_region = _CURRENCY_TO_REGION.get(currency_code)
    if target_region:
        exact_units, exact_nanos = _price_to_units_nanos(price)
        for cfg in regional_configs:
            if cfg["regionCode"] == target_region:
                cfg["price"] = {
                    "currencyCode": currency_code,
                    "units": str(exact_units),
                    "nanos": exact_nanos,
                }
                break

    # Parse localizations
    locales = json.loads(localizations)
    listings = [
        {"languageCode": loc["language"], "title": loc["title"], "description": loc["description"]}
        for loc in locales
    ]

    body = {
        "packageName": package_name,
        "productId": sku,
        "listings": listings,
        "taxAndComplianceSettings": _age_rating_settings(age_rating),
        "purchaseOptions": [{
            "purchaseOptionId": purchase_option_id,
            "buyOption": {"legacyCompatible": True},
            "regionalPricingAndAvailabilityConfigs": regional_configs,
            # No newRegionsConfig — all regions have explicit prices.
            # New regions Google adds later won't auto-get this product.
        }],
    }

    request = service.monetization().onetimeproducts().patch(
        packageName=package_name,
        productId=sku,
        body=body,
        allowMissing=True,
        updateMask="listings,purchaseOptions,taxAndComplianceSettings",
    )

    # Add regionsVersion parameter
    sep = "&" if "?" in request.uri else "?"
    request.uri += f"{sep}regionsVersion.version={regions_version}"

    result = request.execute()

    # Build verification output
    target_region = _CURRENCY_TO_REGION.get(currency_code)
    price_verification = _extract_verification_prices(result, target_region)

    output = [
        f"Successfully created/updated one-time product: {sku}",
        f"Base price: {price:,.2f} {currency_code}",
        f"Localizations: {len(listings)}",
        f"Regions: {len(regional_configs)}",
    ]

    if cap_warnings:
        output.append(f"Price caps applied ({len(cap_warnings)}):")
        output.extend(cap_warnings)

    if price_verification:
        output.append("Verification prices:")
        output.append(price_verification)

    output.append("Status: DRAFT — use activate_inapp_product to activate.")

    return "\n".join(output)


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
        List of all in-app products with their details.
    """
    service = _get_service()
    package_name = _get_package_name()

    result = service.monetization().onetimeproducts().list(
        packageName=package_name,
    ).execute()

    products = result.get("oneTimeProducts", [])

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

        options = product.get("purchaseOptions", [])
        status = "UNKNOWN"
        price_info = ""
        for opt in options:
            if "state" in opt:
                status = opt["state"]
            configs = opt.get("regionalPricingAndAvailabilityConfigs", [])
            for cfg in configs:
                if cfg.get("regionCode") == "US":
                    price_info = _format_money(cfg.get("price", {}))
                    break

        output.append(f"- {product_id}: {title} ({price_info}) [{status}]")

    return "\n".join(output)


@mcp.tool()
def batch_create_inapp_products(products_json: str) -> str:
    """Create multiple one-time in-app products from a JSON array.

    Each product needs: sku, localizations, price, currency_code.
    Optionally: purchase_option_id (default: "default"), age_rating (default: "EVERYONE").

    Args:
        products_json: JSON array of product definitions.
            Example: [
              {"sku": "rezi_until_exam", "price": 1998, "currency_code": "RON",
               "localizations": [
                 {"language": "en-US", "title": "Rezi - Until Exam", "description": "Full access"},
                 {"language": "ro", "title": "Rezidentiat", "description": "Acces complet"}
               ]}
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

            info = _convert_prices(service, package_name, prod_price, prod_currency)
            regions_version = info["regionsVersion"]

            regional_configs, cap_warnings = _build_regional_configs(info["convertedRegionPrices"])

            # Override the target region with the exact user-specified price
            target_region = _CURRENCY_TO_REGION.get(prod_currency)
            if target_region:
                exact_units, exact_nanos = _price_to_units_nanos(prod_price)
                for cfg in regional_configs:
                    if cfg["regionCode"] == target_region:
                        cfg["price"] = {
                            "currencyCode": prod_currency,
                            "units": str(exact_units),
                            "nanos": exact_nanos,
                        }
                        break

            listings = [
                {"languageCode": loc["language"], "title": loc["title"], "description": loc["description"]}
                for loc in product["localizations"]
            ]

            opt_id = product.get("purchase_option_id", "default")

            prod_age_rating = product.get("age_rating", "EVERYONE")

            body = {
                "packageName": package_name,
                "productId": sku,
                "listings": listings,
                "taxAndComplianceSettings": _age_rating_settings(prod_age_rating),
                "purchaseOptions": [{
                    "purchaseOptionId": opt_id,
                    "buyOption": {"legacyCompatible": True},
                    "regionalPricingAndAvailabilityConfigs": regional_configs,
                }],
            }

            request = service.monetization().onetimeproducts().patch(
                packageName=package_name,
                productId=sku,
                body=body,
                allowMissing=True,
                updateMask="listings,purchaseOptions,taxAndComplianceSettings",
            )
            sep = "&" if "?" in request.uri else "?"
            request.uri += f"{sep}regionsVersion.version={regions_version}"
            request.execute()

            caps = f" (capped: {len(cap_warnings)})" if cap_warnings else ""
            results.append(f"[{i}/{len(products)}] OK: {sku} ({prod_price} {prod_currency}){caps}")

        except Exception as e:
            results.append(f"[{i}/{len(products)}] FAIL: {product.get('sku', 'unknown')} - {e}")

    return "\n".join(results)


@mcp.tool()
def batch_activate_inapp_products(skus_json: str) -> str:
    """Activate multiple in-app products.

    Args:
        skus_json: JSON array of product IDs to activate.
            Example: ["rezi_until_exam", "bac_until_exam"]

    Returns:
        Summary of results for each product.
    """
    skus = json.loads(skus_json)
    service = _get_service()
    package_name = _get_package_name()
    results = []

    for i, sku in enumerate(skus, 1):
        try:
            service.monetization().onetimeproducts().purchaseOptions().batchUpdateStates(
                packageName=package_name,
                productId=sku,
                body={
                    "requests": [{
                        "activatePurchaseOptionRequest": {
                            "packageName": package_name,
                            "productId": sku,
                            "purchaseOptionId": "default",
                        }
                    }]
                },
            ).execute()

            results.append(f"[{i}/{len(skus)}] OK: {sku} activated")

        except Exception as e:
            results.append(f"[{i}/{len(skus)}] FAIL: {sku} - {e}")

    return "\n".join(results)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@mcp.tool()
def list_subscriptions() -> str:
    """List all subscription products for the app.

    Returns:
        List of all subscription products.
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
        if title == "No title" and listings:
            title = listings[0].get("title", title)

        base_plans = sub.get("basePlans", [])
        plan_ids = [p.get("basePlanId", "?") for p in base_plans]

        output.append(f"- {product_id}: {title} (plans: {', '.join(plan_ids)})")

    return "\n".join(output)


@mcp.tool()
def create_subscription(
    product_id: str,
    localizations: str,
    base_plans: str,
    tax_category: str = "SOFTWARE",
    age_rating: str = "EVERYONE",
) -> str:
    """Create a subscription product with base plans.

    Creates the subscription with base plans in DRAFT state. Sets explicit
    regional prices for all regions with automatic capping.
    Use activate_base_plan to make each base plan available.

    Args:
        product_id: Subscription product ID (e.g., "rezi"). Lowercase, numbers, underscores.
        localizations: JSON array of listings. Each needs "language", "title", and optionally
            "benefits" (array of up to 4 strings) and "description" (max 80 chars).
            Example: [{"language": "en-US", "title": "Rezi", "benefits": ["Full access"]},
                       {"language": "ro", "title": "Rezidentiat", "benefits": ["Acces complet"]}]
        base_plans: JSON array of base plan definitions. Each needs:
            - "id": base plan ID (e.g., "weekly", "monthly")
            - "period": ISO 8601 duration (P1W, P1M, P3M, P6M, P1Y)
            - "price": buyer-facing price (what the customer pays, same as Google Play Console)
            - "currency_code": ISO currency code (e.g., "RON", "EUR", "TRY")
            - "legacy_compatible" (optional, bool): set true on ONE plan
            Example: [{"id": "monthly", "period": "P1M", "price": 250, "currency_code": "RON", "legacy_compatible": true}]
        tax_category: Tax category. Default "SOFTWARE".
        age_rating: US age rating. One of "EVERYONE", "13+", "16+", "18+". Default "EVERYONE".

    Returns:
        Subscription details including prices for verification.
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
    all_cap_warnings = []

    for plan in plans:
        plan_currency = plan.get("currency_code", "USD")

        info = _convert_prices(service, package_name, plan["price"], plan_currency)
        regions_version = info["regionsVersion"]

        regional_configs, cap_warnings = _build_subscription_regional_configs(info["convertedRegionPrices"])
        all_cap_warnings.extend(cap_warnings)

        # Override the target region with the exact user-specified price
        target_region = _CURRENCY_TO_REGION.get(plan_currency)
        if target_region:
            exact_units, exact_nanos = _price_to_units_nanos(plan["price"])
            for cfg in regional_configs:
                if cfg["regionCode"] == target_region:
                    cfg["price"] = {
                        "currencyCode": plan_currency,
                        "units": str(exact_units),
                        "nanos": exact_nanos,
                    }
                    break

        base_plan = {
            "basePlanId": plan["id"],
            "regionalConfigs": regional_configs,
            # No otherRegionsConfig — all regions have explicit prices.
            "autoRenewingBasePlanType": {
                "billingPeriodDuration": plan["period"],
                "resubscribeState": "RESUBSCRIBE_STATE_ACTIVE",
            },
        }

        if plan.get("legacy_compatible"):
            base_plan["autoRenewingBasePlanType"]["legacyCompatible"] = True

        base_plan_objects.append(base_plan)

    # Create with first base plan, then patch to add additional plans
    # (the API rejects multiple base plans with overlapping regions in create)
    body = {
        "packageName": package_name,
        "productId": product_id,
        "listings": listings,
        "basePlans": [base_plan_objects[0]],
        "taxAndComplianceSettings": {
            "eeaWithdrawalRightType": "WITHDRAWAL_RIGHT_DIGITAL_CONTENT",
            "taxRateInfoByRegionCode": {},
            **_age_rating_settings(age_rating),
        },
    }

    request = service.monetization().subscriptions().create(
        packageName=package_name,
        productId=product_id,
        body=body,
    )
    if regions_version:
        sep = "&" if "?" in request.uri else "?"
        request.uri += f"{sep}regionsVersion.version={regions_version}"
    result = request.execute()

    # Add remaining base plans via patch
    if len(base_plan_objects) > 1:
        body["basePlans"] = base_plan_objects
        request = service.monetization().subscriptions().patch(
            packageName=package_name,
            productId=product_id,
            body=body,
            updateMask="basePlans",
        )
        sep = "&" if "?" in request.uri else "?"
        request.uri += f"{sep}regionsVersion.version={regions_version}"
        result = request.execute()

    plan_summaries = [f"{p['id']} ({p['price']} {p.get('currency_code', 'USD')}, {p['period']})" for p in plans]

    output = [
        f"Successfully created subscription: {product_id}",
        f"Listings: {len(listings)}",
        f"Base plans (DRAFT): {', '.join(plan_summaries)}",
    ]

    if all_cap_warnings:
        output.append(f"Price caps applied ({len(all_cap_warnings)}):")
        output.extend(all_cap_warnings)

    target_region = _CURRENCY_TO_REGION.get(plans[0].get("currency_code", "USD"))
    verification = _extract_subscription_verification(result, target_region)
    if verification:
        output.append("Verification prices:")
        output.append(verification)

    output.append("Use activate_base_plan to activate each base plan.")

    return "\n".join(output)


@mcp.tool()
def update_subscription(
    product_id: str,
    localizations: str = "",
) -> str:
    """Update an existing subscription's listings.

    Args:
        product_id: Subscription product ID (e.g., "rezi").
        localizations: JSON array of listings to update (same format as create_subscription).

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    if not localizations:
        return "Nothing to update. Provide localizations to update."

    locales = json.loads(localizations)
    listings = []
    for loc in locales:
        listing = {"languageCode": loc["language"], "title": loc["title"]}
        if "benefits" in loc:
            listing["benefits"] = loc["benefits"]
        if "description" in loc:
            listing["description"] = loc["description"]
        listings.append(listing)

    body = {
        "packageName": package_name,
        "productId": product_id,
        "listings": listings,
    }

    # Get regions version (required for patch)
    info = _convert_prices(service, package_name, 1.0, "USD")
    regions_version = info["regionsVersion"]

    request = service.monetization().subscriptions().patch(
        packageName=package_name,
        productId=product_id,
        body=body,
        updateMask="listings",
    )
    sep = "&" if "?" in request.uri else "?"
    request.uri += f"{sep}regionsVersion.version={regions_version}"
    request.execute()

    return f"Successfully updated subscription '{product_id}' listings ({len(listings)} locales)."


@mcp.tool()
def add_base_plan_to_subscription(
    product_id: str,
    base_plan_id: str,
    period: str,
    price: float,
    currency_code: str = "USD",
) -> str:
    """Add a new base plan to an existing subscription.

    Creates the base plan in DRAFT state with regional pricing for all regions.
    Use activate_base_plan to make it available for new subscribers.

    Args:
        product_id: Subscription product ID (e.g., "medschool").
        base_plan_id: New base plan ID (e.g., "annual").
        period: ISO 8601 duration (P1W, P1M, P3M, P6M, P1Y).
        price: Buyer-facing price (what the customer pays, same as Google Play Console).
        currency_code: ISO currency code (e.g., "RON", "EUR", "TRY"). Default "USD".

    Returns:
        Success info with price verification, cap warnings, and DRAFT status note.
    """
    service = _get_service()
    package_name = _get_package_name()

    # 1. Get the existing subscription to preserve its base plans
    existing = service.monetization().subscriptions().get(
        packageName=package_name,
        productId=product_id,
    ).execute()

    existing_plans = existing.get("basePlans", [])

    # 2. Convert the price to all regional prices
    info = _convert_prices(service, package_name, price, currency_code)
    regions_version = info["regionsVersion"]

    # 3. Build regional configs with capping
    regional_configs, cap_warnings = _build_subscription_regional_configs(info["convertedRegionPrices"])

    # 4. Override target region with exact price
    target_region = _CURRENCY_TO_REGION.get(currency_code)
    if target_region:
        exact_units, exact_nanos = _price_to_units_nanos(price)
        for cfg in regional_configs:
            if cfg["regionCode"] == target_region:
                cfg["price"] = {
                    "currencyCode": currency_code,
                    "units": str(exact_units),
                    "nanos": exact_nanos,
                }
                break

    # 5. Build the new base plan object
    new_plan = {
        "basePlanId": base_plan_id,
        "regionalConfigs": regional_configs,
        "autoRenewingBasePlanType": {
            "billingPeriodDuration": period,
            "resubscribeState": "RESUBSCRIBE_STATE_ACTIVE",
        },
    }

    # 6. Patch the subscription with existing plans + new plan
    body = {
        "packageName": package_name,
        "productId": product_id,
        "basePlans": existing_plans + [new_plan],
    }

    request = service.monetization().subscriptions().patch(
        packageName=package_name,
        productId=product_id,
        body=body,
        updateMask="basePlans",
    )
    sep = "&" if "?" in request.uri else "?"
    request.uri += f"{sep}regionsVersion.version={regions_version}"
    result = request.execute()

    # Build output
    output = [
        f"Successfully added base plan '{base_plan_id}' to subscription '{product_id}'.",
        f"Period: {period}",
        f"Price: {price:,.2f} {currency_code}",
        f"Regions: {len(regional_configs)}",
    ]

    if cap_warnings:
        output.append(f"Price caps applied ({len(cap_warnings)}):")
        output.extend(cap_warnings)

    verification = _extract_subscription_verification(result, target_region)
    if verification:
        output.append("Verification prices:")
        output.append(verification)

    output.append("Status: DRAFT — use activate_base_plan to activate.")

    return "\n".join(output)


@mcp.tool()
def delete_subscription(product_id: str) -> str:
    """Delete a subscription product.

    WARNING: Only works if the subscription has never had any subscribers.

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
    The trial is available in all regions where the base plan is available.

    Args:
        product_id: Parent subscription product ID (e.g., "rezi").
        base_plan_id: Base plan ID to attach the offer to (e.g., "monthly").
        offer_id: Unique offer ID (e.g., "monthly-free-trial").
        free_trial_duration: ISO 8601 duration (e.g., "P3D" for 3 days, "P7D" for 7 days).

    Returns:
        A message indicating success.
    """
    service = _get_service()
    package_name = _get_package_name()

    # Get all region codes from a price conversion call
    info = _convert_prices(service, package_name, 1.0, "USD")
    regions_version = info["regionsVersion"]
    all_regions = list(info["convertedRegionPrices"].keys())

    body = {
        "packageName": package_name,
        "productId": product_id,
        "basePlanId": base_plan_id,
        "offerId": offer_id,
        "phases": [{
            "duration": free_trial_duration,
            "recurrenceCount": 1,
            "regionalConfigs": [
                {"regionCode": rc, "free": {}}
                for rc in all_regions
            ],
        }],
        "targeting": {
            "acquisitionRule": {
                "scope": {"thisSubscription": {}},
            },
        },
        "regionalConfigs": [
            {"regionCode": rc, "newSubscriberAvailability": True}
            for rc in all_regions
        ],
    }

    request = service.monetization().subscriptions().basePlans().offers().create(
        packageName=package_name,
        productId=product_id,
        basePlanId=base_plan_id,
        offerId=offer_id,
        body=body,
    )
    sep = "&" if "?" in request.uri else "?"
    request.uri += f"{sep}regionsVersion.version={regions_version}"
    request.execute()

    return (
        f"Successfully created free trial offer.\n"
        f"Subscription: {product_id}, Base plan: {base_plan_id}\n"
        f"Offer ID: {offer_id}, Duration: {free_trial_duration}\n"
        f"Regions: {len(all_regions)}\n"
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


# ---------------------------------------------------------------------------
# Store listing
# ---------------------------------------------------------------------------


@mcp.tool()
def get_store_listing(language: str = "en-US") -> str:
    """Get current store listing for the app.

    Args:
        language: Language code (e.g., "en-US", "ro", "tr"). Default is "en-US".

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
        language: Language code (e.g., "en-US", "ro", "tr").
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
        try:
            current = service.edits().listings().get(
                packageName=package_name,
                editId=edit_id,
                language=language,
            ).execute()
        except Exception:
            current = {}

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
    language: str = "en-US",
) -> str:
    """Upload an image to the store listing.

    Args:
        image_path: Path to the image file (PNG or JPEG).
        image_type: Type of image: "icon", "featureGraphic", "phoneScreenshots",
            "sevenInchScreenshots", "tenInchScreenshots", "tvBanner",
            "tvScreenshots", or "wearScreenshots".
        language: Language code (e.g., "en-US", "ro"). Default is "en-US".

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
def list_store_images(language: str = "en-US", image_type: str = "phoneScreenshots") -> str:
    """List uploaded images for the store listing.

    Args:
        language: Language code (e.g., "en-US", "ro"). Default is "en-US".
        image_type: Type of image to list. Default is "phoneScreenshots".

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
        language: Language code (e.g., "en-US", "ro").
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
    language: str = "en-US",
    clear_existing: bool = False,
) -> str:
    """Upload all images from a directory to the store listing in a single edit.

    Args:
        directory: Path to the directory containing image files (PNG or JPEG).
        image_type: Type of image (e.g., "phoneScreenshots", "icon").
        language: Language code (e.g., "en-US", "ro"). Default is "en-US".
        clear_existing: If True, delete all existing images of this type first.

    Returns:
        Summary of upload results.
    """
    import glob as glob_mod

    service = _get_service()
    package_name = _get_package_name()

    if not os.path.isdir(directory):
        raise ValueError(f"Directory not found: {directory}")

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
        language: Language code (e.g., "en-US", "ro").
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


if __name__ == "__main__":
    mcp.run()
