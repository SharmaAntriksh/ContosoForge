"""Build the geography_master.parquet file.

Run once:  python scripts/build_geography_master.py
Output:    data/geography/geography_master.parquet
"""
import pandas as pd
from pathlib import Path

CITIES = [
    # (City, State, Country, Continent, CurrencyCode, Lat, Lon, Timezone, Population)

    # === North America — United States (12) ===
    ("New York", "New York", "United States", "North America", "USD", 40.7128, -74.0060, "America/New_York", 8336817),
    ("Los Angeles", "California", "United States", "North America", "USD", 34.0522, -118.2437, "America/Los_Angeles", 3979576),
    ("Chicago", "Illinois", "United States", "North America", "USD", 41.8781, -87.6298, "America/Chicago", 2693976),
    ("Houston", "Texas", "United States", "North America", "USD", 29.7604, -95.3698, "America/Chicago", 2304580),
    ("Phoenix", "Arizona", "United States", "North America", "USD", 33.4484, -112.0740, "America/Phoenix", 1608139),
    ("Dallas", "Texas", "United States", "North America", "USD", 32.7767, -96.7970, "America/Chicago", 1304379),
    ("San Francisco", "California", "United States", "North America", "USD", 37.7749, -122.4194, "America/Los_Angeles", 873965),
    ("Seattle", "Washington", "United States", "North America", "USD", 47.6062, -122.3321, "America/Los_Angeles", 737015),
    ("Miami", "Florida", "United States", "North America", "USD", 25.7617, -80.1918, "America/New_York", 442241),
    ("Atlanta", "Georgia", "United States", "North America", "USD", 33.7490, -84.3880, "America/New_York", 498715),
    ("Boston", "Massachusetts", "United States", "North America", "USD", 42.3601, -71.0589, "America/New_York", 694583),
    ("Denver", "Colorado", "United States", "North America", "USD", 39.7392, -104.9903, "America/Denver", 715522),

    # === North America — Canada (5) ===
    ("Toronto", "Ontario", "Canada", "North America", "CAD", 43.6532, -79.3832, "America/Toronto", 2794356),
    ("Vancouver", "British Columbia", "Canada", "North America", "CAD", 49.2827, -123.1207, "America/Vancouver", 662248),
    ("Montreal", "Quebec", "Canada", "North America", "CAD", 45.5017, -73.5673, "America/Montreal", 1762949),
    ("Calgary", "Alberta", "Canada", "North America", "CAD", 51.0447, -114.0719, "America/Edmonton", 1336000),
    ("Ottawa", "Ontario", "Canada", "North America", "CAD", 45.4215, -75.6972, "America/Toronto", 1017449),

    # === North America — Mexico (1) ===
    ("Mexico City", "Mexico City", "Mexico", "North America", "MXN", 19.4326, -99.1332, "America/Mexico_City", 9209944),

    # === Europe — United Kingdom (4) ===
    ("London", "England", "United Kingdom", "Europe", "GBP", 51.5074, -0.1278, "Europe/London", 8982000),
    ("Manchester", "England", "United Kingdom", "Europe", "GBP", 53.4808, -2.2426, "Europe/London", 553230),
    ("Birmingham", "England", "United Kingdom", "Europe", "GBP", 52.4862, -1.8904, "Europe/London", 1144900),
    ("Edinburgh", "Scotland", "United Kingdom", "Europe", "GBP", 55.9533, -3.1883, "Europe/London", 524930),

    # === Europe — Germany (4) ===
    ("Berlin", "Berlin", "Germany", "Europe", "EUR", 52.5200, 13.4050, "Europe/Berlin", 3644826),
    ("Munich", "Bavaria", "Germany", "Europe", "EUR", 48.1351, 11.5820, "Europe/Berlin", 1471508),
    ("Hamburg", "Hamburg", "Germany", "Europe", "EUR", 53.5511, 9.9937, "Europe/Berlin", 1841179),
    ("Frankfurt", "Hesse", "Germany", "Europe", "EUR", 50.1109, 8.6821, "Europe/Berlin", 753056),

    # === Europe — France (3) ===
    ("Paris", "Ile-de-France", "France", "Europe", "EUR", 48.8566, 2.3522, "Europe/Paris", 2161000),
    ("Lyon", "Auvergne-Rhone-Alpes", "France", "Europe", "EUR", 45.7640, 4.8357, "Europe/Paris", 516092),
    ("Marseille", "Provence-Alpes-Cote-dAzur", "France", "Europe", "EUR", 43.2965, 5.3698, "Europe/Paris", 861635),

    # === Europe — Spain (2) ===
    ("Madrid", "Madrid", "Spain", "Europe", "EUR", 40.4168, -3.7038, "Europe/Madrid", 3223334),
    ("Barcelona", "Catalonia", "Spain", "Europe", "EUR", 41.3874, 2.1686, "Europe/Madrid", 1620343),

    # === Europe — Italy (2) ===
    ("Rome", "Lazio", "Italy", "Europe", "EUR", 41.9028, 12.4964, "Europe/Rome", 2873000),
    ("Milan", "Lombardy", "Italy", "Europe", "EUR", 45.4642, 9.1900, "Europe/Rome", 1352000),

    # === Europe — Netherlands (1) ===
    ("Amsterdam", "North Holland", "Netherlands", "Europe", "EUR", 52.3676, 4.9041, "Europe/Amsterdam", 872680),

    # === Europe — Belgium (1) ===
    ("Brussels", "Brussels", "Belgium", "Europe", "EUR", 50.8503, 4.3517, "Europe/Brussels", 1209000),

    # === Europe — Austria (1) ===
    ("Vienna", "Vienna", "Austria", "Europe", "EUR", 48.2082, 16.3738, "Europe/Vienna", 1911191),

    # === Europe — Switzerland (1) ===
    ("Zurich", "Zurich", "Switzerland", "Europe", "CHF", 47.3769, 8.5417, "Europe/Zurich", 421878),

    # === Europe — Sweden (1) ===
    ("Stockholm", "Stockholm", "Sweden", "Europe", "SEK", 59.3293, 18.0686, "Europe/Stockholm", 975904),

    # === Europe — Poland (1) ===
    ("Warsaw", "Masovia", "Poland", "Europe", "PLN", 52.2297, 21.0122, "Europe/Warsaw", 1793579),

    # === Europe — Ireland (1) ===
    ("Dublin", "Leinster", "Ireland", "Europe", "EUR", 53.3498, -6.2603, "Europe/Dublin", 1228179),

    # === Europe — Portugal (1) ===
    ("Lisbon", "Lisbon", "Portugal", "Europe", "EUR", 38.7223, -9.1393, "Europe/Lisbon", 544851),

    # === Europe — Denmark (1) ===
    ("Copenhagen", "Capital Region", "Denmark", "Europe", "EUR", 55.6761, 12.5683, "Europe/Copenhagen", 794128),

    # === Europe — Norway (1) ===
    ("Oslo", "Oslo", "Norway", "Europe", "NOK", 59.9139, 10.7522, "Europe/Oslo", 697549),

    # === Asia — India (3) ===
    ("Mumbai", "Maharashtra", "India", "Asia", "INR", 19.0760, 72.8777, "Asia/Kolkata", 20411000),
    ("Delhi", "Delhi", "India", "Asia", "INR", 28.7041, 77.1025, "Asia/Kolkata", 16787941),
    ("Bengaluru", "Karnataka", "India", "Asia", "INR", 12.9716, 77.5946, "Asia/Kolkata", 8443675),

    # === Asia — China (3) ===
    ("Shanghai", "Shanghai", "China", "Asia", "CNY", 31.2304, 121.4737, "Asia/Shanghai", 24870895),
    ("Beijing", "Beijing", "China", "Asia", "CNY", 39.9042, 116.4074, "Asia/Shanghai", 21542000),
    ("Shenzhen", "Guangdong", "China", "Asia", "CNY", 22.5431, 114.0579, "Asia/Shanghai", 12528300),

    # === Asia — Japan (2) ===
    ("Tokyo", "Tokyo", "Japan", "Asia", "JPY", 35.6762, 139.6503, "Asia/Tokyo", 13960000),
    ("Osaka", "Osaka", "Japan", "Asia", "JPY", 34.6937, 135.5023, "Asia/Tokyo", 2753862),

    # === Asia — South Korea (1) ===
    ("Seoul", "Seoul", "South Korea", "Asia", "KRW", 37.5665, 126.9780, "Asia/Seoul", 9776000),

    # === Asia — Singapore (1) ===
    ("Singapore", "Singapore", "Singapore", "Asia", "SGD", 1.3521, 103.8198, "Asia/Singapore", 5686000),

    # === Asia — Hong Kong (1) ===
    ("Hong Kong", "Hong Kong", "Hong Kong", "Asia", "HKD", 22.3193, 114.1694, "Asia/Hong_Kong", 7482500),

    # === Asia — Thailand (1) ===
    ("Bangkok", "Bangkok", "Thailand", "Asia", "THB", 13.7563, 100.5018, "Asia/Bangkok", 10539000),

    # === Asia — Indonesia (1) ===
    ("Jakarta", "Jakarta", "Indonesia", "Asia", "IDR", -6.2088, 106.8456, "Asia/Jakarta", 10562088),

    # === Asia — Malaysia (1) ===
    ("Kuala Lumpur", "Kuala Lumpur", "Malaysia", "Asia", "MYR", 3.1390, 101.6869, "Asia/Kuala_Lumpur", 1982112),

    # === Asia — Philippines (1) ===
    ("Manila", "Metro Manila", "Philippines", "Asia", "PHP", 14.5995, 120.9842, "Asia/Manila", 1780148),

    # === Asia — Taiwan (1) ===
    ("Taipei", "Taipei", "Taiwan", "Asia", "TWD", 25.0330, 121.5654, "Asia/Taipei", 2646204),

    # === Asia — UAE (1) ===
    ("Dubai", "Dubai", "United Arab Emirates", "Asia", "AED", 25.2048, 55.2708, "Asia/Dubai", 3331420),

    # === Asia — Saudi Arabia (1) ===
    ("Riyadh", "Riyadh", "Saudi Arabia", "Asia", "SAR", 24.7136, 46.6753, "Asia/Riyadh", 7676654),

    # === Oceania — Australia (3) ===
    ("Sydney", "New South Wales", "Australia", "Oceania", "AUD", -33.8688, 151.2093, "Australia/Sydney", 5312163),
    ("Melbourne", "Victoria", "Australia", "Oceania", "AUD", -37.8136, 144.9631, "Australia/Melbourne", 5078193),
    ("Brisbane", "Queensland", "Australia", "Oceania", "AUD", -27.4698, 153.0251, "Australia/Brisbane", 2560720),

    # === Oceania — New Zealand (1) ===
    ("Auckland", "Auckland", "New Zealand", "Oceania", "NZD", -36.8485, 174.7633, "Pacific/Auckland", 1657200),

    # === South America — Brazil (2) ===
    ("Sao Paulo", "Sao Paulo", "Brazil", "South America", "BRL", -23.5505, -46.6333, "America/Sao_Paulo", 12325232),
    ("Rio de Janeiro", "Rio de Janeiro", "Brazil", "South America", "BRL", -22.9068, -43.1729, "America/Sao_Paulo", 6748000),

    # === South America — Argentina (1) ===
    ("Buenos Aires", "Buenos Aires", "Argentina", "South America", "ARS", -34.6037, -58.3816, "America/Argentina/Buenos_Aires", 3075646),

    # === South America — Colombia (1) ===
    ("Bogota", "Bogota", "Colombia", "South America", "COP", 4.7110, -74.0721, "America/Bogota", 7181469),

    # === South America — Chile (1) ===
    ("Santiago", "Santiago", "Chile", "South America", "CLP", -33.4489, -70.6693, "America/Santiago", 6158080),

    # === South America — Peru (1) ===
    ("Lima", "Lima", "Peru", "South America", "PEN", -12.0464, -77.0428, "America/Lima", 10151200),

    # === Africa — South Africa (2) ===
    ("Johannesburg", "Gauteng", "South Africa", "Africa", "ZAR", -26.2041, 28.0473, "Africa/Johannesburg", 5635127),
    ("Cape Town", "Western Cape", "South Africa", "Africa", "ZAR", -33.9249, 18.4241, "Africa/Johannesburg", 4618000),

    # === Africa — Nigeria (1) ===
    ("Lagos", "Lagos", "Nigeria", "Africa", "NGN", 6.5244, 3.3792, "Africa/Lagos", 15388000),

    # === Africa — Egypt (1) ===
    ("Cairo", "Cairo", "Egypt", "Africa", "EGP", 30.0444, 31.2357, "Africa/Cairo", 10230350),
]


def main():
    df = pd.DataFrame(CITIES, columns=[
        "City", "State", "Country", "Continent", "CurrencyCode",
        "Latitude", "Longitude", "Timezone", "Population",
    ])

    # Validate
    dupes = df.groupby(["City", "State", "Country"]).size()
    assert (dupes == 1).all(), f"Duplicate cities: {dupes[dupes > 1]}"

    out = Path("data/geography/geography_master.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)

    print(f"Cities: {len(df)}")
    print(f"Countries: {df['Country'].nunique()}")
    print(f"Continents: {df['Continent'].nunique()}")
    print(f"Currencies: {df['CurrencyCode'].nunique()}")
    print(f"\nBy continent:")
    print(df.groupby("Continent").size().sort_values(ascending=False).to_string())
    print(f"\nBy country:")
    print(df.groupby("Country").size().sort_values(ascending=False).to_string())
    print(f"\nWritten: {out}")


if __name__ == "__main__":
    main()
