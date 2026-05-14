"""
One-time script to expand the base product catalog with synthetic products.

Adds 8 new retail categories (Clothing, Food, Health, Furniture, Sports,
Office, Garden, Automotive) with ~41 subcategories and ~2,000 new products
to the existing Contoso base parquets.

Adds a 'Source' column to all three tables: 'Contoso' for originals,
'Synthetic' for new rows.

Usage:
    python scripts/expand_product_catalog.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


DATA_DIR = Path("data/contoso_products")

# -----------------------------------------------------------------------
# New categories (keys 9–16)
# -----------------------------------------------------------------------
NEW_CATEGORIES = [
    (9,  "Clothing & Apparel",      "09"),
    (10, "Food & Beverage",         "10"),
    (11, "Health & Beauty",         "11"),
    (12, "Home & Furniture",        "12"),
    (13, "Sports & Outdoors",       "13"),
    (14, "Office & Stationery",     "14"),
    (15, "Garden & Outdoor Living", "15"),
    (16, "Automotive",              "16"),
]

# -----------------------------------------------------------------------
# New subcategories (keys 49–89)
# -----------------------------------------------------------------------
NEW_SUBCATEGORIES = [
    # (SubcategoryKey, SubcategoryLabel, Subcategory, CategoryKey)
    (49, "0901", "T-Shirts & Tops",         9),
    (50, "0902", "Pants & Jeans",           9),
    (51, "0903", "Jackets & Coats",         9),
    (52, "0904", "Dresses & Skirts",        9),
    (53, "0905", "Activewear",              9),
    (54, "0906", "Footwear",                9),
    (55, "0907", "Fashion Accessories",     9),

    (56, "1001", "Snacks & Confectionery",  10),
    (57, "1002", "Beverages",               10),
    (58, "1003", "Coffee & Tea",            10),
    (59, "1004", "Canned & Packaged Goods", 10),
    (60, "1005", "Condiments & Sauces",     10),
    (61, "1006", "Frozen Foods",            10),

    (62, "1101", "Skincare",                11),
    (63, "1102", "Haircare",                11),
    (64, "1103", "Vitamins & Supplements",  11),
    (65, "1104", "Fragrances",              11),
    (66, "1105", "Oral Care",               11),

    (67, "1201", "Living Room Furniture",   12),
    (68, "1202", "Bedroom Furniture",       12),
    (69, "1203", "Office Furniture",        12),
    (70, "1204", "Rugs & Carpets",          12),
    (71, "1205", "Home Décor",              12),

    (72, "1301", "Fitness Equipment",       13),
    (73, "1302", "Camping & Hiking",        13),
    (74, "1303", "Cycling",                 13),
    (75, "1304", "Water Sports",            13),
    (76, "1305", "Team Sports",             13),

    (77, "1401", "Paper & Notebooks",       14),
    (78, "1402", "Writing Instruments",     14),
    (79, "1403", "Desk Organizers",         14),
    (80, "1404", "Bags & Cases",            14),

    (81, "1501", "Power Tools",             15),
    (82, "1502", "Hand Tools",              15),
    (83, "1503", "Outdoor Furniture",       15),
    (84, "1504", "Grills & BBQ",            15),
    (85, "1505", "Garden Care",             15),

    (86, "1601", "Car Care & Cleaning",     16),
    (87, "1602", "Interior Accessories",    16),
    (88, "1603", "Car Electronics",         16),
    (89, "1604", "Tires & Parts",           16),
]

# -----------------------------------------------------------------------
# Brands per category — real-world names for realistic analytics data
# -----------------------------------------------------------------------
BRANDS_CLOTHING = ["Nike", "Adidas", "Levi's", "Under Armour", "Patagonia", "H&M", "Gap", "Puma", "Ralph Lauren", "Calvin Klein"]
BRANDS_FOOD = ["Nestlé", "Heinz", "Kellogg's", "Del Monte", "Starbucks", "Pepsi"]
BRANDS_HEALTH = ["Neutrogena", "Dove", "L'Oréal", "Nivea", "Colgate", "Burt's Bees"]
BRANDS_FURNITURE = ["IKEA", "Ashley Home", "West Elm", "Pottery Barn", "Restoration Hardware", "Herman Miller", "Crate & Barrel"]
BRANDS_SPORTS = ["Columbia", "The North Face", "Callaway", "Wilson", "Speedo", "Peloton", "Bowflex", "Yeti"]
BRANDS_OFFICE = ["3M", "Moleskine", "Samsonite", "JanSport"]
BRANDS_GARDEN = ["DeWalt", "Black+Decker", "Weber", "Husqvarna", "Craftsman", "Milwaukee", "Makita"]
BRANDS_AUTO = ["Meguiar's", "Armor All", "Michelin", "Bosch", "Garmin"]

# Cross-category brand sets (brands that sell in multiple categories)
_CROSSOVER_ATHLETIC = ["Nike", "Adidas", "Under Armour", "Puma"]
_CROSSOVER_OUTDOOR = ["Columbia", "The North Face"]

# -----------------------------------------------------------------------
# Product templates per subcategory
#
# Each template: (name_pattern, brand_pool, colors, classes,
#                 price_min, price_max, stock_type_weights)
#
# name_pattern uses {brand}, {color}, {spec} placeholders.
# specs are subcategory-specific product line variations.
# -----------------------------------------------------------------------
TEMPLATES: dict[int, dict] = {
    # --- Clothing & Apparel (cat 9) ---
    49: {  # T-Shirts & Tops
        "specs": ["Classic Crew Tee", "V-Neck Tee", "Long Sleeve Henley",
                  "Polo Shirt", "Performance Tee", "Graphic Tee",
                  "Slim Fit Tee", "Oversized Tee"],
        "colors": ["Black", "White", "Grey", "Blue", "Red", "Green"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (12, 65),
        "brands": BRANDS_CLOTHING,
    },
    50: {  # Pants & Jeans
        "specs": ["Straight Fit Jeans", "Slim Fit Jeans", "Relaxed Chinos",
                  "Cargo Pants", "Jogger Pants", "Dress Trousers",
                  "Athletic Pants"],
        "colors": ["Black", "Blue", "Grey", "Brown"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (25, 120),
        "brands": BRANDS_CLOTHING,
    },
    51: {  # Jackets & Coats
        "specs": ["Puffer Jacket", "Rain Jacket", "Denim Jacket",
                  "Fleece Zip-Up", "Wool Overcoat", "Windbreaker",
                  "Parka", "Leather Moto Jacket", "Down Expedition Parka"],
        "colors": ["Black", "Grey", "Blue", "Green", "Brown"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (55, 800),
        "brands": BRANDS_CLOTHING + _CROSSOVER_OUTDOOR,
    },
    52: {  # Dresses & Skirts
        "specs": ["A-Line Dress", "Wrap Dress", "Midi Skirt",
                  "Maxi Dress", "Pencil Skirt", "Shift Dress"],
        "colors": ["Black", "White", "Red", "Blue", "Pink"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (30, 180),
        "brands": BRANDS_CLOTHING,
    },
    53: {  # Activewear
        "specs": ["Running Shorts", "Compression Leggings", "Sports Bra",
                  "Training Tank", "Track Jacket", "Yoga Pants",
                  "Moisture-Wicking Tee"],
        "colors": ["Black", "Grey", "Blue", "Pink", "Green"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (18, 95),
        "brands": BRANDS_CLOTHING + _CROSSOVER_OUTDOOR,
    },
    54: {  # Footwear
        "specs": ["Running Shoe", "Casual Sneaker", "Hiking Boot",
                  "Leather Oxford", "Slip-On Loafer", "Sandal",
                  "Trail Runner", "Canvas Shoe", "Premium Leather Boot"],
        "colors": ["Black", "White", "Brown", "Grey", "Blue"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (35, 500),
        "brands": BRANDS_CLOTHING + _CROSSOVER_OUTDOOR,
    },
    55: {  # Fashion Accessories
        "specs": ["Leather Belt", "Canvas Belt", "Knit Scarf",
                  "Wool Beanie", "Aviator Sunglasses", "Crossbody Bag",
                  "Wrist Watch", "Silk Tie"],
        "colors": ["Black", "Brown", "Grey", "Blue", "Red"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (10, 150),
        "brands": BRANDS_CLOTHING,
    },

    # --- Food & Beverage (cat 10) ---
    56: {  # Snacks & Confectionery
        "specs": ["Mixed Nuts 200g", "Granola Bar 6pk", "Dark Chocolate Bar 100g",
                  "Potato Chips 150g", "Trail Mix 250g", "Rice Crackers 120g",
                  "Protein Bar 12pk", "Dried Fruit Mix 180g"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (2, 18),
        "brands": BRANDS_FOOD,
    },
    57: {  # Beverages
        "specs": ["Sparkling Water 12pk", "Orange Juice 1L", "Energy Drink 4pk",
                  "Coconut Water 6pk", "Iced Tea 12pk", "Mineral Water 6pk",
                  "Lemonade 2L", "Sports Drink 8pk"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (3, 22),
        "brands": BRANDS_FOOD,
    },
    58: {  # Coffee & Tea
        "specs": ["Whole Bean Coffee 500g", "Ground Coffee 250g", "Coffee Pods 24pk",
                  "Green Tea 50 bags", "Earl Grey 40 bags", "Espresso Beans 1kg",
                  "Herbal Tea Sampler", "Cold Brew Concentrate 1L"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (5, 45),
        "brands": BRANDS_FOOD,
    },
    59: {  # Canned & Packaged Goods
        "specs": ["Diced Tomatoes 400g", "Chickpeas 400g", "Coconut Milk 400ml",
                  "Tuna in Olive Oil 185g", "Black Beans 400g", "Pasta Sauce 500g",
                  "Chicken Broth 1L", "Corn Kernels 340g"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (1, 8),
        "brands": BRANDS_FOOD,
    },
    60: {  # Condiments & Sauces
        "specs": ["Ketchup 500ml", "Mustard 250g", "Hot Sauce 150ml",
                  "Soy Sauce 500ml", "Olive Oil 750ml", "Balsamic Vinegar 250ml",
                  "Mayonnaise 400g", "BBQ Sauce 350ml"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (2, 15),
        "brands": BRANDS_FOOD,
    },
    61: {  # Frozen Foods
        "specs": ["Frozen Pizza 400g", "Ice Cream 1L", "Frozen Vegetables 500g",
                  "Fish Fillets 4pk", "Frozen Berries 500g", "Chicken Nuggets 1kg",
                  "Frozen Dumplings 600g"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (3, 18),
        "brands": BRANDS_FOOD,
    },

    # --- Health & Beauty (cat 11) ---
    62: {  # Skincare
        "specs": ["Daily Moisturizer 50ml", "Sunscreen SPF50 100ml",
                  "Night Cream 50ml", "Facial Cleanser 200ml",
                  "Eye Cream 15ml", "Serum Vitamin C 30ml",
                  "Exfoliating Scrub 100ml", "Hydrating Mask 5pk"],
        "colors": ["N/A"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (8, 120),
        "brands": BRANDS_HEALTH,
    },
    63: {  # Haircare
        "specs": ["Shampoo 400ml", "Conditioner 400ml", "Hair Oil 100ml",
                  "Styling Gel 250ml", "Dry Shampoo 200ml",
                  "Heat Protectant Spray 150ml", "Hair Mask 200ml"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (5, 45),
        "brands": BRANDS_HEALTH,
    },
    64: {  # Vitamins & Supplements
        "specs": ["Multivitamin 60ct", "Vitamin D3 90ct", "Omega-3 Fish Oil 120ct",
                  "Probiotic 30ct", "Vitamin C 1000mg 100ct", "Iron 60ct",
                  "Magnesium 90ct", "Zinc 100ct"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (8, 40),
        "brands": BRANDS_HEALTH,
    },
    65: {  # Fragrances
        "specs": ["Eau de Toilette 50ml", "Eau de Parfum 100ml",
                  "Body Mist 250ml", "Cologne 75ml",
                  "Perfume Gift Set", "Travel Spray 10ml",
                  "Luxury Parfum Collection 100ml"],
        "colors": ["N/A"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (20, 450),
        "brands": BRANDS_HEALTH,
    },
    66: {  # Oral Care
        "specs": ["Electric Toothbrush", "Toothpaste 150ml", "Mouthwash 500ml",
                  "Dental Floss 50m", "Whitening Strips 14ct",
                  "Replacement Brush Heads 4pk"],
        "colors": ["White", "Blue", "Black"],
        "classes": ["Economy", "Regular"],
        "price_range": (3, 80),
        "brands": BRANDS_HEALTH,
    },

    # --- Home & Furniture (cat 12) ---
    67: {  # Living Room Furniture
        "specs": ["3-Seater Sofa", "2-Seater Loveseat", "Recliner Chair",
                  "Coffee Table", "TV Stand", "Bookshelf",
                  "Side Table", "Ottoman",
                  "Leather Sectional Sofa L-Shape", "Electric Recliner Sofa",
                  "Home Theater Recliner Set 3pc"],
        "colors": ["Black", "Grey", "Brown", "White", "Blue"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (120, 5500),
        "brands": BRANDS_FURNITURE,
    },
    68: {  # Bedroom Furniture
        "specs": ["Queen Bed Frame", "King Bed Frame", "Nightstand",
                  "Dresser 6-Drawer", "Wardrobe", "Mattress Queen",
                  "Mattress King", "Vanity Table",
                  "Smart Adjustable Bed Frame King", "Luxury Memory Foam Mattress King"],
        "colors": ["Black", "White", "Brown", "Grey"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (120, 5000),
        "brands": BRANDS_FURNITURE,
    },
    69: {  # Office Furniture
        "specs": ["Executive Desk", "Standing Desk", "Ergonomic Chair",
                  "Filing Cabinet", "Bookcase", "Monitor Arm",
                  "Desk Lamp LED",
                  "Motorized Standing Desk 72in", "Executive Leather Chair"],
        "colors": ["Black", "White", "Grey", "Brown"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (60, 2800),
        "brands": BRANDS_FURNITURE,
    },
    70: {  # Rugs & Carpets
        "specs": ["Area Rug 5x7", "Area Rug 8x10", "Runner Rug 2x8",
                  "Shag Rug 4x6", "Outdoor Rug 6x9", "Door Mat"],
        "colors": ["Grey", "Blue", "Brown", "Red", "Green"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (20, 500),
        "brands": BRANDS_FURNITURE,
    },
    71: {  # Home Décor
        "specs": ["Wall Art Canvas", "Throw Pillow Set", "Table Lamp",
                  "Floor Lamp", "Decorative Vase", "Wall Mirror",
                  "Photo Frame Set", "Candle Set 3pk"],
        "colors": ["Black", "White", "Gold", "Silver", "Blue"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (10, 200),
        "brands": BRANDS_FURNITURE,
    },

    # --- Sports & Outdoors (cat 13) ---
    72: {  # Fitness Equipment
        "specs": ["Adjustable Dumbbell Set", "Yoga Mat", "Resistance Bands Set",
                  "Jump Rope", "Pull-Up Bar", "Kettlebell 20lb",
                  "Exercise Ball 65cm", "Foam Roller",
                  "Smart Treadmill", "Indoor Cycling Bike",
                  "Home Gym Multi-Station", "Rowing Machine"],
        "colors": ["Black", "Blue", "Grey", "Red"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (15, 3500),
        "brands": BRANDS_SPORTS + _CROSSOVER_ATHLETIC,
    },
    73: {  # Camping & Hiking
        "specs": ["2-Person Tent", "Sleeping Bag 20F", "Hiking Backpack 40L",
                  "Camping Stove", "Headlamp 300lm", "Trekking Poles",
                  "Water Filter", "Camp Chair",
                  "4-Season Expedition Tent", "Hardshell Cooler 65qt"],
        "colors": ["Green", "Black", "Blue", "Orange"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (20, 900),
        "brands": BRANDS_SPORTS + ["Nike", "Patagonia"],
    },
    74: {  # Cycling
        "specs": ["Road Bike Helmet", "Bike Lock U-Style", "Cycling Gloves",
                  "Bike Pump Floor", "Water Bottle Cage", "Rear Bike Light",
                  "Saddle Bag", "Cycling Jersey"],
        "colors": ["Black", "White", "Red", "Yellow"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (8, 180),
        "brands": BRANDS_SPORTS + _CROSSOVER_ATHLETIC,
    },
    75: {  # Water Sports
        "specs": ["Swim Goggles", "Snorkel Set", "Life Jacket Adult",
                  "Dry Bag 20L", "Paddleboard Paddle", "Rash Guard",
                  "Beach Towel XL"],
        "colors": ["Black", "Blue", "Red", "Yellow"],
        "classes": ["Economy", "Regular"],
        "price_range": (10, 200),
        "brands": BRANDS_SPORTS + ["Nike", "Adidas"],
    },
    76: {  # Team Sports
        "specs": ["Soccer Ball Size 5", "Basketball Official", "Football",
                  "Baseball Glove", "Tennis Racket", "Volleyball Net",
                  "Shin Guards", "Sports Bag"],
        "colors": ["White", "Black", "Orange", "Yellow"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (8, 250),
        "brands": BRANDS_SPORTS + _CROSSOVER_ATHLETIC,
    },

    # --- Office & Stationery (cat 14) ---
    77: {  # Paper & Notebooks
        "specs": ["Copy Paper 500 sheets", "Legal Pad 3pk", "Spiral Notebook A4",
                  "Sticky Notes 12pk", "Index Cards 200ct", "Graph Paper Pad",
                  "Composition Book 3pk"],
        "colors": ["White", "Yellow"],
        "classes": ["Economy", "Regular"],
        "price_range": (2, 25),
        "brands": BRANDS_OFFICE,
    },
    78: {  # Writing Instruments
        "specs": ["Ballpoint Pen 12pk", "Gel Pen Set 8pk", "Mechanical Pencil 4pk",
                  "Highlighter Set 6pk", "Permanent Marker 10pk",
                  "Fountain Pen", "Whiteboard Marker 4pk"],
        "colors": ["Black", "Blue", "Red"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (2, 35),
        "brands": BRANDS_OFFICE,
    },
    79: {  # Desk Organizers
        "specs": ["Desktop File Sorter", "Pen Holder Cup", "Desk Tray 3-Tier",
                  "Cable Management Box", "Monitor Stand Riser",
                  "Drawer Organizer Set"],
        "colors": ["Black", "White", "Grey"],
        "classes": ["Economy", "Regular"],
        "price_range": (8, 60),
        "brands": BRANDS_OFFICE,
    },
    80: {  # Bags & Cases
        "specs": ["Laptop Sleeve 15in", "Messenger Bag", "Rolling Briefcase",
                  "Backpack 25L", "Document Portfolio", "Tablet Case 10in"],
        "colors": ["Black", "Grey", "Brown", "Blue"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (15, 180),
        "brands": BRANDS_OFFICE + ["Nike", "Adidas", "Under Armour", "The North Face", "Patagonia"],
    },

    # --- Garden & Outdoor Living (cat 15) ---
    81: {  # Power Tools
        "specs": ["Cordless Drill 20V", "Circular Saw 7in", "Impact Driver 20V",
                  "Reciprocating Saw", "Angle Grinder 4.5in", "Jigsaw",
                  "Orbital Sander",
                  "Table Saw 10in", "Miter Saw Sliding 12in", "Air Compressor 6gal"],
        "colors": ["Yellow", "Red", "Blue", "Black"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (40, 900),
        "brands": BRANDS_GARDEN,
    },
    82: {  # Hand Tools
        "specs": ["Hammer 16oz", "Screwdriver Set 10pc", "Tape Measure 25ft",
                  "Pliers Set 3pc", "Wrench Set 8pc", "Utility Knife",
                  "Level 24in", "Socket Set 40pc"],
        "colors": ["Red", "Yellow", "Black", "Blue"],
        "classes": ["Economy", "Regular"],
        "price_range": (5, 80),
        "brands": BRANDS_GARDEN,
    },
    83: {  # Outdoor Furniture
        "specs": ["Patio Dining Set 5pc", "Adirondack Chair", "Patio Umbrella 9ft",
                  "Outdoor Bench", "Hammock with Stand", "Lounge Chair",
                  "Premium Patio Dining Set 9pc", "Pergola with Canopy"],
        "colors": ["Brown", "White", "Grey", "Green"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (60, 2200),
        "brands": BRANDS_GARDEN + ["IKEA", "Pottery Barn", "Crate & Barrel", "West Elm"],
    },
    84: {  # Grills & BBQ
        "specs": ["Gas Grill 3-Burner", "Charcoal Grill 22in", "Portable Grill",
                  "Smoker Vertical", "Grill Tool Set 18pc", "Grill Cover",
                  "Built-In Gas Grill 6-Burner", "Pellet Smoker & Grill"],
        "colors": ["Black", "Silver", "Red"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (30, 2200),
        "brands": BRANDS_GARDEN,
    },
    85: {  # Garden Care
        "specs": ["Garden Hose 50ft", "Pruning Shears", "Lawn Mower Electric",
                  "Leaf Blower Cordless", "Sprinkler System", "Wheelbarrow",
                  "Garden Gloves 3pk"],
        "colors": ["Green", "Black", "Yellow", "Orange"],
        "classes": ["Economy", "Regular"],
        "price_range": (8, 400),
        "brands": BRANDS_GARDEN + ["Bosch"],
    },

    # --- Automotive (cat 16) ---
    86: {  # Car Care & Cleaning
        "specs": ["Car Wash Soap 1L", "Tire Shine Spray", "Interior Cleaner 500ml",
                  "Microfiber Cloth 6pk", "Car Wax 400g", "Glass Cleaner 500ml",
                  "Detailing Kit"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (5, 50),
        "brands": BRANDS_AUTO,
    },
    87: {  # Interior Accessories
        "specs": ["Seat Covers Universal", "Floor Mats 4pc", "Steering Wheel Cover",
                  "Sun Shade Windshield", "Phone Mount Magnetic",
                  "Trunk Organizer", "Air Freshener 3pk"],
        "colors": ["Black", "Grey", "Brown"],
        "classes": ["Economy", "Regular"],
        "price_range": (5, 80),
        "brands": BRANDS_AUTO,
    },
    88: {  # Car Electronics
        "specs": ["Dash Cam 1080p", "Bluetooth FM Transmitter", "USB Car Charger",
                  "Tire Pressure Monitor", "Jump Starter Portable",
                  "LED Headlight Bulbs 2pk"],
        "colors": ["Black"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (10, 200),
        "brands": BRANDS_AUTO,
    },
    89: {  # Tires & Parts
        "specs": ["All-Season Tire 205/55R16", "All-Season Tire 225/65R17",
                  "Wiper Blades 2pk", "Oil Filter", "Air Filter",
                  "Brake Pads Front", "Battery 12V"],
        "colors": ["Black"],
        "classes": ["Economy", "Regular"],
        "price_range": (8, 250),
        "brands": BRANDS_AUTO,
    },

    # =================================================================
    # Synthetic brands in Contoso subcategories (keys 1-48)
    # Real-world electronics/appliance brands competing alongside
    # Fabrikam, Litware, and other original Contoso brands.
    # =================================================================

    # --- Audio (cat 1) ---
    1: {  # MP4 & MP3 / Portable Audio
        "specs": ["Portable Speaker", "Smart Speaker", "Party Speaker",
                  "Wireless Speaker Mini", "Bluetooth Speaker Rugged"],
        "colors": ["Black", "White", "Blue", "Grey"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (30, 400),
        "brands": ["Sony", "Bose"],
    },
    6: {  # Bluetooth Headphones
        "specs": ["Wireless Earbuds", "Over-Ear NC Headphones",
                  "Sport Earbuds", "Open-Ear Buds", "Studio Headphones",
                  "Wireless Earbuds Pro"],
        "colors": ["Black", "White", "Blue", "Silver"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (30, 500),
        "brands": ["Samsung", "Sony", "Apple", "Bose"],
    },

    # --- TV and Video (cat 2) ---
    9: {  # Televisions
        "specs": ["55\" 4K OLED TV", "65\" QLED Smart TV", "75\" 4K Smart TV",
                  "43\" LED TV", "50\" UHD TV", "85\" 8K TV",
                  "55\" QLED TV", "65\" OLED TV"],
        "colors": ["Black", "Silver", "White"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (200, 3500),
        "brands": ["Samsung", "Sony", "LG"],
    },
    11: {  # Home Theater System
        "specs": ["Soundbar 5.1", "AV Receiver 7.2", "Surround System 7.1",
                  "Soundbar 3.1", "Wireless Subwoofer",
                  "Soundbar Atmos", "Compact Soundbar 2.1"],
        "colors": ["Black", "Silver", "White"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (100, 2500),
        "brands": ["Samsung", "Sony", "LG", "Bose"],
    },

    # --- Computers (cat 3) ---
    15: {  # Laptops
        "specs": ["Laptop 14\" i5", "Laptop 15\" i7", "Laptop 13\" Ultrabook",
                  "Laptop 16\" Creator", "Laptop 17\" Workstation",
                  "Laptop 14\" AMD Ryzen", "Laptop 15\" OLED"],
        "colors": ["Silver", "Black", "White", "Blue"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (400, 2800),
        "brands": ["Samsung", "Apple", "HP"],
    },
    17: {  # Desktops
        "specs": ["Desktop Tower i7", "All-in-One 27\"", "Mini PC",
                  "Desktop Workstation", "All-in-One 24\"",
                  "Desktop Tower i9"],
        "colors": ["Silver", "Black", "White"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (500, 3200),
        "brands": ["Apple", "HP"],
    },
    18: {  # Monitors
        "specs": ["Monitor 27\" 4K", "Monitor 32\" Curved", "Monitor 24\" FHD",
                  "Ultrawide 34\"", "Monitor 27\" 144Hz",
                  "Monitor 32\" 4K", "Monitor 49\" Super Ultrawide"],
        "colors": ["Black", "Silver", "White"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (150, 1200),
        "brands": ["Samsung", "LG", "HP"],
    },
    20: {  # Printers, Scanners & Fax
        "specs": ["LaserJet Mono", "LaserJet Color", "InkJet All-in-One",
                  "LaserJet Pro MFP", "Portable Printer",
                  "Tank Printer", "Wide Format Printer"],
        "colors": ["White", "Black", "Grey"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (80, 600),
        "brands": ["HP"],
    },
    22: {  # Computers Accessories
        "specs": ["Wireless Mouse", "Mechanical Keyboard", "USB-C Hub",
                  "Webcam 4K", "External SSD 1TB", "Laptop Stand",
                  "Wireless Keyboard & Mouse Combo", "Portable Monitor 15\""],
        "colors": ["Black", "White", "Silver", "Grey"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (15, 250),
        "brands": ["Samsung", "Apple", "HP"],
    },

    # --- Cameras and camcorders (cat 4) ---
    23: {  # Digital Cameras
        "specs": ["Mirrorless Camera A7", "Compact Camera RX",
                  "Action Camera", "Vlog Camera ZV",
                  "Point & Shoot WX"],
        "colors": ["Black", "Silver"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (300, 2500),
        "brands": ["Sony"],
    },
    24: {  # Digital SLR Cameras
        "specs": ["DSLR Body Alpha", "Full Frame Kit 28-70mm",
                  "APS-C Body", "Pro Body Flagship"],
        "colors": ["Black"],
        "classes": ["Deluxe"],
        "price_range": (800, 3500),
        "brands": ["Sony"],
    },
    27: {  # Camcorders
        "specs": ["Handycam 4K", "Professional Camcorder",
                  "Action Cam Mini", "Cinema Camera"],
        "colors": ["Black", "Silver"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (300, 2000),
        "brands": ["Sony"],
    },

    # --- Cell phones (cat 5) ---
    32: {  # Smart phones & PDAs
        "specs": ["Smartphone Pro Max", "Smartphone Ultra",
                  "Smartphone SE", "Smartphone Flip",
                  "Smartphone Plus", "Smartphone Mini",
                  "Smartphone FE"],
        "colors": ["Black", "White", "Blue", "Purple", "Green", "Silver"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (400, 1800),
        "brands": ["Samsung", "Apple"],
    },
    31: {  # Touch Screen Phones (tablets)
        "specs": ["Tablet 10\" WiFi", "Tablet 12\" 5G",
                  "Tablet 8\" Lite", "Tablet 11\" Pro",
                  "Tablet 10\" with Keyboard"],
        "colors": ["Black", "Silver", "White", "Blue"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (200, 1200),
        "brands": ["Samsung", "Apple"],
    },

    # --- Games (cat 7) ---
    39: {  # Download Games
        "specs": ["Console Game Action", "Console Game RPG",
                  "Console Game Sports", "Console Game Racing",
                  "Console Game Adventure", "Console Game Strategy",
                  "Console Game Simulation"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular"],
        "price_range": (15, 70),
        "brands": ["Sony"],
    },

    # --- Home Appliances (cat 8) ---
    41: {  # Washers & Dryers
        "specs": ["Front Load Washer", "Top Load Washer",
                  "Washer-Dryer Combo", "Stackable Set",
                  "Compact Washer", "Steam Washer"],
        "colors": ["White", "Silver", "Black"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (500, 2500),
        "brands": ["Samsung", "LG"],
    },
    42: {  # Refrigerators
        "specs": ["French Door Fridge", "Side-by-Side Fridge",
                  "Top Freezer Fridge", "Mini Fridge",
                  "Counter Depth Fridge", "4-Door Flex Fridge",
                  "Bottom Freezer Fridge"],
        "colors": ["Silver", "Black", "White"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (400, 3500),
        "brands": ["Samsung", "LG"],
    },
    43: {  # Microwaves
        "specs": ["Countertop Microwave", "Over-Range Microwave",
                  "Microwave Drawer", "Convection Microwave",
                  "Compact Microwave"],
        "colors": ["Silver", "Black", "White"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (80, 600),
        "brands": ["Samsung", "LG"],
    },
    47: {  # Air Conditioners
        "specs": ["Window AC 8000BTU", "Portable AC 12000BTU",
                  "Mini Split Inverter", "Smart AC Unit",
                  "Window AC 12000BTU"],
        "colors": ["White", "Silver"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (200, 1500),
        "brands": ["Samsung", "LG"],
    },
    48: {  # Fans
        "specs": ["Tower Fan Smart", "Bladeless Fan",
                  "Air Purifier Fan", "Ceiling Fan Smart",
                  "Pedestal Fan"],
        "colors": ["White", "Black", "Silver"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (50, 600),
        "brands": ["LG"],
    },

    # =================================================================
    # Backfill for ContosoRetailDW subcategories that shipped with no
    # products in the source data.  Each was empty in both
    # contoso_products.parquet and the synthetic catalog, so no sales
    # could ever reference them.  Templates use modern real-world brands
    # and price bands aligned with current retail.
    # =================================================================

    # --- Audio (cat 1) ---
    2: {  # Recorder — voice / field / dictation recorders
        "specs": ["Digital Voice Recorder", "Pocket Dictation Recorder",
                  "Stereo Field Recorder", "Handheld Audio Recorder",
                  "Lavalier Recorder Kit", "Podcast Recorder 4-Track"],
        "colors": ["Black", "Silver"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (30, 500),
        "brands": ["Sony", "Olympus", "Tascam", "Zoom", "Philips"],
    },
    3: {  # Radio — portable / clock / shortwave / tabletop
        "specs": ["Portable AM/FM Radio", "Clock Radio with USB",
                  "Shortwave World Receiver", "DAB+ Digital Radio",
                  "Emergency Hand-Crank Radio", "Tabletop Bluetooth Radio"],
        "colors": ["Black", "White", "Silver", "Walnut", "Red"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (18, 400),
        "brands": ["Sony", "Panasonic", "Sangean", "Bose", "Tivoli"],
    },
    5: {  # Headphones — wired / studio / audiophile (sub 6 covers BT)
        "specs": ["Studio Monitor Headphones", "Open-Back Audiophile Headphones",
                  "Closed-Back Reference Headphones", "Wired In-Ear Monitors",
                  "DJ Headphones", "Planar Magnetic Headphones",
                  "Wired Gaming Headset"],
        "colors": ["Black", "Silver", "White", "Red"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (35, 700),
        "brands": ["Sony", "Sennheiser", "Audio-Technica", "Beyerdynamic",
                   "AKG", "Shure"],
    },
    7: {  # Speakers — bookshelf / tower / portable / smart
        "specs": ["Bookshelf Speakers Pair", "Floorstanding Tower Speakers",
                  "Center Channel Speaker", "Bluetooth Portable Speaker",
                  "Smart Wi-Fi Speaker", "Outdoor Patio Speakers Pair",
                  "Powered Subwoofer 10in"],
        "colors": ["Black", "White", "Walnut", "Grey"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (45, 2200),
        "brands": ["JBL", "Bose", "Sony", "Sonos", "Klipsch", "KEF"],
    },
    8: {  # Audio Accessories — cables / stands / DACs / adapters
        "specs": ["3.5mm Audio Cable 6ft", "Optical Toslink Cable 6ft",
                  "Headphone Stand Aluminum", "Bluetooth Audio Receiver",
                  "RCA Stereo Cable 6ft", "Banana Plug Connectors 12pk",
                  "Headphone Splitter 4-Way", "USB DAC Headphone Amplifier",
                  "Speaker Wire 14AWG 50ft"],
        "colors": ["Black", "Silver", "White"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (5, 300),
        "brands": ["Anker", "Belkin", "AudioQuest", "Mediabridge", "FiiO",
                   "Monoprice"],
    },

    # --- TV and Video (cat 2) ---
    14: {  # TV & Video Accessories — mounts / cables / streaming / remotes
        "specs": ["TV Wall Mount Tilt 32-65in",
                  "TV Wall Mount Full Motion 55-90in",
                  "HDMI 2.1 Cable 6ft", "Streaming Stick 4K HDR",
                  "Universal Remote Control", "TV Antenna Indoor HD",
                  "Coaxial Cable 25ft", "Swivel TV Stand 55in",
                  "Streaming Box 4K"],
        "colors": ["Black", "White", "Silver"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (8, 300),
        "brands": ["Roku", "Amazon", "Logitech", "Sanus", "Anker",
                   "Mediabridge", "Mohu"],
    },

    # --- Computers (cat 3) ---
    16: {  # Netbooks — Chromebooks / mini laptops / convertibles
        "specs": ["Chromebook 11.6in", "Chromebook 14in Touchscreen",
                  "Convertible 2-in-1 Chromebook 11in",
                  "Mini Laptop 12in", "Cloudbook 14in",
                  "Education Chromebook 11in Rugged"],
        "colors": ["Silver", "Black", "White", "Blue"],
        "classes": ["Economy", "Regular"],
        "price_range": (180, 700),
        "brands": ["Acer", "Asus", "HP", "Lenovo", "Samsung"],
    },
    21: {  # Computer Setup & Service — house-brand services
        "specs": ["In-Home Computer Setup", "Data Migration & Transfer Service",
                  "Virus Removal & Cleanup", "Operating System Reinstall",
                  "Annual Tech Support Plan", "Extended Warranty 2-Year",
                  "Hardware Diagnostic Service", "Home Network Setup"],
        "colors": ["N/A"],
        "classes": ["Regular", "Deluxe"],
        "price_range": (40, 400),
        "brands": ["ProTech Services", "TechAssist", "GeekCare"],
    },

    # --- Cameras and camcorders (cat 4) ---
    25: {  # Film Cameras — instant / 35mm / disposable
        "specs": ["Instant Photo Camera Mini", "Wide-Format Instant Camera",
                  "35mm Point & Shoot Camera", "35mm SLR Film Camera",
                  "Disposable Camera 3-Pack", "Half-Frame 35mm Camera",
                  "Instant Photo Camera Square"],
        "colors": ["Black", "White", "Pink", "Blue", "Mint", "Beige"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (15, 400),
        "brands": ["Fujifilm", "Kodak", "Lomography", "Polaroid", "Ilford"],
    },

    # --- Music, Movies and Audio Books (cat 6) ---
    34: {  # Music CD — albums / compilations / box sets
        "specs": ["Greatest Hits Album", "Live Concert Album",
                  "Classical Symphony Collection", "Movie Soundtrack Album",
                  "Holiday Music Album", "Jazz Standards Collection",
                  "Rock Anthology 2-Disc Set", "Pop Hits Compilation",
                  "Deluxe Edition Box Set"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (8, 50),
        "brands": ["Universal Music", "Sony Music", "Warner Music",
                   "Capitol Records", "Decca", "Blue Note"],
    },
    36: {  # Audio Books — CD and digital format
        "specs": ["Audio Book Bestseller Novel", "Audio Book Biography",
                  "Audio Book Self-Help", "Audio Book Mystery & Thriller",
                  "Audio Book Classic Literature",
                  "Audio Book Business & Finance",
                  "Audio Book Sci-Fi Series", "Audio Book Children's Collection",
                  "Audio Book History"],
        "colors": ["N/A"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (10, 60),
        "brands": ["Audible Studios", "HarperAudio",
                   "Penguin Random House Audio", "Macmillan Audio",
                   "Hachette Audio", "Simon & Schuster Audio"],
    },

    # --- Games and Toys (cat 7) ---
    40: {  # Games Accessories — controllers / headsets / docks / peripherals
        "specs": ["Wireless Controller", "Gaming Headset Wireless",
                  "Charging Dock Dual", "Racing Wheel & Pedals",
                  "Arcade Fight Stick", "Console Carrying Case",
                  "External Game Storage 1TB", "Mechanical Gaming Keyboard",
                  "RGB Gaming Mouse"],
        "colors": ["Black", "White", "Red", "Blue"],
        "classes": ["Economy", "Regular", "Deluxe"],
        "price_range": (18, 350),
        "brands": ["Logitech", "Razer", "SteelSeries", "8BitDo", "HyperX",
                   "Sony", "Microsoft", "PowerA"],
    },
}

# Pre-built lookup: SubcategoryKey → SubcategoryLabel
# Covers both Contoso (from parquet) and Synthetic (from NEW_SUBCATEGORIES)
_SUBCAT_KEY_TO_LABEL = {sk: sl for sk, sl, _, _ in NEW_SUBCATEGORIES}
if DATA_DIR.joinpath("product_subcategory.parquet").exists():
    _contoso_subs = pd.read_parquet(DATA_DIR / "product_subcategory.parquet")
    for _, _row in _contoso_subs.iterrows():
        _sk = int(_row["SubcategoryKey"])
        if _sk not in _SUBCAT_KEY_TO_LABEL and "SubcategoryLabel" in _row.index:
            _SUBCAT_KEY_TO_LABEL[_sk] = str(_row["SubcategoryLabel"])
    del _contoso_subs


# -----------------------------------------------------------------------
# Stock type assignment based on price position within subcategory
# -----------------------------------------------------------------------
def _assign_stock_type(prices: np.ndarray) -> tuple[list[str], list[str]]:
    """Assign StockType/StockTypeCode based on price tercile within group."""
    n = len(prices)
    if n == 0:
        return [], []

    ranks = prices.argsort().argsort()  # rank by price
    tercile = (ranks * 3) // n  # 0=low, 1=mid, 2=high

    codes = np.where(tercile == 2, "1", np.where(tercile == 1, "2", "3"))
    types = np.where(tercile == 2, "High", np.where(tercile == 1, "Mid", "Low"))
    return types.tolist(), codes.tolist()


def _generate_products_for_subcategory(
    subcat_key: int,
    template: dict,
    rng: np.random.Generator,
    start_key: int,
) -> tuple[pd.DataFrame, int]:
    """Generate product rows for one subcategory from its template."""
    specs = template["specs"]
    colors = template["colors"]
    classes = template["classes"]
    brands = template["brands"]
    price_min, price_max = template["price_range"]

    rows = []
    pk = start_key

    subcat_label = _SUBCAT_KEY_TO_LABEL[subcat_key]

    for spec in specs:
        # Multiple brands carry the same product type (like real retail).
        # Pick at least 2 brands, up to all of them (use all when ≤2 brands).
        if len(brands) <= 2:
            chosen_brands = list(brands)
        else:
            n_brands = rng.integers(max(2, len(brands) // 2), len(brands) + 1)
            chosen_brands = rng.choice(brands, size=n_brands, replace=False).tolist()

        for brand in chosen_brands:
            # Each brand-spec combo comes in a subset of colors
            n_colors = rng.integers(1, len(colors) + 1)
            chosen_colors = rng.choice(colors, size=n_colors, replace=False).tolist()

            cls = classes[rng.integers(0, len(classes))]

            # Tiers overlap so mid-range products appear across classes
            span = price_max - price_min
            if cls == "Economy":
                base = rng.uniform(price_min, price_min + span * 0.30)
            elif cls == "Deluxe":
                base = rng.uniform(price_min + span * 0.45, price_max)
            else:  # Regular
                base = rng.uniform(price_min, price_min + span * 0.65)
            base = float(np.clip(base, price_min, price_max))

            cost_ratio = rng.uniform(0.40, 0.75)
            unit_cost = round(base * cost_ratio, 2)
            unit_price = round(base, 2)

            for color in chosen_colors:
                if color == "N/A":
                    name = f"{brand} {spec}"
                else:
                    name = f"{brand} {spec} {color}"

                code = f"{subcat_label}{str(pk).zfill(4)[-4:]}"

                rows.append({
                    "ProductKey": pk,
                    "ProductCode": code,
                    "ProductName": name,
                    "ProductDescription": f"{brand} {spec}",
                    "ProductSubcategoryKey": subcat_key,
                    "Brand": brand,
                    "Class": cls,
                    "Color": color,
                    "UnitCost": unit_cost,
                    "UnitPrice": unit_price,
                })
                pk += 1

    df = pd.DataFrame(rows)

    # Assign stock type based on price position
    if len(df) > 0:
        prices = df["UnitPrice"].to_numpy(dtype=np.float64)
        stock_types, stock_codes = _assign_stock_type(prices)
        df["StockType"] = stock_types
        df["StockTypeCode"] = stock_codes

    return df, pk


def main():
    rng = np.random.default_rng(42)

    # --- Load existing data (keep only Contoso originals) ---
    cats = pd.read_parquet(DATA_DIR / "product_category.parquet")
    subs = pd.read_parquet(DATA_DIR / "product_subcategory.parquet")
    prods = pd.read_parquet(DATA_DIR / "products.parquet")

    # Strip any previously generated synthetic rows so the script is idempotent.
    # Source column only lives on products (not category/subcategory).
    if "Source" in prods.columns:
        prods = prods[prods["Source"] == "Contoso"].copy()
    else:
        prods["Source"] = "Contoso"

    # For category/subcategory, use key ranges to identify originals
    cats = cats[cats["CategoryKey"] <= 8].copy()
    subs = subs[subs["SubcategoryKey"] <= 48].copy()
    # Drop Source column if it leaked in from a previous run
    for tbl in (cats, subs):
        if "Source" in tbl.columns:
            tbl.drop(columns=["Source"], inplace=True)

    print(f"Contoso base: {len(cats)} categories, {len(subs)} subcategories, {len(prods)} products")

    # --- Generate new categories ---
    new_cat_rows = []
    for key, name, label in NEW_CATEGORIES:
        new_cat_rows.append({
            "CategoryKey": key,
            "Category": name,
            "CategoryLabel": label,
        })
    new_cats = pd.DataFrame(new_cat_rows)

    # --- Generate new subcategories ---
    new_sub_rows = []
    for key, label, name, cat_key in NEW_SUBCATEGORIES:
        new_sub_rows.append({
            "SubcategoryKey": key,
            "SubcategoryLabel": label,
            "Subcategory": name,
            "CategoryKey": cat_key,
        })
    new_subs = pd.DataFrame(new_sub_rows)

    # --- Generate new products ---
    start_key = int(prods["ProductKey"].max()) + 1
    all_new_products = []

    for subcat_key, template in TEMPLATES.items():
        df, start_key = _generate_products_for_subcategory(
            subcat_key, template, rng, start_key,
        )
        all_new_products.append(df)

    new_prods = pd.concat(all_new_products, ignore_index=True)
    new_prods["Source"] = "Synthetic"

    # --- Merge ---
    final_cats = pd.concat([cats, new_cats], ignore_index=True)
    final_subs = pd.concat([subs, new_subs], ignore_index=True)
    final_prods = pd.concat([prods, new_prods], ignore_index=True)

    # Ensure consistent dtypes
    final_cats["CategoryKey"] = final_cats["CategoryKey"].astype("int64")
    final_subs["SubcategoryKey"] = final_subs["SubcategoryKey"].astype("int64")
    final_subs["CategoryKey"] = final_subs["CategoryKey"].astype("int64")
    final_prods["ProductKey"] = final_prods["ProductKey"].astype("int64")
    final_prods["ProductSubcategoryKey"] = final_prods["ProductSubcategoryKey"].astype("int64")

    # --- Write ---
    final_cats.to_parquet(DATA_DIR / "product_category.parquet", index=False)
    final_subs.to_parquet(DATA_DIR / "product_subcategory.parquet", index=False)
    # Split source files for catalog selection (contoso / synthetic / all)
    contoso_prods = final_prods[final_prods["Source"] == "Contoso"].copy()
    synthetic_prods = final_prods[final_prods["Source"] == "Synthetic"].copy()
    contoso_prods.to_parquet(DATA_DIR / "contoso_products.parquet", index=False)
    synthetic_prods.to_parquet(DATA_DIR / "synthetic_products.parquet", index=False)
    # Combined file for backward compatibility
    final_prods.to_parquet(DATA_DIR / "products.parquet", index=False)

    n_new = len(new_prods)
    n_total = len(final_prods)
    n_new_subs = len(new_subs)
    n_new_cats = len(new_cats)

    print(f"\nAdded: {n_new_cats} categories, {n_new_subs} subcategories, {n_new} products")
    print(f"Total: {len(final_cats)} categories, {len(final_subs)} subcategories, {n_total} products")

    # Summary by new category
    merged = new_prods.merge(
        new_subs[["SubcategoryKey", "Subcategory", "CategoryKey"]],
        left_on="ProductSubcategoryKey", right_on="SubcategoryKey", how="left",
    ).merge(
        new_cats[["CategoryKey", "Category"]],
        on="CategoryKey", how="left",
    )
    summary = merged.groupby(["Category", "Subcategory"]).agg(
        Count=("ProductKey", "count"),
        MinPrice=("UnitPrice", "min"),
        MaxPrice=("UnitPrice", "max"),
    ).reset_index()
    print(f"\nNew products by subcategory:")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
