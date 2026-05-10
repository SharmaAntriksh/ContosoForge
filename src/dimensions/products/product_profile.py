from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.exceptions import ValidationError


# ---------------------------------------------------------------------
# Deterministic hashing utilities (no row-order dependence)
# ---------------------------------------------------------------------
_U64 = np.uint64
_MASK64 = _U64(0xFFFFFFFFFFFFFFFF)


def _splitmix64(x: np.ndarray) -> np.ndarray:
    """Vectorized splitmix64 hash; returns uint64 array."""
    z = (x + _U64(0x9E3779B97F4A7C15)) & _MASK64
    z = (z ^ (z >> _U64(30))) * _U64(0xBF58476D1CE4E5B9) & _MASK64
    z = (z ^ (z >> _U64(27))) * _U64(0x94D049BB133111EB) & _MASK64
    return (z ^ (z >> _U64(31))) & _MASK64


def _u01(u: np.ndarray) -> np.ndarray:
    """Map uint64 -> float64 in [0,1)."""
    return ((u >> _U64(11)).astype(np.float64)) * (1.0 / (1 << 53))


def _base_uniform(uniq_base_u64: np.ndarray, seed: int, salt: int) -> np.ndarray:
    s = _U64(int(seed) ^ int(salt))
    return _u01(_splitmix64(uniq_base_u64 ^ s))



# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Attribute enrichment (merch, channel, logistics, quality)
# ---------------------------------------------------------------------
_COLOR_FAMILY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("black",), "Black"),
    (("white", "ivory", "cream"), "White"),
    (("gray", "grey", "silver"), "Gray"),
    (("blue", "navy", "cyan", "teal", "turquoise"), "Blue"),
    (("red", "maroon", "crimson"), "Red"),
    (("green", "olive", "lime"), "Green"),
    (("yellow", "gold"), "Yellow"),
    (("orange",), "Orange"),
    (("purple", "violet", "lavender"), "Purple"),
    (("pink", "magenta"), "Pink"),
    (("brown", "tan", "beige"), "Brown"),
    (("metal", "chrome", "steel"), "Metallic"),
]


# ---------------------------------------------------------------------
# Subcategory archetypes — drive Material, Style, ProductLine, Weight,
# Dimensions from realistic per-subcategory profiles so columns are
# coherent with each other.
#
# Keys: mat = materials, mw = material weights, wt = weight-kg range,
#        sz = size class, sty = styles, ln = product lines,
#        season = seasonal bias (optional), digital = no physical form
# ---------------------------------------------------------------------
_SUBCATEGORY_ARCHETYPES: dict[str, dict] = {
    # ---- Audio (Category 1) ----
    "MP4&MP3":              {"mat": ["Plastic", "Aluminum"],                 "mw": [0.60, 0.40], "wt": (0.05, 0.30),   "sz": "xs", "sty": ["Modern", "Minimalist", "Sport"],                   "ln": ["Core", "Essentials", "Active"]},
    "Recorder":             {"mat": ["Plastic", "Aluminum"],                 "mw": [0.65, 0.35], "wt": (0.10, 0.50),   "sz": "xs", "sty": ["Classic", "Modern"],                               "ln": ["Core", "Essentials", "Pro"]},
    "Radio":                {"mat": ["Plastic", "Wood"],                     "mw": [0.60, 0.40], "wt": (0.30, 3.00),   "sz": "sm", "sty": ["Classic", "Vintage", "Modern"],                    "ln": ["Classic", "Essentials"]},
    "Recording Pen":        {"mat": ["Plastic", "Aluminum"],                 "mw": [0.50, 0.50], "wt": (0.02, 0.10),   "sz": "xs", "sty": ["Modern", "Minimalist"],                            "ln": ["Core", "Essentials", "Pro"]},
    "Headphones":           {"mat": ["Plastic", "Aluminum", "Leather"],      "mw": [0.50, 0.30, 0.20], "wt": (0.10, 0.50), "sz": "sm", "sty": ["Sport", "Modern", "Minimalist"],               "ln": ["Core", "Pro", "Active"]},
    "Bluetooth Headphones": {"mat": ["Plastic", "Aluminum"],                 "mw": [0.55, 0.45], "wt": (0.05, 0.35),   "sz": "sm", "sty": ["Sport", "Modern", "Minimalist"],                   "ln": ["Core", "Pro", "Active"]},
    "Speakers":             {"mat": ["Plastic", "Wood", "Aluminum"],         "mw": [0.45, 0.35, 0.20], "wt": (0.50, 15.0), "sz": "sm", "sty": ["Modern", "Classic", "Premium"],                "ln": ["Core", "Pro", "Studio"]},
    "Audio Accessories":    {"mat": ["Plastic", "Rubber", "Nylon"],          "mw": [0.50, 0.30, 0.20], "wt": (0.02, 0.50), "sz": "xs", "sty": ["Classic", "Modern"],                           "ln": ["Core", "Essentials"]},
    # ---- TV and Video (Category 2) ----
    "Televisions":          {"mat": ["Plastic", "Glass", "Aluminum"],        "mw": [0.45, 0.30, 0.25], "wt": (5.0, 35.0),  "sz": "lg", "sty": ["Modern", "Minimalist"],                       "ln": ["Core", "Pro", "Elite"]},
    "VCD & DVD":            {"mat": ["Plastic", "Aluminum"],                 "mw": [0.70, 0.30], "wt": (1.0, 4.0),    "sz": "sm", "sty": ["Classic", "Modern"],                               "ln": ["Core", "Essentials"]},
    "Home Theater System":  {"mat": ["Plastic", "Wood"],                     "mw": [0.50, 0.50], "wt": (5.0, 25.0),   "sz": "md", "sty": ["Modern", "Classic", "Premium"],                    "ln": ["Core", "Pro", "Elite"]},
    "Car Video":            {"mat": ["Plastic", "Aluminum"],                 "mw": [0.60, 0.40], "wt": (0.50, 3.0),   "sz": "sm", "sty": ["Modern", "Sport"],                                 "ln": ["Core", "Pro"]},
    "TV & Video Accessories": {"mat": ["Plastic", "Rubber"],                 "mw": [0.65, 0.35], "wt": (0.05, 1.0),   "sz": "xs", "sty": ["Modern", "Classic"],                               "ln": ["Core", "Essentials"]},
    # ---- Computers (Category 3) ----
    "Laptops":              {"mat": ["Aluminum", "Plastic"],                 "mw": [0.55, 0.45], "wt": (1.0, 3.5),    "sz": "md", "dims": {"L": (30, 40), "W": (20, 28), "H": (1.5, 3.0)}, "sty": ["Modern", "Minimalist", "Premium"], "ln": ["Core", "Pro", "Elite"]},
    "Netbooks":             {"mat": ["Plastic", "Aluminum"],                 "mw": [0.60, 0.40], "wt": (0.80, 2.0),   "sz": "sm", "dims": {"L": (24, 32), "W": (16, 24), "H": (1.5, 2.5)}, "sty": ["Modern", "Minimalist"],            "ln": ["Core", "Essentials"]},
    "Desktops":             {"mat": ["Steel", "Plastic", "Aluminum"],        "mw": [0.45, 0.35, 0.20], "wt": (5.0, 15.0), "sz": "md", "sty": ["Modern", "Classic"],                           "ln": ["Core", "Pro", "Elite"]},
    "Monitors":             {"mat": ["Plastic", "Aluminum"],                 "mw": [0.55, 0.45], "wt": (3.0, 12.0),   "sz": "md", "dims": {"L": (50, 80), "W": (5, 12), "H": (35, 55)},     "sty": ["Modern", "Minimalist"],            "ln": ["Core", "Pro", "Elite"]},
    "Projectors & Screens": {"mat": ["Plastic", "Aluminum"],                 "mw": [0.60, 0.40], "wt": (2.0, 8.0),    "sz": "md", "sty": ["Modern", "Classic"],                               "ln": ["Core", "Pro"]},
    "Printers, Scanners & Fax": {"mat": ["Plastic", "Steel"],               "mw": [0.70, 0.30], "wt": (3.0, 15.0),   "sz": "md", "sty": ["Classic", "Modern"],                               "ln": ["Core", "Pro", "Essentials"]},
    "Computer Setup & Service": {"mat": ["Plastic"],                         "mw": [1.0],        "wt": (0.10, 1.0),   "sz": "xs", "sty": ["Classic"],                                         "ln": ["Core", "Essentials"]},
    "Computers Accessories": {"mat": ["Plastic", "Rubber", "Aluminum"],      "mw": [0.50, 0.25, 0.25], "wt": (0.05, 1.0), "sz": "xs", "sty": ["Modern", "Classic"],                           "ln": ["Core", "Essentials"]},
    # ---- Cameras and camcorders (Category 4) ----
    "Digital Cameras":      {"mat": ["Aluminum", "Plastic"],                 "mw": [0.55, 0.45], "wt": (0.15, 0.50),  "sz": "sm", "sty": ["Modern", "Classic", "Premium"],                    "ln": ["Core", "Pro", "Elite"]},
    "Digital SLR Cameras":  {"mat": ["Aluminum", "Plastic"],                 "mw": [0.60, 0.40], "wt": (0.50, 1.50),  "sz": "sm", "sty": ["Premium", "Classic", "Modern"],                    "ln": ["Pro", "Elite", "Studio"]},
    "Film Cameras":         {"mat": ["Aluminum", "Plastic"],                 "mw": [0.60, 0.40], "wt": (0.30, 1.00),  "sz": "sm", "sty": ["Vintage", "Classic"],                              "ln": ["Classic", "Studio"]},
    "Camcorders":           {"mat": ["Plastic", "Aluminum"],                 "mw": [0.55, 0.45], "wt": (0.30, 1.00),  "sz": "sm", "sty": ["Modern", "Classic"],                               "ln": ["Core", "Pro", "Studio"]},
    "Cameras & Camcorders Accessories": {"mat": ["Plastic", "Rubber", "Nylon"], "mw": [0.45, 0.30, 0.25], "wt": (0.02, 0.50), "sz": "xs", "sty": ["Classic", "Modern"],                      "ln": ["Core", "Essentials"]},
    # ---- Cell phones (Category 5) ----
    "Home & Office Phones": {"mat": ["Plastic"],                             "mw": [1.0],        "wt": (0.20, 1.00),  "sz": "sm", "sty": ["Classic", "Modern"],                               "ln": ["Core", "Essentials"]},
    "Touch Screen Phones":  {"mat": ["Glass", "Aluminum"],                   "mw": [0.55, 0.45], "wt": (0.15, 0.25),  "sz": "xs", "sty": ["Modern", "Minimalist", "Premium"],                 "ln": ["Core", "Pro", "Elite"]},
    "Smart phones & PDAs":  {"mat": ["Glass", "Aluminum"],                   "mw": [0.55, 0.45], "wt": (0.15, 0.25),  "sz": "xs", "sty": ["Modern", "Minimalist", "Premium"],                 "ln": ["Core", "Pro", "Elite"]},
    "Cell phones Accessories": {"mat": ["Plastic", "Rubber", "Leather"],     "mw": [0.45, 0.30, 0.25], "wt": (0.02, 0.20), "sz": "xs", "sty": ["Modern", "Casual"],                           "ln": ["Core", "Essentials"]},
    # ---- Music, Movies and Audio Books (Category 6) ----
    "Music CD":             {"mat": ["Plastic"],                             "mw": [1.0],        "wt": (0.08, 0.15),  "sz": "xs", "sty": ["Classic"],                                         "ln": ["Classic", "Essentials"]},
    "Movie DVD":            {"mat": ["Plastic"],                             "mw": [1.0],        "wt": (0.08, 0.15),  "sz": "xs", "sty": ["Classic"],                                         "ln": ["Classic", "Essentials"]},
    "Audio Books":          {"mat": ["Plastic"],                             "mw": [1.0],        "wt": (0.08, 0.20),  "sz": "xs", "sty": ["Classic"],                                         "ln": ["Classic", "Essentials"]},
    # ---- Games and Toys (Category 7) ----
    "Boxed Games":          {"mat": ["Plastic", "Cardboard"],                "mw": [0.45, 0.55], "wt": (0.20, 1.50),  "sz": "sm", "sty": ["Casual", "Modern"],                                "ln": ["Core", "Active", "Studio"]},
    "Download Games":       {"mat": ["Digital"],                             "mw": [1.0],        "wt": (0.0, 0.0),    "sz": "digital", "sty": ["Casual", "Modern"],                            "ln": ["Core", "Active", "Studio"], "digital": True},
    "Games Accessories":    {"mat": ["Plastic", "Rubber"],                   "mw": [0.65, 0.35], "wt": (0.10, 0.50),  "sz": "sm", "sty": ["Modern", "Sport"],                                 "ln": ["Core", "Active"]},
    # ---- Home Appliances (Category 8) ----
    "Washers & Dryers":     {"mat": ["Steel"],                               "mw": [1.0],        "wt": (50.0, 90.0),  "sz": "xl", "sty": ["Classic", "Modern"],                               "ln": ["Home", "Essentials"]},
    "Refrigerators":        {"mat": ["Steel"],                               "mw": [1.0],        "wt": (40.0, 120.0), "sz": "xl", "sty": ["Classic", "Modern"],                               "ln": ["Home", "Essentials"]},
    "Microwaves":           {"mat": ["Steel", "Plastic"],                    "mw": [0.60, 0.40], "wt": (10.0, 25.0),  "sz": "md", "sty": ["Modern", "Classic"],                               "ln": ["Home", "Essentials"]},
    "Water Heaters":        {"mat": ["Steel"],                               "mw": [1.0],        "wt": (15.0, 50.0),  "sz": "lg", "sty": ["Classic"],                                         "ln": ["Home", "Essentials"], "season": "Winter"},
    "Coffee Machines":      {"mat": ["Plastic", "Steel"],                    "mw": [0.55, 0.45], "wt": (2.0, 10.0),   "sz": "md", "sty": ["Modern", "Classic", "Premium"],                    "ln": ["Home", "Essentials", "Pro"]},
    "Lamps":                {"mat": ["Glass", "Aluminum", "Plastic"],        "mw": [0.40, 0.35, 0.25], "wt": (0.50, 5.0), "sz": "sm", "sty": ["Modern", "Classic", "Minimalist", "Vintage"],  "ln": ["Home", "Classic", "Studio"]},
    "Air Conditioners":     {"mat": ["Steel", "Plastic"],                    "mw": [0.65, 0.35], "wt": (15.0, 50.0),  "sz": "lg", "sty": ["Modern", "Classic"],                               "ln": ["Home", "Essentials"], "season": "Summer"},
    "Fans":                 {"mat": ["Plastic", "Steel"],                    "mw": [0.55, 0.45], "wt": (2.0, 8.0),    "sz": "md", "sty": ["Modern", "Classic"],                               "ln": ["Home", "Essentials"], "season": "Summer"},
    # ---- Clothing & Apparel (Category 9) ----
    "T-Shirts & Tops":      {"mat": ["Cotton", "Polyester", "Linen"],        "mw": [0.50, 0.35, 0.15], "wt": (0.10, 0.30),  "sz": "xs", "sty": ["Casual", "Modern", "Classic"],                      "ln": ["Core", "Essentials", "Active"]},
    "Pants & Jeans":         {"mat": ["Cotton", "Polyester", "Denim"],        "mw": [0.40, 0.25, 0.35], "wt": (0.30, 0.80),  "sz": "sm", "sty": ["Casual", "Classic", "Modern"],                      "ln": ["Core", "Essentials"]},
    "Jackets & Coats":       {"mat": ["Polyester", "Nylon", "Cotton"],        "mw": [0.40, 0.35, 0.25], "wt": (0.50, 2.00),  "sz": "sm", "sty": ["Modern", "Classic", "Sport"],                       "ln": ["Core", "Pro", "Active"], "season": "Winter"},
    "Dresses & Skirts":      {"mat": ["Cotton", "Polyester", "Silk"],         "mw": [0.40, 0.40, 0.20], "wt": (0.15, 0.60),  "sz": "xs", "sty": ["Classic", "Modern", "Casual"],                      "ln": ["Core", "Essentials"]},
    "Activewear":            {"mat": ["Polyester", "Nylon", "Spandex"],       "mw": [0.45, 0.35, 0.20], "wt": (0.10, 0.50),  "sz": "xs", "sty": ["Sport", "Modern", "Casual"],                        "ln": ["Active", "Core", "Pro"]},
    "Footwear":              {"mat": ["Leather", "Rubber", "Synthetic"],      "mw": [0.40, 0.35, 0.25], "wt": (0.30, 1.50),  "sz": "sm", "sty": ["Casual", "Sport", "Classic"],                       "ln": ["Core", "Active", "Pro"]},
    "Fashion Accessories":   {"mat": ["Leather", "Cotton", "Polyester"],      "mw": [0.40, 0.35, 0.25], "wt": (0.05, 0.50),  "sz": "xs", "sty": ["Classic", "Modern", "Casual"],                      "ln": ["Core", "Essentials"]},
    # ---- Food & Beverage (Category 10) ----
    "Snacks & Confectionery": {"mat": ["Cardboard", "Plastic"],               "mw": [0.55, 0.45], "wt": (0.05, 0.50),  "sz": "xs", "sty": ["Classic", "Casual"],                                    "ln": ["Core", "Essentials"]},
    "Beverages":             {"mat": ["Plastic", "Glass", "Aluminum"],        "mw": [0.40, 0.35, 0.25], "wt": (0.30, 5.00),  "sz": "sm", "sty": ["Classic", "Modern"],                               "ln": ["Core", "Essentials"]},
    "Coffee & Tea":          {"mat": ["Cardboard", "Plastic", "Foil"],        "mw": [0.40, 0.35, 0.25], "wt": (0.10, 1.00),  "sz": "xs", "sty": ["Classic", "Modern", "Premium"],                    "ln": ["Core", "Essentials", "Pro"]},
    "Canned & Packaged Goods": {"mat": ["Steel", "Cardboard", "Glass"],       "mw": [0.45, 0.30, 0.25], "wt": (0.15, 1.00),  "sz": "xs", "sty": ["Classic"],                                        "ln": ["Core", "Essentials"]},
    "Condiments & Sauces":   {"mat": ["Glass", "Plastic"],                    "mw": [0.55, 0.45], "wt": (0.15, 0.80),  "sz": "xs", "sty": ["Classic"],                                              "ln": ["Core", "Essentials"]},
    "Frozen Foods":          {"mat": ["Cardboard", "Plastic"],                "mw": [0.50, 0.50], "wt": (0.20, 1.50),  "sz": "xs", "sty": ["Classic", "Casual"],                                    "ln": ["Core", "Essentials"]},
    # ---- Health & Beauty (Category 11) ----
    "Skincare":              {"mat": ["Plastic", "Glass"],                    "mw": [0.55, 0.45], "wt": (0.02, 0.25),  "sz": "xs", "sty": ["Modern", "Premium", "Minimalist"],                      "ln": ["Core", "Pro", "Essentials"]},
    "Haircare":              {"mat": ["Plastic"],                             "mw": [1.0],        "wt": (0.10, 0.50),  "sz": "xs", "sty": ["Modern", "Classic"],                                     "ln": ["Core", "Essentials", "Pro"]},
    "Vitamins & Supplements": {"mat": ["Plastic"],                            "mw": [1.0],        "wt": (0.05, 0.30),  "sz": "xs", "sty": ["Classic", "Modern"],                                     "ln": ["Core", "Essentials"]},
    "Fragrances":            {"mat": ["Glass", "Aluminum"],                   "mw": [0.65, 0.35], "wt": (0.03, 0.20),  "sz": "xs", "sty": ["Premium", "Modern", "Classic"],                         "ln": ["Core", "Pro", "Elite"]},
    "Oral Care":             {"mat": ["Plastic"],                             "mw": [1.0],        "wt": (0.05, 0.30),  "sz": "xs", "sty": ["Modern", "Classic"],                                     "ln": ["Core", "Essentials"]},
    # ---- Home & Furniture (Category 12) ----
    "Living Room Furniture": {"mat": ["Wood", "Fabric", "Steel"],             "mw": [0.45, 0.35, 0.20], "wt": (5.0, 80.0),   "sz": "xl", "sty": ["Modern", "Classic", "Minimalist"],                 "ln": ["Home", "Classic", "Studio"]},
    "Bedroom Furniture":     {"mat": ["Wood", "Fabric", "Steel"],             "mw": [0.50, 0.30, 0.20], "wt": (10.0, 100.0), "sz": "xl", "sty": ["Modern", "Classic"],                               "ln": ["Home", "Classic"]},
    "Office Furniture":      {"mat": ["Steel", "Wood", "Plastic"],            "mw": [0.40, 0.35, 0.25], "wt": (5.0, 40.0),   "sz": "lg", "sty": ["Modern", "Classic", "Minimalist"],                 "ln": ["Core", "Pro"]},
    "Rugs & Carpets":        {"mat": ["Wool", "Cotton", "Polyester"],         "mw": [0.35, 0.35, 0.30], "wt": (1.0, 15.0),   "sz": "lg", "sty": ["Classic", "Modern", "Vintage"],                    "ln": ["Home", "Classic", "Studio"]},
    "Home Décor":            {"mat": ["Ceramic", "Glass", "Wood"],            "mw": [0.40, 0.30, 0.30], "wt": (0.10, 5.0),   "sz": "sm", "sty": ["Modern", "Classic", "Minimalist", "Vintage"],      "ln": ["Home", "Classic", "Studio"]},
    # ---- Sports & Outdoors (Category 13) ----
    "Fitness Equipment":     {"mat": ["Steel", "Rubber", "Plastic"],          "mw": [0.50, 0.30, 0.20], "wt": (0.50, 50.0),  "sz": "md", "sty": ["Sport", "Modern"],                                 "ln": ["Active", "Pro", "Core"]},
    "Camping & Hiking":      {"mat": ["Nylon", "Polyester", "Aluminum"],      "mw": [0.40, 0.35, 0.25], "wt": (0.20, 5.0),   "sz": "sm", "sty": ["Sport", "Classic"],                                "ln": ["Active", "Core", "Pro"], "season": "Summer"},
    "Cycling":               {"mat": ["Aluminum", "Steel", "Rubber"],         "mw": [0.40, 0.35, 0.25], "wt": (0.10, 15.0),  "sz": "md", "sty": ["Sport", "Modern"],                                 "ln": ["Active", "Pro", "Core"]},
    "Water Sports":          {"mat": ["Nylon", "Rubber", "Plastic"],          "mw": [0.40, 0.35, 0.25], "wt": (0.20, 5.0),   "sz": "sm", "sty": ["Sport", "Modern"],                                 "ln": ["Active", "Core"], "season": "Summer"},
    "Team Sports":           {"mat": ["Leather", "Rubber", "Polyester"],      "mw": [0.35, 0.35, 0.30], "wt": (0.10, 2.0),   "sz": "sm", "sty": ["Sport", "Classic"],                                "ln": ["Active", "Core"]},
    # ---- Office & Stationery (Category 14) ----
    "Paper & Notebooks":     {"mat": ["Paper", "Cardboard"],                  "mw": [0.70, 0.30], "wt": (0.05, 0.50),  "sz": "xs", "sty": ["Classic", "Modern"],                                    "ln": ["Core", "Essentials"]},
    "Writing Instruments":   {"mat": ["Plastic", "Aluminum"],                 "mw": [0.60, 0.40], "wt": (0.01, 0.10),  "sz": "xs", "sty": ["Classic", "Modern", "Premium"],                         "ln": ["Core", "Essentials", "Pro"]},
    "Desk Organizers":       {"mat": ["Plastic", "Steel", "Wood"],            "mw": [0.45, 0.30, 0.25], "wt": (0.10, 2.0),   "sz": "sm", "sty": ["Modern", "Classic", "Minimalist"],                 "ln": ["Core", "Essentials"]},
    "Bags & Cases":          {"mat": ["Nylon", "Polyester", "Leather"],       "mw": [0.40, 0.35, 0.25], "wt": (0.30, 3.0),   "sz": "sm", "sty": ["Modern", "Classic", "Casual"],                     "ln": ["Core", "Pro"]},
    # ---- Garden & Outdoor Living (Category 15) ----
    "Power Tools":           {"mat": ["Steel", "Plastic"],                    "mw": [0.55, 0.45], "wt": (1.0, 10.0),   "sz": "sm", "sty": ["Modern", "Classic"],                                    "ln": ["Core", "Pro"]},
    "Hand Tools":            {"mat": ["Steel", "Rubber", "Wood"],             "mw": [0.50, 0.25, 0.25], "wt": (0.10, 2.0),   "sz": "xs", "sty": ["Classic"],                                        "ln": ["Core", "Essentials"]},
    "Outdoor Furniture":     {"mat": ["Aluminum", "Wood", "Polyester"],       "mw": [0.40, 0.35, 0.25], "wt": (3.0, 30.0),   "sz": "lg", "sty": ["Modern", "Classic"],                               "ln": ["Home", "Classic"], "season": "Summer"},
    "Grills & BBQ":          {"mat": ["Steel", "Cast Iron", "Aluminum"],      "mw": [0.50, 0.30, 0.20], "wt": (5.0, 50.0),   "sz": "lg", "sty": ["Classic", "Modern"],                               "ln": ["Home", "Pro"], "season": "Summer"},
    "Garden Care":           {"mat": ["Plastic", "Steel"],                    "mw": [0.55, 0.45], "wt": (0.20, 5.0),   "sz": "sm", "sty": ["Classic"],                                              "ln": ["Home", "Essentials"], "season": "Summer"},
    # ---- Automotive (Category 16) ----
    "Car Care & Cleaning":   {"mat": ["Plastic"],                             "mw": [1.0],        "wt": (0.20, 2.0),   "sz": "xs", "sty": ["Classic", "Modern"],                                     "ln": ["Core", "Essentials"]},
    "Interior Accessories":  {"mat": ["Plastic", "Rubber", "Leather"],        "mw": [0.40, 0.35, 0.25], "wt": (0.10, 2.0),   "sz": "xs", "sty": ["Classic", "Modern"],                               "ln": ["Core", "Essentials"]},
    "Car Electronics":       {"mat": ["Plastic", "Aluminum"],                 "mw": [0.60, 0.40], "wt": (0.10, 1.5),   "sz": "xs", "sty": ["Modern", "Sport"],                                      "ln": ["Core", "Pro"]},
    "Tires & Parts":         {"mat": ["Rubber", "Steel"],                     "mw": [0.60, 0.40], "wt": (5.0, 15.0),   "sz": "md", "sty": ["Classic"],                                              "ln": ["Core", "Essentials"]},
}

_DEFAULT_ARCHETYPE: dict = {
    "mat": ["Plastic", "Steel", "Aluminum"],
    "mw": [0.45, 0.30, 0.25],
    "wt": (0.5, 10.0),
    "sz": "sm",
    "sty": ["Classic", "Modern", "Casual"],
    "ln": ["Core", "Essentials"],
}

# Validate all archetype material weight arrays sum to ~1.0 at import time
for _sc_name, _arch in list(_SUBCATEGORY_ARCHETYPES.items()) + [("_DEFAULT", _DEFAULT_ARCHETYPE)]:
    _mw_sum = float(sum(_arch["mw"]))
    if abs(_mw_sum - 1.0) > 1e-6:
        raise ValidationError(f"product_profile._SUBCATEGORY_ARCHETYPES[{_sc_name!r}].mw sums to {_mw_sum}, expected 1.0")
    if len(_arch["mat"]) != len(_arch["mw"]):
        raise ValidationError(f"product_profile._SUBCATEGORY_ARCHETYPES[{_sc_name!r}] mat/mw length mismatch")
del _sc_name, _arch, _mw_sum

_SIZE_DIMS: dict[str, dict] = {
    "xs":      {"L": (3, 15),   "W": (2, 10),   "H": (1, 8)},
    "sm":      {"L": (10, 40),  "W": (8, 30),   "H": (3, 25)},
    "md":      {"L": (25, 65),  "W": (15, 50),  "H": (10, 45)},
    "lg":      {"L": (50, 100), "W": (40, 70),  "H": (50, 100)},
    "xl":      {"L": (55, 90),  "W": (55, 75),  "H": (80, 180)},
    "digital": {"L": (0, 0),    "W": (0, 0),    "H": (0, 0)},
}


_AGE_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("baby", "infant", "toddler"), "Baby"),
    (("kid", "kids", "child", "children", "youth"), "Kids"),
    (("teen", "teenage"), "Teen"),
]


_SEASON_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("holiday", "christmas", "xmas"), "Holiday"),
    (("winter", "snow"), "Winter"),
    (("summer", "beach"), "Summer"),
    (("spring",), "Spring"),
    (("back to school", "school"), "BackToSchool"),
]


def _lower_series(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("").str.lower()


def _pick_from_list(u: np.ndarray, choices: list[str]) -> np.ndarray:
    if len(choices) == 1:
        return np.full(u.shape[0], choices[0], dtype=object)
    idx = np.floor(u * len(choices)).astype(np.int64)
    idx = np.clip(idx, 0, len(choices) - 1)
    return np.array([choices[i] for i in idx], dtype=object)


def _resolve_subcategory_names(df: pd.DataFrame, output_folder: Path) -> np.ndarray:
    """Map each row's SubcategoryKey to its subcategory name string."""
    subcat_path = output_folder / "product_subcategory.parquet"
    if subcat_path.exists():
        sc = pd.read_parquet(subcat_path)
    else:
        sc = pd.read_parquet(Path("data/contoso_products/product_subcategory.parquet"))
    name_col = "Subcategory" if "Subcategory" in sc.columns else "SubcategoryLabel"
    subcat_map = dict(zip(sc["SubcategoryKey"], sc[name_col].astype(str).str.strip()))
    return df["SubcategoryKey"].map(subcat_map).fillna("Unknown").to_numpy()


def enrich_products_attributes(
    df: pd.DataFrame,
    cfg: dict,
    *,
    seed: int,
    output_folder: Path,
    _skip_post_merge: bool = False,
) -> pd.DataFrame:
    """Add enrichment columns to the product DataFrame.

    When *_skip_post_merge* is True, rank-dependent columns (BrandTier and
    everything derived from it) are skipped.  The parallel orchestrator calls
    :func:`apply_post_merge_enrichment` on the merged result to compute them.
    """
    out = df.copy()

    # Base keys
    base = pd.to_numeric(out.get("BaseProductID", out["ProductKey"]), errors="coerce").fillna(0).astype("int64").to_numpy()
    uniq_base, inv = np.unique(base, return_inverse=True)
    b_u64 = uniq_base.astype("uint64", copy=False)

    # --- Merch / taxonomy ---
    # ColorFamily from existing Color (rules-based; no RNG)
    if "Color" in out.columns:
        col_l = _lower_series(out["Color"])
        fam = pd.Series("Other", index=out.index, dtype="string")
        for keys, name in _COLOR_FAMILY_RULES:
            mask = False
            for k in keys:
                mask = mask | col_l.str.contains(k, regex=False)
            fam = fam.mask(mask, name)
        out["ColorFamily"] = fam.astype("string")
    else:
        out["ColorFamily"] = pd.Series("Other", index=out.index, dtype="string")

    # --- Resolve subcategory for archetype-driven generation ---
    _subcat_names = _resolve_subcategory_names(out, output_folder)
    N = len(out)

    # Map unique base products to their subcategory (first per base)
    _base_subcat = pd.Series(_subcat_names, index=out.index).groupby(
        pd.Series(base, index=out.index)
    ).first().reindex(uniq_base, fill_value="Unknown").to_numpy()

    # Text for keyword matching (AgeGroup, SeasonalityProfile)
    name_l = _lower_series(out.get("ProductName", pd.Series("", index=out.index)))
    desc_l = _lower_series(out.get("ProductDescription", pd.Series("", index=out.index)))
    text_l = (name_l + " " + desc_l).astype("string")

    # --- Material, Style, ProductLine (archetype-driven) ---
    u_mat = _base_uniform(b_u64, seed, 0x11111111)[inv]
    u_sty = _base_uniform(b_u64, seed, 0x22222222)[inv]
    u_line = _base_uniform(b_u64, seed, 0x33333333)[inv]

    mat = np.full(N, "Plastic", dtype=object)
    sty = np.full(N, "Classic", dtype=object)
    ln = np.full(N, "Core", dtype=object)

    for sc_name, arch in _SUBCATEGORY_ARCHETYPES.items():
        mask = _subcat_names == sc_name
        if not mask.any():
            continue
        # Material (weighted selection)
        m_list, m_w = arch["mat"], arch["mw"]
        cum = np.cumsum(m_w, dtype=np.float64)
        cum /= cum[-1]
        cum[-1] = 1.0
        idx = np.clip(np.searchsorted(cum, u_mat[mask]), 0, len(m_list) - 1)
        mat[mask] = np.array(m_list, dtype=object)[idx]
        # Style (uniform selection)
        s_list = arch["sty"]
        idx_s = np.clip(np.floor(u_sty[mask] * len(s_list)).astype(int), 0, len(s_list) - 1)
        sty[mask] = np.array(s_list, dtype=object)[idx_s]
        # ProductLine (uniform selection)
        l_list = arch["ln"]
        idx_l = np.clip(np.floor(u_line[mask] * len(l_list)).astype(int), 0, len(l_list) - 1)
        ln[mask] = np.array(l_list, dtype=object)[idx_l]

    # Default archetype for any unmatched subcategories
    unmatched = ~np.isin(_subcat_names, list(_SUBCATEGORY_ARCHETYPES.keys()))
    if unmatched.any():
        da = _DEFAULT_ARCHETYPE
        cum = np.cumsum(da["mw"], dtype=np.float64)
        cum /= cum[-1]
        cum[-1] = 1.0
        mat[unmatched] = np.array(da["mat"], dtype=object)[np.clip(np.searchsorted(cum, u_mat[unmatched]), 0, len(da["mat"]) - 1)]
        sty[unmatched] = np.array(da["sty"], dtype=object)[np.clip(np.floor(u_sty[unmatched] * len(da["sty"])).astype(int), 0, len(da["sty"]) - 1)]
        ln[unmatched] = np.array(da["ln"], dtype=object)[np.clip(np.floor(u_line[unmatched] * len(da["ln"])).astype(int), 0, len(da["ln"]) - 1)]

    out["Material"] = pd.Series(mat, index=out.index, dtype="string")
    out["Style"] = pd.Series(sty, index=out.index, dtype="string")
    out["ProductLine"] = pd.Series(ln, index=out.index, dtype="string")

    # AgeGroup: keyword first, default to "Adult"
    age = pd.Series("", index=out.index, dtype="string")
    for keys, val in _AGE_KEYWORDS:
        mask = False
        for k in keys:
            mask = mask | text_l.str.contains(k, regex=False)
        age = age.mask((age == "") & mask, val)
    age = age.mask(age == "", "Adult")
    out["AgeGroup"] = age.astype("string")

    # SeasonalityProfile: keyword first, then archetype bias, then default
    seas = pd.Series("", index=out.index, dtype="string")
    for keys, val in _SEASON_KEYWORDS:
        mask = False
        for k in keys:
            mask = mask | text_l.str.contains(k, regex=False)
        seas = seas.mask((seas == "") & mask, val)

    if (seas == "").any():
        u_seas = _base_uniform(b_u64, seed, 0x44444444)[inv]
        seas_arr = seas.to_numpy(copy=True).astype(object)
        for sc_name, arch in _SUBCATEGORY_ARCHETYPES.items():
            sc_mask = (_subcat_names == sc_name) & (seas_arr == "")
            if not sc_mask.any():
                continue
            sc_season = arch.get("season")
            u_s = u_seas[sc_mask]
            if sc_season:
                # Higher probability for the archetype's natural season
                seas_arr[sc_mask] = np.where(
                    u_s < 0.40, sc_season,
                    np.where(u_s < 0.70, "None",
                    np.where(u_s < 0.80, "Holiday",
                    np.where(u_s < 0.90, "Winter", "Summer"))))
            else:
                seas_arr[sc_mask] = np.where(
                    u_s < 0.70, "None",
                    np.where(u_s < 0.78, "Holiday",
                    np.where(u_s < 0.86, "Winter",
                    np.where(u_s < 0.93, "Summer", "BackToSchool"))))
        # Fallback for unmatched subcategories
        still_empty = seas_arr == ""
        if still_empty.any():
            u_s = u_seas[still_empty]
            seas_arr[still_empty] = np.where(
                u_s < 0.70, "None",
                np.where(u_s < 0.78, "Holiday",
                np.where(u_s < 0.86, "Winter",
                np.where(u_s < 0.93, "Summer", "BackToSchool"))))
        seas = pd.Series(seas_arr, index=out.index, dtype="string")
    out["SeasonalityProfile"] = seas.astype("string")

    # --- Logistics & fulfillment (archetype-driven, base-level) ---
    u_w = _base_uniform(b_u64, seed, 0x61616161)
    u_d1 = _base_uniform(b_u64, seed, 0x71717171)
    u_d2 = _base_uniform(b_u64, seed, 0x81818181)
    u_d3 = _base_uniform(b_u64, seed, 0x91919191)

    n_base = len(uniq_base)
    w_kg = np.full(n_base, 1.0, dtype=np.float64)
    L = np.full(n_base, 20.0, dtype=np.float64)
    W_dim = np.full(n_base, 15.0, dtype=np.float64)
    H = np.full(n_base, 10.0, dtype=np.float64)
    is_digital_base = np.zeros(n_base, dtype=bool)
    # size_bucket: 0=small, 1=medium, 2=large (used by downstream sections)
    size_bucket = np.full(n_base, 0, dtype=np.int64)

    for sc_name, arch in _SUBCATEGORY_ARCHETYPES.items():
        b_mask = _base_subcat == sc_name
        if not b_mask.any():
            continue
        if arch.get("digital", False):
            w_kg[b_mask] = 0.0
            L[b_mask] = 0.0
            W_dim[b_mask] = 0.0
            H[b_mask] = 0.0
            is_digital_base[b_mask] = True
            continue
        wt_lo, wt_hi = arch["wt"]
        w_kg[b_mask] = wt_lo + u_w[b_mask] * (wt_hi - wt_lo)
        # Explicit dims override, else fall back to size-class ranges
        dims = arch.get("dims") or _SIZE_DIMS[arch["sz"]]
        L[b_mask] = dims["L"][0] + u_d1[b_mask] * (dims["L"][1] - dims["L"][0])
        W_dim[b_mask] = dims["W"][0] + u_d2[b_mask] * (dims["W"][1] - dims["W"][0])
        H[b_mask] = dims["H"][0] + u_d3[b_mask] * (dims["H"][1] - dims["H"][0])
        # Map size class to bucket
        if arch["sz"] in ("lg", "xl"):
            size_bucket[b_mask] = 2
        elif arch["sz"] == "md":
            size_bucket[b_mask] = 1
        # xs, sm → 0 (default)

    # Default archetype for unmatched base products
    unmatched_b = ~np.isin(_base_subcat, list(_SUBCATEGORY_ARCHETYPES.keys()))
    if unmatched_b.any():
        da = _DEFAULT_ARCHETYPE
        wt_lo, wt_hi = da["wt"]
        w_kg[unmatched_b] = wt_lo + u_w[unmatched_b] * (wt_hi - wt_lo)
        sz = _SIZE_DIMS[da["sz"]]
        L[unmatched_b] = sz["L"][0] + u_d1[unmatched_b] * (sz["L"][1] - sz["L"][0])
        W_dim[unmatched_b] = sz["W"][0] + u_d2[unmatched_b] * (sz["W"][1] - sz["W"][0])
        H[unmatched_b] = sz["H"][0] + u_d3[unmatched_b] * (sz["H"][1] - sz["H"][0])

    vol = L * W_dim * H

    # Shipping class thresholds
    freight = (w_kg >= 35.0) | (vol >= 150_000.0)
    oversize = (~freight) & ((w_kg >= 15.0) | (vol >= 90_000.0))
    ship_class = np.where(
        is_digital_base, "Digital",
        np.where(freight, "Freight", np.where(oversize, "Oversize", "Standard")))

    # Fragile/Hazmat (base)
    u_frag = _base_uniform(b_u64, seed, 0xA0A0A0A0)
    u_haz = _base_uniform(b_u64, seed, 0xB0B0B0B0)

    # Use base-level material
    mat_row = out["Material"].astype("string")
    base_mat = mat_row.groupby(pd.Series(base, index=out.index)).first()
    base_mat = base_mat.reindex(uniq_base, fill_value="Plastic").astype("string").to_numpy()

    fragile_base = ((u_frag < 0.10) | np.isin(base_mat, ["Glass", "Ceramic"])) & (~is_digital_base)
    haz_base = (u_haz < 0.01) & (~is_digital_base)

    # Lead time depends on shipping class
    u_lt = _base_uniform(b_u64, seed, 0xC0C0C0C0)
    lead = np.zeros_like(u_lt, dtype=np.int64)
    lead[ship_class == "Standard"] = (2 + np.floor(u_lt[ship_class == "Standard"] * 9)).astype(np.int64)
    lead[ship_class == "Oversize"] = (5 + np.floor(u_lt[ship_class == "Oversize"] * 17)).astype(np.int64)
    lead[ship_class == "Freight"] = (10 + np.floor(u_lt[ship_class == "Freight"] * 36)).astype(np.int64)
    # Digital → 0 (already zeroed)

    # Case pack qty
    u_cp = _base_uniform(b_u64, seed, 0xD0D0D0D0)
    case_opts = [1, 2, 4, 6, 8, 12, 24]
    case_pack = np.array(case_opts, dtype=np.int64)[np.clip((u_cp * len(case_opts)).astype(np.int64), 0, len(case_opts) - 1)]
    case_pack[is_digital_base] = 1

    # Fulfillment type
    u_f = _base_uniform(b_u64, seed, 0xE0E0E0E0)
    fulfil = np.where(
        is_digital_base, "Digital",
        np.where(u_f < 0.70, "Stocked", np.where(u_f < 0.88, "3PL", "DropShip")))

    # Broadcast logistics to rows
    out["WeightKg"] = pd.Series(w_kg[inv], index=out.index).astype("float32")
    out["LengthCm"] = pd.Series(L[inv], index=out.index).astype("float32")
    out["WidthCm"] = pd.Series(W_dim[inv], index=out.index).astype("float32")
    out["HeightCm"] = pd.Series(H[inv], index=out.index).astype("float32")
    out["VolumeCm3"] = pd.Series(vol[inv], index=out.index).astype("float32")
    out["ShippingClass"] = pd.Series(ship_class[inv], index=out.index, dtype="string")
    out["IsFragile"] = pd.Series(fragile_base[inv].astype(bool), index=out.index)
    out["IsHazmat"] = pd.Series(haz_base[inv].astype(bool), index=out.index)
    out["LeadTimeDays"] = pd.Series(lead[inv], index=out.index).astype("int32")
    out["CasePackQty"] = pd.Series(case_pack[inv], index=out.index).astype("int32")
    out["FulfillmentType"] = pd.Series(fulfil[inv], index=out.index, dtype="string")

    # --- Channel eligibility (hash-seeded, safe in chunks) ---
    u_ch = _base_uniform(b_u64, seed, 0xF0F0F0F0)[inv]
    is_haz = out["IsHazmat"].to_numpy(dtype=bool, copy=False)
    ship = out["ShippingClass"].astype("string")

    out["EligibleStore"] = True
    out["EligibleOnline"] = True

    # Marketplace: avoid hazmat/freight, then probabilistic
    eligible_mkt = (~is_haz) & (ship != "Freight") & (u_ch < 0.70)
    out["EligibleMarketplace"] = eligible_mkt

    # B2B: small share + bias if Class contains business/enterprise
    cls = _lower_series(out.get("Class", pd.Series("", index=out.index)))
    b2b_bias = cls.str.contains("business", regex=False) | cls.str.contains("enterprise", regex=False)
    eligible_b2b = (u_ch < 0.10) | b2b_bias
    out["EligibleB2B"] = eligible_b2b

    # --- Sourcing & Compliance (hash-seeded, safe in chunks) ---
    _COUNTRY_OF_ORIGIN = ["China", "USA", "Germany", "India", "Vietnam", "Mexico", "Japan", "Taiwan"]
    _COO_PROBS_BY_MATERIAL = {
        "Steel":      [0.30, 0.15, 0.20, 0.10, 0.05, 0.05, 0.10, 0.05],
        "Aluminum":   [0.35, 0.10, 0.15, 0.10, 0.05, 0.10, 0.10, 0.05],
        "Plastic":    [0.40, 0.10, 0.10, 0.10, 0.15, 0.10, 0.03, 0.02],
        "Cotton":     [0.20, 0.05, 0.05, 0.30, 0.25, 0.05, 0.05, 0.05],
        "Wood":       [0.25, 0.15, 0.10, 0.10, 0.20, 0.10, 0.05, 0.05],
    }
    _COO_DEFAULT_PROBS = [0.35, 0.12, 0.10, 0.12, 0.12, 0.08, 0.06, 0.05]

    u_coo = _base_uniform(b_u64, seed, 0x1A1A1A1A)[inv]
    mat_vals = out["Material"].astype(str).to_numpy()
    coo = np.empty(N, dtype=object)
    assigned = np.zeros(N, dtype=bool)
    for mat_key, probs in _COO_PROBS_BY_MATERIAL.items():
        mask = (~assigned) & (mat_vals == mat_key)
        if mask.any():
            cum = np.cumsum(probs, dtype=np.float64)
            cum /= cum[-1]
            cum[-1] = 1.0
            idx = np.searchsorted(cum, u_coo[mask])
            idx = np.clip(idx, 0, len(_COUNTRY_OF_ORIGIN) - 1)
            coo[mask] = np.array(_COUNTRY_OF_ORIGIN)[idx]
            assigned[mask] = True
    if (~assigned).any():
        cum = np.cumsum(_COO_DEFAULT_PROBS, dtype=np.float64)
        cum /= cum[-1]
        cum[-1] = 1.0
        idx = np.searchsorted(cum, u_coo[~assigned])
        idx = np.clip(idx, 0, len(_COUNTRY_OF_ORIGIN) - 1)
        coo[~assigned] = np.array(_COUNTRY_OF_ORIGIN)[idx]
    out["CountryOfOrigin"] = pd.Series(coo, index=out.index, dtype="string")

    u_cert = _base_uniform(b_u64, seed, 0x3A3A3A3A)[inv]
    cert = np.where(
        u_cert < 0.25, "CE",
        np.where(u_cert < 0.45, "UL",
        np.where(u_cert < 0.60, "ISO",
        np.where(u_cert < 0.68, "FDA", "None"))),
    )
    out["CertificationType"] = pd.Series(cert, index=out.index, dtype="string")

    u_asm = _base_uniform(b_u64, seed, 0x4A4A4A4A)[inv]
    asm_prob = np.where(size_bucket[inv] == 2, 0.55, np.where(size_bucket[inv] == 1, 0.25, 0.08))
    out["AssemblyRequired"] = pd.Series(
        u_asm < asm_prob, index=out.index, dtype=bool
    )

    # PackagingType (hash-seeded, safe in chunks)
    u_pkg = _base_uniform(b_u64, seed, 0x8A8A8A8A)[inv]
    pkg = np.where(
        size_bucket[inv] == 2,
        np.where(u_pkg < 0.40, "Pallet", np.where(u_pkg < 0.75, "Bulk", "Box")),
        np.where(
            size_bucket[inv] == 1,
            np.where(u_pkg < 0.60, "Box", np.where(u_pkg < 0.85, "Bag", "Blister")),
            np.where(u_pkg < 0.35, "Blister", np.where(u_pkg < 0.70, "Box", "Bag")),
        ),
    )
    out["PackagingType"] = pd.Series(pkg, index=out.index, dtype="string")

    # IsGiftEligible (hash-seeded, safe in chunks)
    u_gift = _base_uniform(b_u64, seed, 0xCC1C1C1C)[inv]
    gift_prob = np.where(size_bucket[inv] == 0, 0.70, np.where(size_bucket[inv] == 1, 0.50, 0.20))
    out["IsGiftEligible"] = pd.Series(
        u_gift < gift_prob, index=out.index, dtype=bool
    )

    # TargetGender (keyword-based, safe in chunks)
    _GENDER_MALE_KW = ("men", " male", "boy", "gentleman")
    _GENDER_FEMALE_KW = ("women", "woman", " female", "girl", "lady", "ladies")
    tg = pd.Series("Unisex", index=out.index, dtype="string")
    for kw in _GENDER_FEMALE_KW:
        tg = tg.mask((tg == "Unisex") & text_l.str.contains(kw, regex=False), "Female")
    for kw in _GENDER_MALE_KW:
        tg = tg.mask((tg == "Unisex") & text_l.str.contains(kw, regex=False), "Male")
    out["TargetGender"] = tg

    # --- Digital product overrides ---
    _is_digital = is_digital_base[inv]
    if _is_digital.any():
        dm = pd.Series(_is_digital, index=out.index)
        out.loc[dm, "PackagingType"] = "Digital"
        out.loc[dm, "AssemblyRequired"] = False
        out.loc[dm, "IsGiftEligible"] = True

    # --- Rank-dependent columns (BrandTier + dependents) ---
    # Skipped in parallel chunk mode; computed on full dataset after merge.
    if not _skip_post_merge:
        out = apply_post_merge_enrichment(out, seed)

    return out


def apply_post_merge_enrichment(df: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Compute rank-dependent columns on the full (merged) product DataFrame.

    These columns depend on BrandTier (a rank-based partition of ListPrice
    across ALL products), so they cannot be computed correctly on chunks.
    The parallel orchestrator calls this after merging enriched chunks.

    Columns computed: BrandTier, WarrantyMonths, ReturnRateBase, DefectRateBase,
    ReturnWindowDays, SustainabilityScore, PopularityScore, ABCClassification,
    ReorderPointUnits, SafetyStockUnits, AvgCustomerRating, ReviewCount,
    CompetitorPriceIndex, MarginCategory, IsBestseller, IsEcoFriendly.
    """
    out = df

    # Recompute base key hash arrays (deterministic on BaseProductID)
    base = pd.to_numeric(
        out.get("BaseProductID", out["ProductKey"]), errors="coerce"
    ).fillna(0).astype("int64").to_numpy()
    uniq_base, inv = np.unique(base, return_inverse=True)
    b_u64 = uniq_base.astype("uint64", copy=False)
    N = len(out)

    # --- BrandTier from ListPrice (rank-based) ---
    up = pd.to_numeric(out.get("ListPrice", 0.0), errors="coerce").to_numpy(dtype=np.float64, copy=False)

    tier = np.full(N, "Mainstream", dtype=object)
    finite = np.isfinite(up)

    if finite.any() and N >= 3:
        tie = pd.to_numeric(
            out.get("ProductKey", np.arange(N)), errors="coerce"
        ).fillna(0).to_numpy(dtype=np.int64, copy=False)
        idx = np.where(finite)[0]
        order = idx[np.lexsort((tie[idx], up[idx]))]
        n = order.size
        c1 = n // 3
        c2 = (2 * n) // 3
        tier[order[:c1]] = "Value"
        tier[order[c1:c2]] = "Mainstream"
        tier[order[c2:]] = "Premium"

    out["BrandTier"] = pd.Series(tier, index=out.index, dtype="string")
    bt = out["BrandTier"].astype("string")

    # --- Quality / returns (BrandTier-dependent) ---
    u_q = _base_uniform(b_u64, seed, 0xABABABAB)[inv]

    warr = np.where(
        bt == "Premium",
        np.where(u_q < 0.50, 24, np.where(u_q < 0.85, 36, 12)),
        np.where(
            bt == "Value",
            np.where(u_q < 0.65, 6, 12),
            np.where(u_q < 0.70, 12, 24),
        ),
    )
    out["WarrantyMonths"] = pd.Series(warr, index=out.index).astype("int32")

    fragile = out["IsFragile"].to_numpy(dtype=bool, copy=False)
    is_haz = out["IsHazmat"].to_numpy(dtype=bool, copy=False)
    ship = out["ShippingClass"].astype("string")
    ship_freight = (ship == "Freight").to_numpy(dtype=bool, copy=False)

    base_return = 0.02 + (fragile * 0.05) + (ship_freight * 0.03)
    base_return = base_return - (bt == "Premium").to_numpy(dtype=np.float64, copy=False) * 0.01
    jitter_r = (u_q * 0.03)
    rr = np.clip(base_return + jitter_r, 0.005, 0.25)
    out["ReturnRateBase"] = pd.Series(rr, index=out.index).astype("float32")

    base_def = 0.002 + (bt == "Value").to_numpy(dtype=np.float64, copy=False) * 0.006 + is_haz.astype(np.float64) * 0.003
    jitter_d = (_base_uniform(b_u64, seed, 0xCDCDCDCD)[inv] * 0.005)
    dr = np.clip(base_def + jitter_d, 0.0005, 0.05)
    out["DefectRateBase"] = pd.Series(dr, index=out.index).astype("float32")

    rw = np.where(bt == "Premium", 90, np.where(bt == "Mainstream", 60, 30))
    out["ReturnWindowDays"] = pd.Series(rw, index=out.index).astype("int32")

    # --- SustainabilityScore (BrandTier-dependent) ---
    u_sus = _base_uniform(b_u64, seed, 0x2A2A2A2A)[inv]
    mat_vals = out["Material"].astype(str).to_numpy()
    tier_num = np.where(bt == "Premium", 0.7, np.where(bt == "Mainstream", 0.5, 0.3))
    mat_eco_bonus = np.where(
        np.isin(mat_vals, ["Cotton", "Wood", "Ceramic"]), 0.15, 0.0
    )
    sus_raw = (tier_num + mat_eco_bonus + u_sus * 0.3) / 1.15 * 100
    out["SustainabilityScore"] = pd.Series(np.clip(sus_raw, 1, 100).astype("int32"), index=out.index)

    # --- Inventory & Supply Chain (BrandTier-dependent) ---
    u_pop = _base_uniform(b_u64, seed, 0x5A5A5A5A)[inv]
    tier_pop_base = np.where(bt == "Premium", 0.6, np.where(bt == "Value", 0.3, 0.45))
    pop_raw = (tier_pop_base + u_pop * 0.5) / 1.1 * 100
    PopularityScore = np.clip(pop_raw, 1, 100).astype("int32")
    out["PopularityScore"] = pd.Series(PopularityScore, index=out.index)

    up_arr = pd.to_numeric(out.get("ListPrice", 0.0), errors="coerce").to_numpy(dtype=np.float64, copy=False)
    velocity_est = np.clip(PopularityScore / 100.0, 0.01, 1.0)
    abc_value = up_arr * velocity_est
    abc_rank = np.argsort(np.argsort(-abc_value))
    abc_n = len(abc_rank)
    abc_class = np.where(
        abc_rank < abc_n * 0.20, "A",
        np.where(abc_rank < abc_n * 0.50, "B", "C"),
    )
    out["ABCClassification"] = pd.Series(abc_class, index=out.index, dtype="string")

    lead_arr = out["LeadTimeDays"].to_numpy(dtype=np.int64, copy=False)
    u_rop = _base_uniform(b_u64, seed, 0x6A6A6A6A)[inv]
    rop_base = velocity_est * 30 * (lead_arr / 10.0)
    ReorderPointUnits = np.clip((rop_base * (0.5 + u_rop)).astype("int64"), 10, 5000)
    out["ReorderPointUnits"] = pd.Series(ReorderPointUnits, index=out.index).astype("int32")

    u_ss = _base_uniform(b_u64, seed, 0x7A7A7A7A)[inv]
    SafetyStockUnits = np.clip((ReorderPointUnits * (0.2 + u_ss * 0.3)).astype("int64"), 5, 1000)
    out["SafetyStockUnits"] = pd.Series(SafetyStockUnits, index=out.index).astype("int32")

    # --- Market & Customer Perception (BrandTier-dependent) ---
    # Center base at 3.6 so Premium + low-defect items land near 4.5
    # instead of saturating at the 5.0 clip ceiling.
    u_rat = _base_uniform(b_u64, seed, 0x9A9A9A9A)[inv]
    defect_arr = out["DefectRateBase"].to_numpy(dtype=np.float64, copy=False)
    rat_base = 3.6 - defect_arr * 30
    tier_rat_bonus = np.where(bt == "Premium", 0.3, np.where(bt == "Value", -0.2, 0.0))
    AvgCustomerRating = np.clip(
        np.round((rat_base + tier_rat_bonus + u_rat * 0.6), 1), 1.0, 5.0
    )
    out["AvgCustomerRating"] = pd.Series(AvgCustomerRating, index=out.index).astype("float32")

    u_rev = _base_uniform(b_u64, seed, 0xAA1A1A1A)[inv]
    review_base = PopularityScore * 50 + u_rev * 2000
    ReviewCount = np.clip(review_base.astype("int64"), 0, 10000)
    out["ReviewCount"] = pd.Series(ReviewCount, index=out.index).astype("int32")

    u_cpi = _base_uniform(b_u64, seed, 0xBB1B1B1B)[inv]
    cpi_base = np.where(bt == "Premium", 1.05, np.where(bt == "Value", 0.85, 0.95))
    CompetitorPriceIndex = np.clip(
        np.round(cpi_base + (u_cpi - 0.5) * 0.3, 2), 0.70, 1.40
    )
    out["CompetitorPriceIndex"] = pd.Series(CompetitorPriceIndex, index=out.index).astype("float32")

    cost_arr = pd.to_numeric(out.get("UnitCost", 0.0), errors="coerce").to_numpy(dtype=np.float64, copy=False)
    margin_pct = np.where(up_arr > 0, (up_arr - cost_arr) / up_arr, 0.0)
    margin_cat = np.where(
        margin_pct < 0.15, "Low",
        np.where(margin_pct < 0.30, "Standard",
        np.where(margin_pct < 0.50, "High", "Premium")),
    )
    out["MarginCategory"] = pd.Series(margin_cat, index=out.index, dtype="string")

    # IsBestseller (rank-based on PopularityScore)
    pop_rank = np.argsort(np.argsort(-PopularityScore))
    out["IsBestseller"] = pd.Series(
        pop_rank < N * 0.15, index=out.index, dtype=bool
    )

    # IsEcoFriendly (depends on SustainabilityScore which depends on BrandTier)
    sus_arr = out["SustainabilityScore"].to_numpy(dtype=np.int32, copy=False)
    out["IsEcoFriendly"] = pd.Series(
        sus_arr > 70, index=out.index, dtype=bool
    )

    return out
