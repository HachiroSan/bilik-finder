from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastmcp import Client

import mcp_server
import mudah


def sample_ad(
    listing_id: int,
    *,
    title: str = "Room near rail",
    price: int = 700,
    company: bool = False,
) -> dict:
    return {
        "type": "ads",
        "id": listing_id,
        "links": {"imageGridBaseurl": "https://img.example/grids"},
        "attributes": {
            "listId": listing_id,
            "adId": listing_id + 1000,
            "subject": title,
            "monthlyRent": price,
            "priceLabel": f"RM {price}",
            "locationLabel": "Cheras, Kuala Lumpur",
            "regionName": "Kuala Lumpur",
            "subareaName": "Cheras",
            "propertyTypeName": "Room",
            "categoryName": "Room",
            "size": "120",
            "companyAd": company,
            "nameLabel": "Example Agent" if company else "Private Advertiser",
            "storeVerified": "verified" if company else "unverified",
            "phone": "0123456789",
            "phoneHidden": False,
            "listTs": 1_790_000_000,
            "modifiedTs": 1_790_000_100,
            "adviewUrl": f"https://www.mudah.my/example-{listing_id}.htm",
            "image": "/12/example.jpg",
            "imageCount": 3,
            "agentData": {
                "section": {"buttons": [{"type": "wa", "text": "WHATSAPP"}]}
            },
        },
    }


def rsc_html(store: dict, query: dict | None = None) -> str:
    flight = (
        '16:["$","$L23",null,{"initialQuery":'
        + json.dumps(query or {}, separators=(",", ":"))
        + ',"initialStore":'
        + json.dumps(store, separators=(",", ":"))
        + "}]"
    )
    return f"<html><script>self.__next_f.push([1,{json.dumps(flight)}])</script></html>"


class ParserTests(unittest.TestCase):
    def test_current_rsc_store_is_parsed(self):
        store = {"ads": [sample_ad(10)], "featuredAds": [], "meta": {}}
        parsed = mudah.parse_search_html(rsc_html(store, {"category": "2100"}))
        self.assertEqual(parsed["source_format"], "next_rsc")
        self.assertEqual(parsed["initial_query"], {"category": "2100"})
        self.assertEqual(parsed["store"]["ads"][0]["attributes"]["listId"], 10)

    def test_legacy_next_data_fallback_is_parsed(self):
        store = {"ads": [sample_ad(11)], "featuredAds": [], "meta": {}}
        payload = {
            "props": {
                "pageProps": {
                    "initialQuery": {"category": "2100"},
                    "initialStore": store,
                }
            }
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script>"
        )
        parsed = mudah.parse_search_html(html)
        self.assertEqual(parsed["source_format"], "next_data")
        self.assertEqual(parsed["store"]["ads"][0]["attributes"]["listId"], 11)

    def test_challenge_page_raises_specific_error(self):
        with self.assertRaises(mudah.MudahBlockedError):
            mudah.parse_search_html("<html>Verify you are human captcha</html>")

    def test_network_is_permission_gated(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(mudah.MudahPermissionError):
                mudah.request_html("/malaysia/rooms-for-rent")


class SearchTests(unittest.TestCase):
    def test_request_builder_maps_all_supported_filters(self):
        path, params = mudah.build_search_request(
            location="kuala-lumpur-cheras",
            rental_type="property",
            query="mrt",
            min_price=1000,
            max_price=2000,
            bedrooms=2,
            bathrooms=1,
            min_sqft=500,
            max_sqft=1000,
            furnishing="fully",
            page=2,
            sort_by="price_asc",
        )
        self.assertEqual(path, "/kuala-lumpur-cheras/properties-for-rent")
        self.assertEqual(
            params,
            {
                "q": "mrt",
                "bathroom_id": 1,
                "furnished_id": "1",
                "monthly_rent": "1000-2000",
                "rooms_id": 2,
                "size": "500-1000",
                "sortby": "price_asc",
                "o": 2,
            },
        )

    def test_open_ended_ranges_use_mudah_format(self):
        _, maximum = mudah.build_search_request(max_price=800)
        _, minimum = mudah.build_search_request(min_price=1500)
        self.assertEqual(maximum["monthly_rent"], "-800")
        self.assertEqual(minimum["monthly_rent"], "1500-")

    def test_normalizer_extracts_public_listing_fields(self):
        record = mudah.normalize_ad(sample_ad(12), featured=True)
        self.assertEqual(record["id"], "12")
        self.assertEqual(record["price"], 700)
        self.assertEqual(record["space_sqft"], 120)
        self.assertEqual(record["seller_type"], "private")
        self.assertEqual(record["contact_phone"], "0123456789")
        self.assertTrue(record["whatsapp"])
        self.assertTrue(record["featured"])
        self.assertEqual(record["cover_image_url"], "https://img.example/grids/12/example.jpg")

    def test_search_filters_seller_locally_and_deduplicates(self):
        private = sample_ad(20, company=False)
        agent = sample_ad(21, company=True)
        store = {
            "ads": [private, agent],
            "featuredAds": [private],
            "meta": {"totalResults": 81, "totalShowing": 2},
        }
        with patch.object(
            mudah,
            "request_html",
            return_value=(rsc_html(store), "https://www.mudah.my/final"),
        ):
            result = mudah.search_rentals(
                location="kuala-lumpur",
                seller_type="private",
                include_featured=True,
            )
        self.assertEqual([item["id"] for item in result["items"]], ["20"])
        self.assertEqual(result["pagination"]["server_total_pages"], 3)
        self.assertTrue(result["pagination"]["seller_filter_local"])

    def test_location_search_returns_state_area_slug(self):
        directory = [
            {
                "type": "AREA",
                "id": 1,
                "name": "Cheras",
                "display_name": "Cheras, Kuala Lumpur",
                "state": "Kuala Lumpur",
                "state_slug": "kuala-lumpur",
                "area": "Cheras",
                "area_slug": "cheras",
                "location_slug": "kuala-lumpur-cheras",
            }
        ]
        with patch.object(mudah, "location_directory", return_value=directory):
            results = mudah.search_locations("cheras", state="kuala-lumpur")
        self.assertEqual(results[0]["location_slug"], "kuala-lumpur-cheras")
        self.assertTrue(results[0]["url"].endswith("/rooms-for-rent"))


class WatchTests(unittest.TestCase):
    def test_watch_seeds_then_reports_only_unseen_ids(self):
        current = [mudah.normalize_ad(sample_ad(30))]

        def fake_search(**_kwargs):
            return {
                "items": list(current),
                "pagination": {
                    "has_next": False,
                    "organic_items_on_page": len(current),
                },
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "watch.db")
            with (
                patch.dict(os.environ, {"MUDAH_MCP_DB": db_path}),
                patch.object(mcp_server.mudah, "search_rentals", side_effect=fake_search),
            ):
                seeded = mcp_server.check_new_rentals(
                    profile="test", location="kuala-lumpur", max_pages=1
                )
                self.assertEqual(seeded["status"], "seeded")
                self.assertEqual(seeded["seeded_count"], 1)

                unchanged = mcp_server.check_new_rentals(
                    profile="test", location="kuala-lumpur", max_pages=1
                )
                self.assertEqual(unchanged["new_count"], 0)

                current.append(mudah.normalize_ad(sample_ad(31)))
                changed = mcp_server.check_new_rentals(
                    profile="test", location="kuala-lumpur", max_pages=1
                )
                self.assertEqual(changed["new_count"], 1)
                self.assertEqual(changed["new_items"][0]["id"], "31")

                reset = mcp_server.reset_rental_watch(profile="test")
                self.assertTrue(reset["reset"])
                self.assertEqual(reset["deleted_seen"], 2)


class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tools_and_search_result(self):
        fake_result = {
            "items": [mudah.normalize_ad(sample_ad(40))],
            "pagination": {
                "page": 1,
                "site_page_size": 40,
                "server_total_items": 1,
                "server_total_pages": 1,
                "has_next": False,
                "organic_items_on_page": 1,
                "featured_items_on_page": 0,
                "matched_items_on_page": 1,
                "seller_filter_local": False,
            },
            "request_url": "https://www.mudah.my/kuala-lumpur/rooms-for-rent",
            "initial_query": {"category": "2100", "region": "9"},
            "source_format": "next_rsc",
        }
        with patch.object(
            mcp_server.mudah, "search_rentals", return_value=fake_result
        ):
            async with Client(mcp_server.mcp) as client:
                names = {tool.name for tool in await client.list_tools()}
                self.assertEqual(
                    names,
                    {
                        "search_rentals",
                        "search_locations",
                        "check_new_rentals",
                        "reset_rental_watch",
                    },
                )
                result = await client.call_tool(
                    "search_rentals",
                    {"location": "kuala-lumpur", "limit": 1},
                )
        self.assertEqual(result.data["items"][0]["id"], "40")
        self.assertEqual(result.data["pagination"]["returned"], 1)

    async def test_stdio_subprocess_lists_tools(self):
        config = {
            "mcpServers": {
                "mudah": {
                    "command": "py",
                    "args": [str(Path(__file__).with_name("mcp_server.py"))],
                }
            }
        }
        async with Client(config) as client:
            names = {tool.name for tool in await client.list_tools()}
        self.assertEqual(
            names,
            {
                "search_rentals",
                "search_locations",
                "check_new_rentals",
                "reset_rental_watch",
            },
        )


if __name__ == "__main__":
    unittest.main()
