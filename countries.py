"""Ülke adı / kodu -> bayrak emojisi."""
from __future__ import annotations

import re

_SUBDIVISIONS = {
    "england": "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "scotland": "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
    "wales": "🏴\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f",
}

_NAMES = {
    "united states": "US", "usa": "US", "united states of america": "US", "america": "US",
    "brazil": "BR", "russia": "RU", "russian federation": "RU", "united kingdom": "GB",
    "great britain": "GB", "uk": "GB", "northern ireland": "GB", "ireland": "IE",
    "canada": "CA", "mexico": "MX", "australia": "AU", "new zealand": "NZ", "china": "CN",
    "japan": "JP", "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "philippines": "PH", "thailand": "TH", "vietnam": "VN", "indonesia": "ID",
    "malaysia": "MY", "singapore": "SG", "india": "IN", "mongolia": "MN",
    "kazakhstan": "KZ", "kyrgyzstan": "KG", "uzbekistan": "UZ", "tajikistan": "TJ",
    "turkmenistan": "TM", "azerbaijan": "AZ", "armenia": "AM", "georgia": "GE",
    "turkey": "TR", "türkiye": "TR", "turkiye": "TR", "iran": "IR", "iraq": "IQ",
    "afghanistan": "AF", "israel": "IL", "lebanon": "LB", "jordan": "JO",
    "saudi arabia": "SA", "united arab emirates": "AE", "uae": "AE", "bahrain": "BH",
    "egypt": "EG", "morocco": "MA", "algeria": "DZ", "tunisia": "TN", "nigeria": "NG",
    "cameroon": "CM", "south africa": "ZA", "dr congo": "CD", "congo": "CG",
    "democratic republic of the congo": "CD", "senegal": "SN", "ghana": "GH",
    "france": "FR", "germany": "DE", "italy": "IT", "spain": "ES", "portugal": "PT",
    "netherlands": "NL", "holland": "NL", "belgium": "BE", "switzerland": "CH",
    "austria": "AT", "poland": "PL", "czech republic": "CZ", "czechia": "CZ",
    "slovakia": "SK", "hungary": "HU", "romania": "RO", "bulgaria": "BG",
    "moldova": "MD", "ukraine": "UA", "belarus": "BY", "lithuania": "LT",
    "latvia": "LV", "estonia": "EE", "finland": "FI", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "iceland": "IS", "serbia": "RS", "croatia": "HR", "bosnia": "BA",
    "bosnia and herzegovina": "BA", "slovenia": "SI", "montenegro": "ME",
    "north macedonia": "MK", "macedonia": "MK", "albania": "AL", "kosovo": "XK",
    "greece": "GR", "cyprus": "CY", "malta": "MT", "argentina": "AR", "chile": "CL",
    "peru": "PE", "colombia": "CO", "venezuela": "VE", "ecuador": "EC", "bolivia": "BO",
    "paraguay": "PY", "uruguay": "UY", "cuba": "CU", "dominican republic": "DO",
    "puerto rico": "PR", "jamaica": "JM", "haiti": "HT", "panama": "PA",
    "costa rica": "CR", "guatemala": "GT", "suriname": "SR", "chechnya": "RU",
    "dagestan": "RU", "taiwan": "TW", "hong kong": "HK", "myanmar": "MM",
    "cambodia": "KH", "pakistan": "PK", "sri lanka": "LK", "nepal": "NP",
}

_ALPHA3 = {
    "USA": "US", "BRA": "BR", "RUS": "RU", "GBR": "GB", "IRL": "IE", "CAN": "CA",
    "MEX": "MX", "AUS": "AU", "NZL": "NZ", "CHN": "CN", "JPN": "JP", "KOR": "KR",
    "PHL": "PH", "THA": "TH", "KAZ": "KZ", "KGZ": "KG", "UZB": "UZ", "TJK": "TJ",
    "AZE": "AZ", "ARM": "AM", "GEO": "GE", "TUR": "TR", "IRN": "IR", "FRA": "FR",
    "DEU": "DE", "GER": "DE", "ITA": "IT", "ESP": "ES", "POR": "PT", "PRT": "PT",
    "NLD": "NL", "NED": "NL", "BEL": "BE", "CHE": "CH", "SUI": "CH", "AUT": "AT",
    "POL": "PL", "CZE": "CZ", "SVK": "SK", "HUN": "HU", "ROU": "RO", "BGR": "BG",
    "UKR": "UA", "BLR": "BY", "LTU": "LT", "SWE": "SE", "NOR": "NO", "DNK": "DK",
    "FIN": "FI", "SRB": "RS", "HRV": "HR", "CRO": "HR", "BIH": "BA", "SVN": "SI",
    "ALB": "AL", "GRC": "GR", "ARG": "AR", "CHL": "CL", "PER": "PE", "COL": "CO",
    "VEN": "VE", "ECU": "EC", "CUB": "CU", "NGA": "NG", "CMR": "CM", "ZAF": "ZA",
    "RSA": "ZA", "MAR": "MA", "EGY": "EG", "MNG": "MN", "CHI": "CL",
}


def iso_to_flag(code: str) -> str:
    code = code.upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


def country_flag(value: str | None) -> str:
    """Ülke adı, ISO-2 veya ISO-3 kodu ya da bayrak görsel dosya adından emoji üretir."""
    if not value:
        return ""
    raw = re.sub(r"\.(png|svg|gif|jpe?g|webp)$", "", value.strip(), flags=re.I)
    raw = raw.rsplit("/", 1)[-1]
    norm = re.sub(r"[_\-]+", " ", raw).lower()
    norm = re.sub(r"\bflag( of)?\b", "", norm).strip()
    if norm in _SUBDIVISIONS:
        return _SUBDIVISIONS[norm]
    if norm in _NAMES:
        return iso_to_flag(_NAMES[norm])
    up = norm.upper().replace(" ", "")
    if len(up) == 2 and up.isalpha():
        return "🇬🇧" if up == "UK" else iso_to_flag(up)
    if up in _ALPHA3:
        return iso_to_flag(_ALPHA3[up])
    return ""
