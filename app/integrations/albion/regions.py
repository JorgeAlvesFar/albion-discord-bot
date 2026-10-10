from urllib.parse import urlparse

# Host da API oficial -> base do Albion Online Data Project da mesma região (preços de mercado).
PRICES_BY_HOST = {
    "gameinfo.albiononline.com": "https://west.albion-online-data.com",
    "gameinfo-ams.albiononline.com": "https://europe.albion-online-data.com",
    "gameinfo-sgp.albiononline.com": "https://east.albion-online-data.com",
}
DEFAULT_HOST = "gameinfo.albiononline.com"

def prices_url_for(api_base_url: str) -> str:
    host = urlparse(api_base_url).hostname or ""
    return PRICES_BY_HOST.get(host, PRICES_BY_HOST[DEFAULT_HOST])
