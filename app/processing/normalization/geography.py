"""Reference geography used by the location normalizer.

The tables are intentionally small and curated rather than a full gazetteer: a
job-market platform needs the countries and the tech hubs that actually appear
in postings, and a compact in-memory index keeps normalization allocation-free
in the hot path.
"""

from __future__ import annotations

from app.core.text import canonical_key

#: ISO-3166 alpha-2 -> canonical English country name.
COUNTRIES: dict[str, str] = {
    "AE": "United Arab Emirates",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CL": "Chile",
    "CN": "China",
    "CO": "Colombia",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "EG": "Egypt",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HK": "Hong Kong",
    "HR": "Croatia",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IR": "Iran",
    "IS": "Iceland",
    "IT": "Italy",
    "JP": "Japan",
    "KE": "Kenya",
    "KR": "South Korea",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MA": "Morocco",
    "MX": "Mexico",
    "MY": "Malaysia",
    "NG": "Nigeria",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PE": "Peru",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PT": "Portugal",
    "QA": "Qatar",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russia",
    "SA": "Saudi Arabia",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "TH": "Thailand",
    "TR": "Turkey",
    "TW": "Taiwan",
    "UA": "Ukraine",
    "US": "United States",
    "VN": "Vietnam",
    "ZA": "South Africa",
}

#: Extra spellings and abbreviations seen in postings.
COUNTRY_ALIASES: dict[str, str] = {
    "usa": "US",
    "u s a": "US",
    "u s": "US",
    "united states of america": "US",
    "america": "US",
    "uk": "GB",
    "u k": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "uae": "AE",
    "emirates": "AE",
    "holland": "NL",
    "the netherlands": "NL",
    "deutschland": "DE",
    "czech republic": "CZ",
    "republic of ireland": "IE",
    "korea": "KR",
    "republic of korea": "KR",
    "russia": "RU",
    "russian federation": "RU",
    "viet nam": "VN",
    "turkiye": "TR",
    "espana": "ES",
    "brasil": "BR",
    "polska": "PL",
}

#: City -> (country code, region). Only hubs that actually appear in feeds.
CITIES: dict[str, tuple[str, str | None]] = {
    "amsterdam": ("NL", "North Holland"),
    "athens": ("GR", "Attica"),
    "atlanta": ("US", "Georgia"),
    "auckland": ("NZ", "Auckland"),
    "austin": ("US", "Texas"),
    "bangalore": ("IN", "Karnataka"),
    "bengaluru": ("IN", "Karnataka"),
    "barcelona": ("ES", "Catalonia"),
    "beijing": ("CN", "Beijing"),
    "belgrade": ("RS", "Belgrade"),
    "berlin": ("DE", "Berlin"),
    "bogota": ("CO", "Bogota"),
    "boston": ("US", "Massachusetts"),
    "bratislava": ("SK", "Bratislava"),
    "brisbane": ("AU", "Queensland"),
    "brussels": ("BE", "Brussels"),
    "bucharest": ("RO", "Bucharest"),
    "budapest": ("HU", "Budapest"),
    "buenos aires": ("AR", "Buenos Aires"),
    "cairo": ("EG", "Cairo"),
    "cape town": ("ZA", "Western Cape"),
    "chicago": ("US", "Illinois"),
    "cologne": ("DE", "North Rhine-Westphalia"),
    "copenhagen": ("DK", "Capital Region"),
    "dallas": ("US", "Texas"),
    "delhi": ("IN", "Delhi"),
    "denver": ("US", "Colorado"),
    "dubai": ("AE", "Dubai"),
    "dublin": ("IE", "Leinster"),
    "dusseldorf": ("DE", "North Rhine-Westphalia"),
    "edinburgh": ("GB", "Scotland"),
    "frankfurt": ("DE", "Hesse"),
    "geneva": ("CH", "Geneva"),
    "gothenburg": ("SE", "Vastra Gotaland"),
    "hamburg": ("DE", "Hamburg"),
    "helsinki": ("FI", "Uusimaa"),
    "hong kong": ("HK", None),
    "hyderabad": ("IN", "Telangana"),
    "istanbul": ("TR", "Istanbul"),
    "jakarta": ("ID", "Jakarta"),
    "johannesburg": ("ZA", "Gauteng"),
    "krakow": ("PL", "Lesser Poland"),
    "kuala lumpur": ("MY", "Kuala Lumpur"),
    "lisbon": ("PT", "Lisbon"),
    "london": ("GB", "England"),
    "los angeles": ("US", "California"),
    "luxembourg": ("LU", None),
    "madrid": ("ES", "Madrid"),
    "malaga": ("ES", "Andalusia"),
    "manchester": ("GB", "England"),
    "melbourne": ("AU", "Victoria"),
    "mexico city": ("MX", "Mexico City"),
    "miami": ("US", "Florida"),
    "milan": ("IT", "Lombardy"),
    "montreal": ("CA", "Quebec"),
    "moscow": ("RU", "Moscow"),
    "mumbai": ("IN", "Maharashtra"),
    "munich": ("DE", "Bavaria"),
    "nairobi": ("KE", "Nairobi"),
    "new york": ("US", "New York"),
    "oslo": ("NO", "Oslo"),
    "ottawa": ("CA", "Ontario"),
    "paris": ("FR", "Ile-de-France"),
    "porto": ("PT", "Porto"),
    "prague": ("CZ", "Prague"),
    "pune": ("IN", "Maharashtra"),
    "riga": ("LV", "Riga"),
    "rome": ("IT", "Lazio"),
    "san diego": ("US", "California"),
    "san francisco": ("US", "California"),
    "santiago": ("CL", "Santiago"),
    "sao paulo": ("BR", "Sao Paulo"),
    "seattle": ("US", "Washington"),
    "seoul": ("KR", "Seoul"),
    "shanghai": ("CN", "Shanghai"),
    "singapore": ("SG", None),
    "sofia": ("BG", "Sofia"),
    "stockholm": ("SE", "Stockholm"),
    "stuttgart": ("DE", "Baden-Wurttemberg"),
    "sydney": ("AU", "New South Wales"),
    "taipei": ("TW", "Taipei"),
    "tallinn": ("EE", "Harju"),
    "tel aviv": ("IL", "Tel Aviv"),
    "tokyo": ("JP", "Tokyo"),
    "toronto": ("CA", "Ontario"),
    "utrecht": ("NL", "Utrecht"),
    "vancouver": ("CA", "British Columbia"),
    "vienna": ("AT", "Vienna"),
    "vilnius": ("LT", "Vilnius"),
    "warsaw": ("PL", "Masovia"),
    "washington": ("US", "District of Columbia"),
    "wroclaw": ("PL", "Lower Silesia"),
    "zagreb": ("HR", "Zagreb"),
    "zurich": ("CH", "Zurich"),
}

#: US state abbreviations, which appear as ``Austin, TX``.
# fmt: off
US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
# fmt: on

#: Multi-country regions that appear instead of a country ("Remote - EMEA").
MACRO_REGIONS: frozenset[str] = frozenset(
    {"emea", "apac", "latam", "anywhere", "worldwide", "global", "europe", "eu", "us only", "na"}
)

REMOTE_TOKENS: frozenset[str] = frozenset(
    {"remote", "fully remote", "100 remote", "work from home", "wfh", "telecommute", "distributed"}
)

HYBRID_TOKENS: frozenset[str] = frozenset({"hybrid", "flexible", "partially remote", "part remote"})

ONSITE_TOKENS: frozenset[str] = frozenset(
    {"onsite", "on site", "in office", "in person", "office based"}
)


def _build_country_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for code, name in COUNTRIES.items():
        index[canonical_key(name)] = code
        index[code.lower()] = code
    for alias, code in COUNTRY_ALIASES.items():
        index[canonical_key(alias)] = code
    return index


#: Canonical lookup key -> ISO country code.
COUNTRY_INDEX: dict[str, str] = _build_country_index()

#: Canonical city key -> (country code, region).
CITY_INDEX: dict[str, tuple[str, str | None]] = {
    canonical_key(city): value for city, value in CITIES.items()
}


def lookup_country(value: str) -> str | None:
    """Resolve a country name, alias or code to an ISO alpha-2 code."""
    if not value:
        return None
    key = canonical_key(value)
    if not key:
        return None
    return COUNTRY_INDEX.get(key)


def lookup_city(value: str) -> tuple[str, str | None] | None:
    """Resolve a city name to ``(country code, region)``."""
    if not value:
        return None
    return CITY_INDEX.get(canonical_key(value))


def country_name(code: str | None) -> str | None:
    """Canonical English name for an ISO country code."""
    return COUNTRIES.get(code.upper()) if code else None


__all__ = [
    "CITIES",
    "CITY_INDEX",
    "COUNTRIES",
    "COUNTRY_ALIASES",
    "COUNTRY_INDEX",
    "HYBRID_TOKENS",
    "MACRO_REGIONS",
    "ONSITE_TOKENS",
    "REMOTE_TOKENS",
    "US_STATES",
    "country_name",
    "lookup_city",
    "lookup_country",
]
