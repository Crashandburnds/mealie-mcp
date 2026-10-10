"""Synthetic HelloFresh pages shaped like the real site (schema.org JSON-LD + __NEXT_DATA__)."""

import json

from .conftest import BREAST_ID, BREAST_URL, HF, THIGH_ID, THIGH_URL


def recipe_page(rid: str, name: str, headline: str) -> str:
    ld = {
        "@context": "http://schema.org",
        "@type": "Recipe",
        "name": name,
        "description": f"{name} {headline}",
        "image": f"https://img.hellofresh.com/{rid}.jpg",
        "recipeYield": 2,
        "totalTime": "PT40M",
        "prepTime": "PT40M",
        "recipeIngredient": ["450 grams Potatoes", "1 teaspoon White Cumin Seeds", "260 grams Chicken Thighs"],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": "Preheat your oven to 220°C."},
            {"@type": "HowToStep", "text": "Roast the potatoes."},
        ],
        "nutrition": {"@type": "NutritionInformation", "calories": "885 kcal", "proteinContent": "49.8 g"},
    }
    nd = {"props": {"pageProps": {"ssrPayload": {"recipe": {"id": rid, "name": name, "headline": headline}}}}}
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></body></html>'
    )


THIGH_PAGE = recipe_page(THIGH_ID, "Curried Chicken Thigh Traybake", "with Cumin Roast Potatoes, Carrots and Green Beans")
BREAST_PAGE = recipe_page(BREAST_ID, "Curried Chicken Breast Traybake", "with Cumin Roast Potatoes, Carrots and Green Beans")

HOME_PAGE = (
    '<html><script id="__NEXT_DATA__" type="application/json">'
    + json.dumps({"props": {"pageProps": {"ssrPayload": {"serverAuth": {"access_token": "anon-hf-token", "expires_in": 3600}}}}})
    + "</script></html>"
)

SEARCH_API = {
    "items": [
        {
            "id": BREAST_ID,
            "name": "Curried Chicken Breast Traybake",
            "headline": "with Cumin Roast Potatoes, Carrots and Green Beans",
            "slug": "curried-chicken-breast-traybake",
            "websiteUrl": BREAST_URL,
            "totalTime": "PT40M",
        },
        {
            "id": THIGH_ID,
            "name": "Curried Chicken Thigh Traybake",
            "headline": "with Cumin Roast Potatoes, Carrots and Green Beans",
            "slug": "curried-chicken-thigh-traybake",
            "websiteUrl": THIGH_URL,
            "totalTime": "PT40M",
        },
    ]
}

SEARCH_HTML = (
    f'<html><a href="/recipes/curried-chicken-thigh-traybake-{THIGH_ID}">Thigh</a>'
    f'<a href="{HF}/recipes/curried-chicken-breast-traybake-{BREAST_ID}">Breast</a></html>'
)


# Shapes observed on the live site (2026-10-10): the page's recipe name repeats the headline
# after a double space, and search lists re-runs under new ids that all link to one page.
LIVE_THIGH_PAGE = recipe_page(
    THIGH_ID,
    "Curried Chicken Thigh Traybake  with Cumin Roast Potatoes, Carrots and Green Beans",
    "with Cumin Roast Potatoes, Carrots and Green Beans",
)
LIVE_SEARCH_API = {
    "items": [
        {
            "id": rid,
            "name": "Curried Chicken Thigh Traybake",
            "headline": "with Cumin Roast Potatoes, Carrots and Green Beans",
            "slug": "curried-chicken-thigh-traybake",
            "websiteUrl": THIGH_URL,
            "totalTime": "PT40M",
        }
        for rid in ("6a4538ec66926770e8cd4e57", "6a3c0228bcb653703ca89dfa", "6a33d34b66e2e6a02a0c6b4e")
    ]
}
