"""Country name → flag emoji. Purely decorative and language-neutral; an unknown
name just renders without a flag, so this never blocks anything.

Names must match what football-data.org returns (English country names).
"""

_FLAGS = {
    "Argentina": "🇦🇷", "Australia": "🇦🇺", "Austria": "🇦🇹", "Belgium": "🇧🇪",
    "Brazil": "🇧🇷", "Cameroon": "🇨🇲", "Canada": "🇨🇦", "Colombia": "🇨🇴",
    "Costa Rica": "🇨🇷", "Croatia": "🇭🇷", "Denmark": "🇩🇰", "Ecuador": "🇪🇨",
    "Egypt": "🇪🇬", "England": "🏴", "France": "🇫🇷", "Germany": "🇩🇪",
    "Ghana": "🇬🇭", "Iran": "🇮🇷", "Italy": "🇮🇹", "Ivory Coast": "🇨🇮",
    "Japan": "🇯🇵", "Mexico": "🇲🇽", "Morocco": "🇲🇦", "Netherlands": "🇳🇱",
    "Nigeria": "🇳🇬", "Norway": "🇳🇴", "Panama": "🇵🇦", "Paraguay": "🇵🇾",
    "Peru": "🇵🇪", "Poland": "🇵🇱", "Portugal": "🇵🇹", "Qatar": "🇶🇦",
    "Saudi Arabia": "🇸🇦", "Scotland": "🏴", "Senegal": "🇸🇳",
    "Serbia": "🇷🇸", "South Korea": "🇰🇷", "Korea Republic": "🇰🇷",
    "Spain": "🇪🇸", "Sweden": "🇸🇪", "Switzerland": "🇨🇭", "Tunisia": "🇹🇳",
    "Turkey": "🇹🇷", "Türkiye": "🇹🇷", "Ukraine": "🇺🇦", "United States": "🇺🇸",
    "USA": "🇺🇸", "Uruguay": "🇺🇾", "Wales": "🏴", "Algeria": "🇩🇿",
    "Chile": "🇨🇱", "Czech Republic": "🇨🇿", "Czechia": "🇨🇿",
    "Greece": "🇬🇷", "Hungary": "🇭🇺",
    "Romania": "🇷🇴", "New Zealand": "🇳🇿", "Jordan": "🇯🇴", "Uzbekistan": "🇺🇿",
    "Jamaica": "🇯🇲", "Honduras": "🇭🇳", "Mali": "🇲🇱", "DR Congo": "🇨🇩",
    "South Africa": "🇿🇦", "Venezuela": "🇻🇪", "Bolivia": "🇧🇴",
    "Bosnia-Herzegovina": "🇧🇦", "Bosnia and Herzegovina": "🇧🇦",
    "Cape Verde": "🇨🇻", "Cabo Verde": "🇨🇻", "Curaçao": "🇨🇼", "Curacao": "🇨🇼",
    "Haiti": "🇭🇹", "Slovakia": "🇸🇰", "Slovenia": "🇸🇮", "Finland": "🇫🇮",
    "Republic of Ireland": "🇮🇪", "Ireland": "🇮🇪", "Iceland": "🇮🇸",
    "United Arab Emirates": "🇦🇪", "Iraq": "🇮🇶", "Oman": "🇴🇲", "Bahrain": "🇧🇭",
    "Kuwait": "🇰🇼", "Indonesia": "🇮🇩", "Thailand": "🇹🇭", "Vietnam": "🇻🇳",
    "China": "🇨🇳", "China PR": "🇨🇳", "Burkina Faso": "🇧🇫", "Gabon": "🇬🇦",
    "Angola": "🇦🇴", "Zambia": "🇿🇲", "Guinea": "🇬🇳", "Benin": "🇧🇯",
    "Togo": "🇹🇬", "Uganda": "🇺🇬", "Kenya": "🇰🇪", "Tanzania": "🇹🇿",
    "Madagascar": "🇲🇬", "Namibia": "🇳🇦", "Mozambique": "🇲🇿",
    "Northern Ireland": "🏴", "Albania": "🇦🇱", "Georgia": "🇬🇪",
    "North Macedonia": "🇲🇰", "Montenegro": "🇲🇪", "Kosovo": "🇽🇰",
}


def flag(name: str) -> str:
    """Return '<emoji> <name>' if known, else just the name."""
    e = _FLAGS.get(name)
    return f"{e} {name}" if e else name
