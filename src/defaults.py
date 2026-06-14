"""Centralized hardcoded constants for dimension and fact generators.

All domain constants that were previously scattered across individual
generator modules are collected here.  Grouped by domain so that
each generator can import only what it needs.

IMPORTANT: values must stay exactly in sync with what was previously
inline — changing a value here changes generation output.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from src.exceptions import ValidationError

# ---------------------------------------------------------------------------
# SCD2 sentinel date for IsCurrent=1 rows.
# NOTE: datetime64[ns] overflows at ~2262, so 9999-12-31 silently wraps to
# 1816-03-29.  Use 2099-12-31 as a safe far-future sentinel.
# ---------------------------------------------------------------------------
SCD2_END_OF_TIME = pd.Timestamp("2099-12-31")


# =================================================================
#  STORE_DEFAULTS
# =================================================================

STORE_TYPES = np.array(
    ["Supermarket", "Convenience", "Online", "Hypermarket"], dtype=object
)
STORE_STATUS = np.array(["Open", "Closed", "Renovating"], dtype=object)
STORE_CLOSE_REASONS = np.array(
    ["Low Sales", "Lease Ended", "Renovation", "Moved Location"], dtype=object
)

STORE_TYPES_P = np.array([0.50, 0.30, 0.10, 0.10], dtype=float)
STORE_STATUS_P = np.array([0.85, 0.10, 0.05], dtype=float)

STORE_BRANDS = np.array(
    [
        "Northwind Market", "Contoso Mart", "Fabrikam Foods", "Woodgrove Grocers",
        "Adventure Works Retail", "Tailspin Superstores", "Wingtip Fresh", "Proseware Market",
        "CitySquare Grocers", "Harborview Market", "Summit Retail", "BlueSky Foods",
    ],
    dtype=object,
)

STORE_AREAS = np.array(
    [
        "Downtown", "Uptown", "Midtown", "Riverside", "Lakeside", "Hillcrest",
        "Old Town", "West End", "Eastside", "Northgate", "Southpark", "Harbor",
        "Airport", "Market District", "Central", "University",
    ],
    dtype=object,
)

STORE_MANAGER_FIRST = np.array(
    [
        "James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Charles",
        "Christopher","Daniel","Matthew","Anthony","Mark","Steven","Paul","Andrew","Joshua","Ryan",
        "Mary","Patricia","Jennifer","Linda","Elizabeth","Barbara","Susan","Jessica","Sarah","Karen",
        "Nancy","Lisa","Margaret","Sandra","Ashley","Kimberly","Emily","Donna","Michelle","Laura",
        "Alex","Jordan","Taylor","Casey","Morgan","Riley","Jamie","Avery","Cameron","Quinn",
    ],
    dtype=object,
)

STORE_MANAGER_LAST = np.array(
    [
        "Smith","Johnson","Williams","Brown","Jones","Miller","Davis","Wilson","Anderson","Thomas",
        "Taylor","Moore","Jackson","Martin","Lee","Perez","Thompson","White","Harris","Clark",
        "Lewis","Robinson","Walker","Young","Allen","King","Wright","Scott","Green","Baker",
        "Adams","Nelson","Hill","Campbell","Mitchell","Carter","Roberts","Turner","Phillips","Parker",
    ],
    dtype=object,
)

STORE_ONLINE_SUFFIX = np.array(
    ["Online", "Digital", "E-Commerce", "Web Store", "Direct"],
    dtype=object,
)

# StoreFormat — choices and probabilities keyed by StoreType
STORE_FORMATS: dict[str, tuple[list[str], list[float]]] = {
    "Online":      (["Digital"],                                   [1.00]),
    "Hypermarket": (["Flagship", "Standard"],                      [0.30, 0.70]),
    "Supermarket": (["Flagship", "Standard", "Express"],           [0.10, 0.60, 0.30]),
    "Convenience": (["Standard", "Express", "Drive-Thru"],         [0.10, 0.50, 0.40]),
}
STORE_DEFAULT_FORMATS: tuple[list[str], list[float]] = (["Standard", "Express"], [0.50, 0.50])

# OwnershipType — choices and probabilities keyed by StoreType
STORE_OWNERSHIP_TYPES: dict[str, tuple[list[str], list[float]]] = {
    "Online":      (["Corporate", "Licensed"],              [0.70, 0.30]),
    "Hypermarket": (["Corporate", "Franchise", "Licensed"], [0.80, 0.15, 0.05]),
    "Supermarket": (["Corporate", "Franchise", "Licensed"], [0.50, 0.35, 0.15]),
    "Convenience": (["Corporate", "Franchise", "Licensed"], [0.30, 0.50, 0.20]),
}
STORE_DEFAULT_OWNERSHIP: tuple[list[str], list[float]] = (
    ["Corporate", "Franchise", "Licensed"], [0.50, 0.35, 0.15],
)

STORE_REVENUE_CLASSES = np.array(["A", "B", "C"], dtype=object)
STORE_REVENUE_CLASSES_P = np.array([0.20, 0.60, 0.20], dtype=float)

# Store-type staffing ranges: (min_staff, max_staff) per store type.
# Used by stores.py to set EmployeeCount; employees.py reads that column directly.
STORE_STAFFING_RANGES: Dict[str, Tuple[int, int]] = {
    "Supermarket":  (8, 20),
    "Hypermarket":  (15, 40),
    "Convenience":  (2, 6),
}
# Fallback for store types not listed above
STORE_STAFFING_DEFAULT: Tuple[int, int] = (2, 6)

# Minimum EmployeeCount for a physical store: 1 store manager + >= 1
# salesperson. Sales emits EmployeeKey=-1 (an orphan FK that breaks the
# generated SQL FK constraints) for any (store, date) with no eligible
# salesperson, so this is a hard floor. The "+1 salesperson" pairs with the
# manager reservation (``EmployeeCount - 1`` staff) in employees/generator.py;
# if that reservation changes, update this floor in lockstep. Online stores
# are exempt (fixed at 1 — they get a dedicated online sales representative).
MIN_PHYSICAL_EMPLOYEE_COUNT: int = 2

# Online store key ranges — easily distinguishable from physical stores
ONLINE_STORE_KEY_BASE: int = 10_000    # Online StoreKeys: 10_001, 10_002, ...
ONLINE_EMP_KEY_BASE: int = 50_000_000  # Online EmployeeKeys: 50_000_901, 50_000_902, ...
ONLINE_SALES_REP_ROLE: str = "Online Sales Representative"

# Warehouse dimension
ONLINE_WAREHOUSE_KEY: int = 9_000      # Dedicated online fulfillment warehouse
WAREHOUSE_TYPES: Tuple[str, ...] = (
    "Distribution Center",
    "Regional Hub",
    "Fulfillment Center",
)
WAREHOUSE_TYPES_P: Tuple[float, ...] = (0.50, 0.30, 0.20)

# Sub-national region labels for warehouse naming (US states -> region name)
US_STATE_REGIONS: Dict[str, str] = {
    "California": "West", "Washington": "West", "Oregon": "West",
    "Arizona": "West", "Colorado": "West", "Nevada": "West", "Utah": "West",
    "New York": "Northeast", "Massachusetts": "Northeast",
    "Pennsylvania": "Northeast", "New Jersey": "Northeast",
    "Connecticut": "Northeast",
    "Texas": "South", "Florida": "South", "Georgia": "South",
    "Virginia": "South", "North Carolina": "South", "Tennessee": "South",
    "Illinois": "Midwest", "Ohio": "Midwest", "Michigan": "Midwest",
    "Minnesota": "Midwest", "Wisconsin": "Midwest", "Indiana": "Midwest",
}

# Transfer share by CloseReason — higher for planned closures, lower for performance
STORE_CLOSE_TRANSFER_SHARE_BY_REASON: Dict[str, float] = {
    "Lease Ended": 0.80,
    "Moved Location": 0.90,
    "Renovation": 0.70,
    "Low Sales": 0.40,
}

# StoreZone derived from ISO/currency code
STORE_ISO_TO_ZONE: dict[str, str] = {
    "USD": "Americas",    "CAD": "Americas",    "MXN": "Americas",    "BRL": "Americas",
    "ARS": "Americas",    "CLP": "Americas",    "COP": "Americas",    "PEN": "Americas",
    "GBP": "Europe",      "EUR": "Europe",      "CHF": "Europe",      "SEK": "Europe",
    "NOK": "Europe",      "DKK": "Europe",      "PLN": "Europe",      "CZK": "Europe",
    "HUF": "Europe",      "RON": "Europe",
    "INR": "South Asia",
    "AUD": "Asia Pacific", "NZD": "Asia Pacific", "CNY": "Asia Pacific", "JPY": "Asia Pacific",
    "HKD": "Asia Pacific", "SGD": "Asia Pacific", "KRW": "Asia Pacific", "TWD": "Asia Pacific",
    "THB": "Asia Pacific", "IDR": "Asia Pacific", "PHP": "Asia Pacific", "MYR": "Asia Pacific",
}

# Brand -> email domain
STORE_BRAND_DOMAINS: dict[str, str] = {
    "Northwind Market":       "northwindmarket.com",
    "Contoso Mart":           "contosomart.com",
    "Fabrikam Foods":         "fabrikamfoods.com",
    "Woodgrove Grocers":      "woodgrovegrocers.com",
    "Adventure Works Retail": "adventureworks.com",
    "Tailspin Superstores":   "tailspinstores.com",
    "Wingtip Fresh":          "wingtipfresh.com",
    "Proseware Market":       "prosewaremarket.com",
    "CitySquare Grocers":     "citysquaregrocers.com",
    "Harborview Market":      "harborviewmarket.com",
    "Summit Retail":          "summitretail.com",
    "BlueSky Foods":          "blueskyfoods.com",
}

# EU country code rotation for phone generation
STORE_EU_COUNTRY_CODES = [33, 34, 39, 49, 31, 32, 41, 46, 47, 45]


# =================================================================
#  EMPLOYEE_DEFAULTS
# =================================================================

# Gender distribution for employees: {other, female, male}
# Thresholds: u < other → "O"; u < other + female → "F"; else → "M"
EMPLOYEE_GENDER_PROBS: Dict[str, float] = {
    "other": 0.02,
    "female": 0.49,
    "male": 0.49,
}
if abs(sum(EMPLOYEE_GENDER_PROBS.values()) - 1.0) > 1e-9:
    raise ValidationError(
        f"EMPLOYEE_GENDER_PROBS must sum to 1.0, got {sum(EMPLOYEE_GENDER_PROBS.values())}"
    )

# Part-time probability by role (Store Managers always full-time)
EMPLOYEE_PART_TIME_RATE_BY_ROLE: Dict[str, float] = {
    "Cashier": 0.45,
    "Sales Associate": 0.25,
    "Stock Associate": 0.15,
    "Customer Support": 0.10,
    "Fulfillment Associate": 0.10,
    "Store Manager": 0.0,
    "Online Sales Representative": 0.0,
}

# FTE values for part-time employees (chosen at hire)
EMPLOYEE_PART_TIME_FTE_VALUES = np.array([0.50, 0.75], dtype=np.float64)

# Termination reasons with weights
EMPLOYEE_TERMINATION_REASON_LABELS = np.array(
    ["Voluntary", "Involuntary", "Retirement", "Relocation"], dtype=object
)
EMPLOYEE_TERMINATION_REASON_PROBS = np.array([0.45, 0.30, 0.15, 0.10], dtype=np.float64)
if abs(EMPLOYEE_TERMINATION_REASON_PROBS.sum() - 1.0) > 1e-9:
    raise ValidationError(
        f"EMPLOYEE_TERMINATION_REASON_PROBS must sum to 1.0, got {EMPLOYEE_TERMINATION_REASON_PROBS.sum()}"
    )

# Transfer reasons for non-primary (away) assignments
EMPLOYEE_TRANSFER_REASON_LABELS = np.array(
    ["Seasonal Support", "New Store Opening", "Backfill", "Employee Request", "Performance"],
    dtype=object,
)
EMPLOYEE_TRANSFER_REASON_PROBS = np.array([0.30, 0.20, 0.25, 0.15, 0.10], dtype=np.float64)
if abs(EMPLOYEE_TRANSFER_REASON_PROBS.sum() - 1.0) > 1e-9:
    raise ValidationError(
        f"EMPLOYEE_TRANSFER_REASON_PROBS must sum to 1.0, got {EMPLOYEE_TRANSFER_REASON_PROBS.sum()}"
    )


# =================================================================
#  CUSTOMER_DEFAULTS
# =================================================================

CUSTOMER_PERSONAL_EMAIL_DOMAINS = np.array(
    ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]
)

CUSTOMER_MARITAL_STATUS_LABELS = np.array(["Married", "Single"])
CUSTOMER_MARITAL_STATUS_PROBS = np.array([0.55, 0.45])

CUSTOMER_EDUCATION_LABELS = np.array(["High School", "Bachelors", "Masters", "PhD"])
CUSTOMER_EDUCATION_PROBS = np.array([0.20, 0.50, 0.25, 0.05])

CUSTOMER_OCCUPATION_LABELS = np.array(
    ["Professional", "Clerical", "Skilled", "Service", "Executive"]
)
CUSTOMER_OCCUPATION_PROBS = np.array([0.50, 0.20, 0.15, 0.10, 0.05])

CUSTOMER_AGE_MIN_DAYS = 18 * 365
CUSTOMER_AGE_MAX_DAYS = 70 * 365
CUSTOMER_INCOME_MIN = 20_000
CUSTOMER_INCOME_MAX = 200_000
CUSTOMER_MAX_CHILDREN = 5  # exclusive upper bound for rng.integers

# Loyalty score component weights
CUSTOMER_LOYALTY_W_WEIGHT = 0.55
CUSTOMER_LOYALTY_W_TEMP = 0.30
CUSTOMER_LOYALTY_W_INCOME = 0.15

# Income model: lognormal base by education, scaled by occupation
CUSTOMER_EDUCATION_INCOME_PARAMS = {
    "High School": (10.50, 0.40),   # median ~$36K
    "Bachelors":   (10.80, 0.38),   # median ~$49K
    "Masters":     (11.00, 0.35),   # median ~$60K
    "PhD":         (11.20, 0.32),   # median ~$73K
}

CUSTOMER_OCCUPATION_INCOME_MULT = {
    "Executive":    1.55,
    "Professional": 1.20,
    "Skilled":      1.00,
    "Clerical":     0.85,
    "Service":      0.75,
}

CUSTOMER_INCOME_ROUND_TO = 1_000

# Derived demographic columns
CUSTOMER_AGE_GROUP_EDGES = np.array([25, 35, 45, 55, 65])
CUSTOMER_AGE_GROUP_LABELS = np.array(
    ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"], dtype=object
)

CUSTOMER_INCOME_GROUP_EDGES = np.array([35_000, 65_000, 110_000])
CUSTOMER_INCOME_GROUP_LABELS = np.array(["Low", "Mid", "High", "Premium"], dtype=object)

CUSTOMER_HOME_OWNERSHIP_LABELS = np.array(["Rent", "Mortgage", "Own"])
CUSTOMER_HOME_OWNERSHIP_PROBS_BY_INCOME = {
    "Low":     np.array([0.65, 0.25, 0.10]),
    "Mid":     np.array([0.35, 0.45, 0.20]),
    "High":    np.array([0.15, 0.45, 0.40]),
    "Premium": np.array([0.08, 0.32, 0.60]),
}

CUSTOMER_CONTACT_METHOD_LABELS = np.array(["Email", "Phone", "SMS", "Mail"])
CUSTOMER_CONTACT_METHOD_PROBS = np.array([0.45, 0.20, 0.25, 0.10])

# Age-conditioned demographic tables
# Age bracket index: 0=18-24, 1=25-34, 2=35-44, 3=45-54, 4=55-64, 5=65+
CUSTOMER_MARITAL_PROBS_BY_AGE = [
    np.array([0.15, 0.85]),  # 18-24: mostly single
    np.array([0.45, 0.55]),  # 25-34
    np.array([0.65, 0.35]),  # 35-44
    np.array([0.60, 0.40]),  # 45-54
    np.array([0.55, 0.45]),  # 55-64
    np.array([0.45, 0.55]),  # 65+: widowed/divorced skew single
]

CUSTOMER_EDUCATION_PROBS_BY_AGE = [
    np.array([0.60, 0.35, 0.04, 0.01]),  # 18-24: mostly HS/Bachelors
    np.array([0.15, 0.55, 0.25, 0.05]),  # 25-34
    np.array([0.15, 0.45, 0.30, 0.10]),  # 35-44
    np.array([0.20, 0.45, 0.28, 0.07]),  # 45-54
    np.array([0.25, 0.45, 0.24, 0.06]),  # 55-64
    np.array([0.30, 0.45, 0.20, 0.05]),  # 65+
]

CUSTOMER_OCCUPATION_PROBS_BY_EDUCATION = {
    "High School":  np.array([0.15, 0.30, 0.30, 0.20, 0.05]),
    "Bachelors":    np.array([0.50, 0.20, 0.15, 0.10, 0.05]),
    "Masters":      np.array([0.50, 0.10, 0.10, 0.05, 0.25]),
    "PhD":          np.array([0.55, 0.05, 0.05, 0.02, 0.33]),
}

# Poisson lambda for TotalChildren by (marital_status, age_bracket)
CUSTOMER_CHILDREN_LAMBDA_BY_MARITAL_AGE = {
    ("Single", 0): 0.05, ("Single", 1): 0.25, ("Single", 2): 0.50,
    ("Single", 3): 0.60, ("Single", 4): 0.60, ("Single", 5): 0.50,
    ("Married", 0): 0.30, ("Married", 1): 1.20, ("Married", 2): 1.80,
    ("Married", 3): 2.10, ("Married", 4): 2.20, ("Married", 5): 2.20,
}

# HomeOwnership age adjustment: [Rent, Mortgage, Own] shift per age bracket
CUSTOMER_HOME_OWNERSHIP_AGE_SHIFT = [
    np.array([0.20, -0.10, -0.10]),   # 18-24: shift toward Rent
    np.array([0.10, 0.00, -0.10]),    # 25-34
    np.array([0.00, 0.00, 0.00]),     # 35-44: neutral
    np.array([-0.05, 0.00, 0.05]),    # 45-54
    np.array([-0.10, -0.05, 0.15]),   # 55-64: shift toward Own
    np.array([-0.15, -0.05, 0.20]),   # 65+
]

# NumberOfCars: Poisson lambda by age bracket
CUSTOMER_CAR_LAMBDA_BY_AGE = np.array([0.3, 0.8, 1.2, 1.3, 1.2, 0.9])

# ----- Household defaults -----
# Fraction of *individual* customers placed into multi-person households
CUSTOMER_HOUSEHOLD_PCT = 0.35

# Household role labels
CUSTOMER_HOUSEHOLD_ROLE_LABELS = np.array(
    ["Head", "Spouse", "Dependent", "Relative"], dtype=object
)

# Min age for a household head (years)
CUSTOMER_HOUSEHOLD_HEAD_MIN_AGE = 25

# Max spousal age gap in years (absolute)
CUSTOMER_HOUSEHOLD_SPOUSE_MAX_AGE_GAP = 12

# Min age for a spouse (years)
CUSTOMER_HOUSEHOLD_SPOUSE_MIN_AGE = 21

# Min age gap between head/spouse and a dependent (years)
CUSTOMER_HOUSEHOLD_DEPENDENT_MIN_AGE_GAP = 18

# Max age for a dependent (years) — young adults still in household
CUSTOMER_HOUSEHOLD_DEPENDENT_MAX_AGE = 24

CUSTOMER_ORG_EMAIL_PREFIXES = np.array([
    "info", "contact", "sales", "hello", "support",
    "admin", "office", "enquiries", "procurement", "orders",
])

# Phone formats keyed by region
CUSTOMER_PHONE_COUNTRY_CODES = {"US": "+1", "IN": "+91", "EU": "+44", "AS": "+81"}

# Address generation pools
CUSTOMER_STREET_NAMES = np.array([
    "Main", "Oak", "Cedar", "Maple", "Park", "Elm", "Pine", "Washington",
    "Lake", "Hill", "Sunset", "River", "Spring", "Church", "Market",
    "Forest", "Bridge", "Meadow", "Valley", "Highland", "Garden", "Willow",
    "Birch", "Chestnut", "Victoria", "Lincoln", "Franklin", "Commerce",
    "Industrial", "Technology", "Innovation", "Central", "Station",
], dtype=object)

CUSTOMER_STREET_TYPES = np.array([
    "St", "Ave", "Blvd", "Dr", "Ln", "Way", "Rd", "Ct", "Pl", "Cir",
], dtype=object)

CUSTOMER_REGION_LAT_LON_CENTER = {
    "US": (39.8, -98.5),
    "IN": (22.5, 78.9),
    "EU": (51.1, 10.4),
    "AS": (36.2, 138.2),
}
CUSTOMER_LAT_LON_JITTER = {"US": (8.0, 15.0), "IN": (5.0, 7.0), "EU": (6.0, 12.0), "AS": (4.0, 8.0)}

CUSTOMER_REGION_TIMEZONE = {
    "US": np.array(["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]),
    "IN": np.array(["Asia/Kolkata"]),
    "EU": np.array(["Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Rome"]),
    "AS": np.array(["Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore"]),
}

CUSTOMER_URBAN_RURAL_LABELS = np.array(["Urban", "Suburban", "Rural"])
CUSTOMER_URBAN_RURAL_PROBS = {
    "US": np.array([0.30, 0.50, 0.20]),
    "IN": np.array([0.45, 0.35, 0.20]),
    "EU": np.array([0.40, 0.40, 0.20]),
    "AS": np.array([0.55, 0.30, 0.15]),
}

CUSTOMER_POSTCODE_FMT = {"US": "5digit", "IN": "6digit", "EU": "uk", "AS": "jp"}

CUSTOMER_LANGUAGE_BY_REGION = {
    "US": np.array(["English", "Spanish", "English", "English", "English"]),
    "IN": np.array(["Hindi", "English", "Hindi", "English", "Hindi"]),
    "EU": np.array(["English", "French", "German", "English", "French"]),
    "AS": np.array(["Japanese", "Mandarin", "English", "Japanese", "Mandarin"]),
}


# =================================================================
#  PROMOTION_DEFAULTS
# =================================================================

PROMOTION_PROMO_TYPES: Dict[str, str] = {
    "Holiday": "Holiday Discount",
    "Seasonal": "Seasonal Discount",
    "Clearance": "Clearance",
    "Limited": "Limited Time",
    "Flash": "Flash Sale",
    "Volume": "Volume Discount",
    "Loyalty": "Loyalty Exclusive",
    "Bundle": "Bundle Deal",
    "NewCustomer": "New Customer",
    "NoDiscount": "No Discount",
}

PROMOTION_CATEGORIES = ["Store", "Online", "Region"]

# name, start_mmdd, end_mmdd, discount_min, discount_max
PROMOTION_HOLIDAYS: List[Tuple[str, str, str, float, float]] = [
    ("Black Friday",   "11-25", "11-30", 0.20, 0.70),
    ("Cyber Monday",   "11-28", "12-02", 0.15, 0.50),
    ("Christmas",      "12-10", "12-31", 0.20, 0.60),
    ("New Year",       "12-26", "01-05", 0.10, 0.40),
    ("Back-to-School", "07-01", "09-15", 0.05, 0.25),
    ("Easter",         "03-20", "04-10", 0.05, 0.30),
    ("Diwali",         "10-01", "11-15", 0.10, 0.50),
]

# Seasonal name -> (start_month, end_month) (end may wrap to next year)
PROMOTION_SEASON_WINDOWS: Dict[str, Tuple[int, int]] = {
    "Spring Event": (2, 4),
    "Summer Event": (5, 8),
    "Autumn Event": (9, 10),
    "Winter Event": (11, 1),           # wraps year
    "Mid-Season Event": (3, 9),
}

# Default counts per promotion type (used when not overridden by config)
PROMOTION_DEFAULT_NUM_SEASONAL = 20
PROMOTION_DEFAULT_NUM_CLEARANCE = 8
PROMOTION_DEFAULT_NUM_LIMITED = 12
PROMOTION_DEFAULT_NUM_FLASH = 6
PROMOTION_DEFAULT_NUM_VOLUME = 4
PROMOTION_DEFAULT_NUM_LOYALTY = 3
PROMOTION_DEFAULT_NUM_BUNDLE = 3
PROMOTION_DEFAULT_NUM_NEW_CUSTOMER = 3


# =================================================================
#  CURRENCY_DEFAULTS
# =================================================================

CURRENCY_NAME_MAP: Dict[str, str] = {
    "USD": "US Dollar",
    "EUR": "Euro",
    "INR": "Indian Rupee",
    "GBP": "British Pound",
    "AUD": "Australian Dollar",
    "CAD": "Canadian Dollar",
    "CNY": "Chinese Yuan",
    "JPY": "Japanese Yen",
    "NZD": "New Zealand Dollar",
    "CHF": "Swiss Franc",
    "SEK": "Swedish Krona",
    "NOK": "Norwegian Krone",
    "SGD": "Singapore Dollar",
    "HKD": "Hong Kong Dollar",
    "KRW": "Korean Won",
    "ZAR": "South African Rand",
}

CURRENCY_BASE = "USD"

CURRENCY_DEFAULT_LIST: List[str] = [
    "CAD", "GBP", "EUR", "INR", "AUD", "CNY", "JPY",
]

CURRENCY_SYMBOL_MAP: Dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "INR": "₹",
    "GBP": "£",
    "AUD": "A$",
    "CAD": "C$",
    "CNY": "¥",
    "JPY": "¥",
    "NZD": "NZ$",
    "CHF": "CHF",
    "SEK": "kr",
    "NOK": "kr",
    "SGD": "S$",
    "HKD": "HK$",
    "KRW": "₩",
    "ZAR": "R",
}

CURRENCY_DECIMAL_PLACES: Dict[str, int] = {
    "JPY": 0,
    "KRW": 0,
}
CURRENCY_DECIMAL_PLACES_DEFAULT = 2


# =================================================================
#  INVENTORY_DEFAULTS
# =================================================================

# Below this pair count, run single-process (overhead of spawning isn't worth it)
INVENTORY_PARALLEL_THRESHOLD = 50_000

# Below this customer count, customer generation stays single-process
CUSTOMER_PARALLEL_THRESHOLD = 200_000

# Below this eligible-customer count, subscriptions stay single-process
SUBSCRIPTION_PARALLEL_THRESHOLD = 200_000

# Below this count, product enrichment stays single-process
PRODUCT_PARALLEL_THRESHOLD = 50_000

# Below this estimated row count, wishlists stay single-process
WISHLIST_PARALLEL_THRESHOLD = 100_000

# Windows worker memory auto-cap (used in sales.py)
WORKER_OS_RESERVE_MB = 4_000   # MB reserved for OS + main process
WORKER_ESTIMATE_MB = 500       # MB estimated per sales worker process


# =================================================================
#  MODULE-LEVEL VALIDATION
# =================================================================

def _validate_probability_arrays() -> None:
    """Verify that all probability arrays in this module sum to ~1.0."""
    _PROB_ARRAYS = {
        "STORE_TYPES_P": STORE_TYPES_P,
        "STORE_STATUS_P": STORE_STATUS_P,
        "STORE_REVENUE_CLASSES_P": STORE_REVENUE_CLASSES_P,
        "CUSTOMER_MARITAL_STATUS_PROBS": CUSTOMER_MARITAL_STATUS_PROBS,
        "CUSTOMER_EDUCATION_PROBS": CUSTOMER_EDUCATION_PROBS,
        "CUSTOMER_OCCUPATION_PROBS": CUSTOMER_OCCUPATION_PROBS,
        "CUSTOMER_CONTACT_METHOD_PROBS": CUSTOMER_CONTACT_METHOD_PROBS,
    }
    for name, arr in _PROB_ARRAYS.items():
        total = float(arr.sum())
        if abs(total - 1.0) > 1e-6:
            raise ValidationError(
                f"defaults.{name} probabilities sum to {total}, expected 1.0"
            )

    # Validate nested probability dicts (dict of arrays)
    _NESTED_PROB_DICTS = {
        "CUSTOMER_HOME_OWNERSHIP_PROBS_BY_INCOME": CUSTOMER_HOME_OWNERSHIP_PROBS_BY_INCOME,
        "CUSTOMER_OCCUPATION_PROBS_BY_EDUCATION": CUSTOMER_OCCUPATION_PROBS_BY_EDUCATION,
        "CUSTOMER_URBAN_RURAL_PROBS": CUSTOMER_URBAN_RURAL_PROBS,
    }
    for name, d in _NESTED_PROB_DICTS.items():
        for key, arr in d.items():
            total = float(arr.sum())
            if abs(total - 1.0) > 1e-6:
                raise ValidationError(
                    f"defaults.{name}[{key!r}] probabilities sum to {total}, expected 1.0"
                )

    # Validate lists of probability arrays
    _PROB_LISTS = {
        "CUSTOMER_MARITAL_PROBS_BY_AGE": CUSTOMER_MARITAL_PROBS_BY_AGE,
        "CUSTOMER_EDUCATION_PROBS_BY_AGE": CUSTOMER_EDUCATION_PROBS_BY_AGE,
    }
    for name, lst in _PROB_LISTS.items():
        for i, arr in enumerate(lst):
            total = float(arr.sum())
            if abs(total - 1.0) > 1e-6:
                raise ValidationError(
                    f"defaults.{name}[{i}] probabilities sum to {total}, expected 1.0"
                )

    # Validate dict-of-tuple probability structures (choices, probs)
    _TUPLE_PROB_DICTS = {
        "STORE_FORMATS": STORE_FORMATS,
        "STORE_OWNERSHIP_TYPES": STORE_OWNERSHIP_TYPES,
    }
    for name, d in _TUPLE_PROB_DICTS.items():
        for key, (choices, probs) in d.items():
            total = float(sum(probs))
            if abs(total - 1.0) > 1e-6:
                raise ValidationError(
                    f"defaults.{name}[{key!r}] probabilities sum to {total}, expected 1.0"
                )
            if len(choices) != len(probs):
                raise ValidationError(
                    f"defaults.{name}[{key!r}] choices/probs length mismatch: {len(choices)} vs {len(probs)}"
                )

    # Validate PROMOTION_HOLIDAYS discount ranges
    for entry in PROMOTION_HOLIDAYS:
        name_h, _, _, d_min, d_max = entry
        if d_min > d_max:
            raise ValidationError(
                f"defaults.PROMOTION_HOLIDAYS[{name_h!r}] discount_min ({d_min}) > discount_max ({d_max})"
            )

    # Validate PROMOTION_SEASON_WINDOWS months are 1-12
    for sname, (s_start, s_end) in PROMOTION_SEASON_WINDOWS.items():
        if not (1 <= s_start <= 12 and 1 <= s_end <= 12):
            raise ValidationError(
                f"defaults.PROMOTION_SEASON_WINDOWS[{sname!r}] has invalid month(s): ({s_start}, {s_end})"
            )


# =================================================================
#  SALES CHANNEL DEFAULTS
# =================================================================
# Core channel keys (1-5) match lookups.py defaults.
# Used as fallback when sales_channels.parquet is not available.
SALES_CHANNEL_CORE_KEYS = np.array([1, 2, 3, 4, 5], dtype=np.int32)
SALES_CHANNEL_CORE_KEYS.flags.writeable = False

# Channel classification arrays (used by sales pipeline correlation logic)
PHYSICAL_CHANNELS = np.array([1, 5, 10], dtype=np.int16)          # Store, CallCenter, Kiosk
DIGITAL_CHANNELS = np.array([2, 3, 6, 7, 8], dtype=np.int16)     # Online, Marketplace, Web, MobileApp, Social
ALL_CHANNELS = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=np.int16)

# StoreType -> (valid_channel_keys, probability_weights)
STORE_TYPE_CHANNEL_MAP = {
    "Online":      (DIGITAL_CHANNELS, np.array([0.35, 0.10, 0.20, 0.25, 0.10], dtype=np.float64)),
    "Fulfillment": (DIGITAL_CHANNELS, np.array([0.35, 0.10, 0.20, 0.25, 0.10], dtype=np.float64)),
    "Retail":       (np.array([1, 2, 3, 5, 7, 10], dtype=np.int16),
                     np.array([0.50, 0.15, 0.05, 0.10, 0.10, 0.10], dtype=np.float64)),
    "Flagship":     (np.array([1, 2, 5, 7, 10], dtype=np.int16),
                     np.array([0.55, 0.15, 0.10, 0.10, 0.10], dtype=np.float64)),
    "Hypermarket":  (np.array([1, 2, 3, 5, 10], dtype=np.int16),
                     np.array([0.55, 0.15, 0.05, 0.10, 0.15], dtype=np.float64)),
    "Supermarket":  (np.array([1, 2, 5, 10], dtype=np.int16),
                     np.array([0.60, 0.15, 0.10, 0.15], dtype=np.float64)),
    "Convenience":  (np.array([1, 5, 10], dtype=np.int16),
                     np.array([0.70, 0.10, 0.20], dtype=np.float64)),
    "Outlet":       (np.array([1, 2, 5], dtype=np.int16),
                     np.array([0.65, 0.20, 0.15], dtype=np.float64)),
    "Warehouse":    (np.array([1, 2, 4, 9], dtype=np.int16),
                     np.array([0.30, 0.25, 0.25, 0.20], dtype=np.float64)),
}
DEFAULT_CHANNEL_MAP = (np.array([1, 2, 3, 5], dtype=np.int16),
                       np.array([0.40, 0.30, 0.15, 0.15], dtype=np.float64))

# Default channel fulfillment days (overridden at runtime from sales_channels.parquet)
DEFAULT_CHANNEL_FULFILLMENT_DAYS = np.array(
    [0, 0, 3, 5, 7, 2, 3, 3, 5, 7, 0], dtype=np.int32,
)  # [Unknown, Store, Online, Marketplace, B2B, CallCenter, Web, MobileApp, SocialCommerce, PartnerReseller, Kiosk]

# Channel key -> product eligibility group index (0=store, 1=online, 2=marketplace, 3=b2b)
CHANNEL_TO_ELIG_GROUP = np.array([0, 0, 1, 2, 3, 1, 1, 1, 2, 3, 0], dtype=np.int8)
#                                 ^0 ^1 ^2 ^3 ^4 ^5 ^6 ^7 ^8 ^9 ^10

# Nanoseconds per day (used for datetime64[ns] arithmetic)
NS_PER_DAY: int = 86_400_000_000_000


# =================================================================
#  RETURN REASONS — single source of truth
#  (key, label, category, default_weight)
# =================================================================

RETURN_REASONS: tuple[tuple[int, str, str, float], ...] = (
    (1, "Defective",            "Quality",     0.20),
    (2, "Damaged in shipping",  "Logistics",   0.12),
    (3, "Wrong item",           "Fulfillment", 0.14),
    (4, "Not as described",     "Customer",    0.10),
    (5, "No longer needed",     "Customer",    0.14),
    (6, "Late delivery",        "Logistics",   0.08),
    (7, "Better price found",   "Customer",    0.07),
    (8, "Other",                "Other",       0.15),
)

RETURN_REASON_KEYS: tuple[int, ...] = tuple(r[0] for r in RETURN_REASONS)
RETURN_REASON_LABELS: dict[int, str] = {r[0]: r[1] for r in RETURN_REASONS}
RETURN_REASON_CATEGORIES: dict[int, str] = {r[0]: r[2] for r in RETURN_REASONS}
RETURN_REASON_DEFAULT_WEIGHTS: dict[int, float] = {r[0]: r[3] for r in RETURN_REASONS}
RETURN_REASON_LOGISTICS_KEYS: frozenset[int] = frozenset(
    r[0] for r in RETURN_REASONS if r[2] == "Logistics"
)

_rr_weights = np.array([r[3] for r in RETURN_REASONS])
if abs(float(_rr_weights.sum()) - 1.0) > 1e-6:
    raise ValidationError(
        f"defaults.RETURN_REASONS weights sum to {_rr_weights.sum()}, expected 1.0"
    )
del _rr_weights


_validate_probability_arrays()
