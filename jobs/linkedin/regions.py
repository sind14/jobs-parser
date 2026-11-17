"""
Built-in sub-regions used to split a country-level search.

The guest search endpoint ignores most facet filters (f_E, f_JT, f_WT are
silently dropped — verified by comparing result sets), but it honours
`location`. Searching per region is therefore a real partition of the
country's result set, and each region usually stays below the ~1000 results
pagination cap. For countries not listed here, regions can be entered
manually in the admin (SearchQuery.regions).
"""

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

REGIONS = {
    "united states": US_STATES,
    "canada": [
        "Alberta", "British Columbia", "Manitoba", "New Brunswick", "Newfoundland and Labrador",
        "Nova Scotia", "Ontario", "Prince Edward Island", "Quebec", "Saskatchewan",
    ],
    "united kingdom": ["England", "Scotland", "Wales", "Northern Ireland"],
    "germany": [
        "Baden-Württemberg", "Bavaria", "Berlin", "Brandenburg", "Bremen", "Hamburg", "Hesse",
        "Lower Saxony", "Mecklenburg-West Pomerania", "North Rhine-Westphalia",
        "Rhineland-Palatinate", "Saarland", "Saxony", "Saxony-Anhalt", "Schleswig-Holstein",
        "Thuringia",
    ],
    "poland": [
        "Lower Silesian", "Kuyavian-Pomeranian", "Lublin", "Lubusz", "Lodz", "Lesser Poland",
        "Mazowieckie", "Opole", "Podkarpackie", "Podlaskie", "Pomeranian", "Silesian",
        "Swietokrzyskie", "Warmian-Masurian", "Greater Poland", "West Pomeranian",
    ],
    "ukraine": [
        "Kyiv City", "Kyiv", "Lviv", "Kharkiv", "Dnipropetrovsk", "Odesa", "Zaporizhzhia",
        "Vinnytsia", "Ivano-Frankivsk", "Ternopil", "Khmelnytskyi", "Chernivtsi", "Zakarpattia",
        "Volyn", "Rivne", "Zhytomyr", "Poltava", "Cherkasy", "Chernihiv", "Sumy", "Kirovohrad",
        "Mykolaiv", "Kherson",
    ],
    "india": [
        "Karnataka", "Maharashtra", "Telangana", "Tamil Nadu", "Delhi", "Haryana", "Uttar Pradesh",
        "West Bengal", "Gujarat", "Kerala", "Rajasthan", "Andhra Pradesh", "Madhya Pradesh",
        "Punjab", "Odisha",
    ],
    "australia": [
        "New South Wales", "Victoria", "Queensland", "Western Australia", "South Australia",
        "Tasmania", "Australian Capital Territory", "Northern Territory",
    ],
}

ALIASES = {
    "usa": "united states", "us": "united states", "u.s.": "united states",
    "united states of america": "united states", "america": "united states",
    "uk": "united kingdom", "great britain": "united kingdom", "britain": "united kingdom",
    "deutschland": "germany", "polska": "poland", "україна": "ukraine",
}

# LinkedIn resolves "USA" poorly; use canonical names for the location param
CANONICAL = {
    "united states": "United States", "united kingdom": "United Kingdom",
}


def normalize_country(location: str) -> str:
    key = (location or "").strip().lower()
    return ALIASES.get(key, key)


def canonical_location(location: str) -> str:
    return CANONICAL.get(normalize_country(location), location)


def regions_for(location: str, custom: str = "") -> list[str]:
    """Return list of 'Region, Country' strings for splitting the search."""
    country = canonical_location(location)
    if custom and custom.strip():
        names = [r.strip() for r in custom.splitlines() if r.strip()]
    else:
        names = REGIONS.get(normalize_country(location), [])
    return [n if "," in n else f"{n}, {country}" for n in names]
