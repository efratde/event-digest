from .barby import BarbyScraper
from .base import Scraper
from .caesarea import CaesareaScraper
from .cameri import CameriScraper
from .gesher import GesherScraper
from .habima import HabimaScraper
from .hasimta import HasimtaScraper
from .heichal_givatayim import HeichalGivatayimScraper
from .heichal_tlv import HeichalTlvScraper
from .lessin import LessinScraper
from .reading3 import Reading3Scraper
from .shuni import ShuniScraper
from .suzanne_dellal import SuzanneDellalScraper
from .tmuna import TmunaScraper
from .tzavta import TzavtaScraper
from .yoram_loewenstein import YoramLoewensteinScraper
from .zappa_herzliya import ZappaHerzliyaScraper
from .zappa_tlv import ZappaTlvScraper

REGISTRY: dict[str, type[Scraper]] = {
    "habima": HabimaScraper,
    "cameri": CameriScraper,
    "tzavta": TzavtaScraper,
    "lessin": LessinScraper,
    "zappa_tlv": ZappaTlvScraper,
    "zappa_herzliya": ZappaHerzliyaScraper,
    "shuni": ShuniScraper,
    "heichal_tlv": HeichalTlvScraper,
    "heichal_givatayim": HeichalGivatayimScraper,
    "gesher": GesherScraper,
    "caesarea": CaesareaScraper,
    "barby": BarbyScraper,
    "suzanne_dellal": SuzanneDellalScraper,
    "reading3": Reading3Scraper,
    "tmuna": TmunaScraper,
    "yoram_loewenstein": YoramLoewensteinScraper,
    "hasimta": HasimtaScraper,
}
