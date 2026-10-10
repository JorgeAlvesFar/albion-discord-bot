from urllib.parse import urlparse

# Host da API oficial -> (rótulo exibido no card, base do Albion Online Data Project da mesma região).
REGIONS = {
    "gameinfo.albiononline.com": ("AMERICAS", "https://west.albion-online-data.com"),
    "gameinfo-ams.albiononline.com": ("EUROPE", "https://europe.albion-online-data.com"),
    "gameinfo-sgp.albiononline.com": ("ASIA", "https://east.albion-online-data.com"),
}
DEFAULT_HOST = "gameinfo.albiononline.com"

def region_for(api_base_url: str) -> tuple[str, str]:
    host = urlparse(api_base_url).hostname or ""
    return REGIONS.get(host, REGIONS[DEFAULT_HOST])
