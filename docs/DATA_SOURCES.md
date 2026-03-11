# Legba: Global Data Sources Catalog

*Compiled: 2026-03-11 | 76+ sources across 11 categories*

---

## 1. GDELT (All APIs)

GDELT monitors print, broadcast, and web news worldwide in 65+ languages, updated every 15 minutes. **All APIs free, no key required.**

### 1.1 DOC 2.0 API (Full-Text Article Search)

- **Base URL**: `https://api.gdeltproject.org/api/v2/doc/doc`
- **Type**: JSON API
- **Coverage**: Global, 65+ languages, rolling 3-month window
- **Update**: Every 15 minutes

```
# Article list, last 24h
https://api.gdeltproject.org/api/v2/doc/doc?query="climate change"&mode=artlist&maxrecords=250&format=json&timespan=24h

# Volume timeline by day
https://api.gdeltproject.org/api/v2/doc/doc?query="conflict"&mode=timelinevol&format=json&timespan=3m

# Filter by source country
https://api.gdeltproject.org/api/v2/doc/doc?query=sourcecountry:CN sourcelang:English&mode=artlist&maxrecords=250&format=json

# Tone timeline
https://api.gdeltproject.org/api/v2/doc/doc?query="trade war"&mode=timelinetone&format=json&timespan=1m
```

Modes: artlist, timelinevol, timelinevolraw, timelinetone, timelinelang, timelinesourcecountry
Params: format (html/json/csv), maxrecords (up to 250), timespan (Xmin/Xh/Xd/Xm), sort, sourcelang, sourcecountry, domainis

### 1.2 GEO 2.0 API (Geographic News Mapping)

- **Base URL**: `https://api.gdeltproject.org/api/v2/geo/geo`
- **Type**: GeoJSON API

```
https://api.gdeltproject.org/api/v2/geo/geo?query="conflict"&mode=pointdata&format=geojson&timespan=24h
https://api.gdeltproject.org/api/v2/geo/geo?query="election"&mode=country&format=json
```

Modes: pointdata, pointheat, pointdensity, country

### 1.3 TV 2.0 API (Television News)

- **Base URL**: `https://api.gdeltproject.org/api/v2/tv/tv`
- **Coverage**: US national/local + select international TV (9+ years)

```
https://api.gdeltproject.org/api/v2/tv/tv?query="Ukraine"&market=National&mode=timelinevol&format=json&datanorm=perc&last24=yes
https://api.gdeltproject.org/api/v2/tv/tv?query="earthquake"&mode=clipgallery&format=json&maxrecords=50
```

### 1.4 Context 2.0 API (Sentence-Level Search)

- **Base URL**: `https://api.gdeltproject.org/api/v2/context/context`
- Sentence-level matches with text snippets

### 1.5 GKG GeoJSON API (Knowledge Graph Maps)

- **Base URL**: `https://api.gdeltproject.org/api/v1/gkg_geojson`
- **Update**: Hourly

```
https://api.gdeltproject.org/api/v1/gkg_geojson?query=theme:TERROR&counto=1000
```

### 1.6 Raw Data Files

- **Base**: `http://data.gdeltproject.org/gdeltv2/`
- **Master list**: `http://data.gdeltproject.org/gdeltv2/masterfilelist.txt`
- **Last update**: `http://data.gdeltproject.org/gdeltv2/lastupdate.txt`
- CSV (tab-delimited, gzip), every 15 minutes

---

## 2. Conflict & Security

| Source | Endpoint | Type | Key | Update | Coverage |
|--------|----------|------|-----|--------|----------|
| **ACLED** | `https://acleddata.com/api/acled/read` | JSON API | Free reg | Weekly | Global political violence + protest events with actors, fatalities, locations |
| **UCDP** | `https://ucdpapi.pcr.uu.se/api/<resource>/<version>` | JSON API | Free token | Annual+API | Global armed conflicts since 1946, battle deaths, dyads |
| **GTD/START** | `https://www.start.umd.edu/gtd/` | CSV download | Free reg | Annual | 200k+ terrorism incidents 1970-present |
| **SIPRI** | `https://armstransfers.sipri.org/` | CSV/scrape | No | Annual | Arms transfers since 1950, military expenditure since 1949 |
| **Crisis Group** | `https://www.crisisgroup.org/rss-0` | RSS | No | Monthly | 80+ active conflicts, escalation alerts |

ACLED example: `https://acleddata.com/api/acled/read?limit=100&event_date=2026-03-01|2026-03-10&event_date_where=BETWEEN`

---

## 3. Political & Governance

| Source | Endpoint | Type | Key | Coverage |
|--------|----------|------|-----|----------|
| **OpenSanctions** | `https://api.opensanctions.org/` | JSON API | Free non-commercial | 330+ sources: sanctions, PEPs, criminal entities |
| **V-Dem** | `https://v-dem.net/data/the-v-dem-dataset/` | CSV download | No | 202 countries, 531 indicators, 1789-present |
| **Freedom House** | `https://freedomhouse.org/country/scores` | CSV download | No | 208 countries, political rights + civil liberties |
| **World Bank WGI** | `https://api.worldbank.org/v2/country/all/indicator/CC.EST?format=json` | JSON API | No | Six governance dimensions, 200+ economies |
| **UN Digital Library** | `https://digitallibrary.un.org/rss` | RSS | No | UNSC resolutions, voting records |

---

## 4. Economic

| Source | Endpoint | Type | Key | Coverage |
|--------|----------|------|-----|----------|
| **World Bank** | `https://api.worldbank.org/v2/` | JSON API | No | ~16k indicators, 200+ countries |
| **IMF** | `http://dataservices.imf.org/REST/SDMX_JSON.svc/` | JSON (SDMX) | No | Global macro: GDP, inflation, BoP, FX |
| **FRED** | `https://api.stlouisfed.org/fred/` | JSON API | Free reg | 800k+ series, US + international |
| **UN Comtrade** | `https://comtradedeveloper.un.org/` | JSON API | Free reg | Global bilateral trade since 1988 |
| **Frankfurter** | `https://api.frankfurter.dev/v1/latest` | JSON API | No | ECB exchange rates, 30+ currencies |
| **API Ninjas Commodities** | `https://api.api-ninjas.com/v1/commodityprice` | JSON API | Free reg | Real-time oil, gold, wheat, etc. |

---

## 5. Health & Disease Surveillance

| Source | Endpoint | Type | Key | Update |
|--------|----------|------|-----|--------|
| **WHO Outbreaks** | `https://www.who.int/api/news/diseaseoutbreaknews` | JSON API + RSS | No | As outbreaks occur |
| **WHO GHO** | `https://ghoapi.azureedge.net/api/` | JSON (OData) | No | 2000+ indicators, all member states |
| **ProMED** | `https://promedmail.org/promed-rss-feed/` | RSS | No | Multiple daily |
| **CDC Open Data** | `https://data.cdc.gov/` | JSON (SODA) | No | US disease surveillance, mortality |
| **HealthMap** | `https://www.healthmap.org/` | Scrape | No | Hourly, automated outbreak detection |

WHO GHO examples:
```
https://ghoapi.azureedge.net/api/Indicator?$filter=contains(IndicatorName,'malaria')
https://ghoapi.azureedge.net/api/WHOSIS_000001?$filter=SpatialDim eq 'USA'
```

---

## 6. Environment & Climate

| Source | Endpoint | Type | Key | Update |
|--------|----------|------|-----|--------|
| **USGS Earthquakes** | `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson` | GeoJSON | No | Real-time (per minute) |
| **NASA EONET** | `https://eonet.gsfc.nasa.gov/api/v3/events?status=open` | GeoJSON | No | Continuous |
| **NASA FIRMS** | `https://firms.modaps.eosdis.nasa.gov/api/` | CSV/JSON | Free MAP_KEY | Near real-time (3h) |
| **NOAA/NWS Alerts** | `https://api.weather.gov/alerts/active` | JSON-LD | No | Real-time (US) |
| **Open-Meteo** | `https://api.open-meteo.com/v1/forecast` | JSON | No | Global weather + flood |
| **EMSC Seismology** | `https://www.emsc-csem.org/service/api/1.6/get.geojson?type=full` | GeoJSON + WebSocket | No | Real-time |
| **USGS Volcanoes** | `https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes` | JSON + RSS | No | Real-time |
| **Copernicus EMS** | `https://global-flood.emergency.copernicus.eu/` | Data download | Free reg | Global flood forecasting |

---

## 7. Disaster & Humanitarian

| Source | Endpoint | Type | Key | Update |
|--------|----------|------|-----|--------|
| **GDACS** | `https://www.gdacs.org/xml/rss.xml` | RSS + JSON API | No | Real-time |
| **ReliefWeb** | `https://api.reliefweb.int/v1/reports?appname=legba&limit=50` | JSON API | No (appname rec.) | Continuous |
| **HDX** | `https://data.humdata.org/api/3/` + HAPI: `https://hapi.humdata.org/api/v1/` | JSON API | Free reg (HAPI) | 18k+ datasets |
| **IFRC GO** | `https://goadmin.ifrc.org/api/v2/event/?limit=50&format=json` | JSON API | No | Emergency operations |
| **New Humanitarian** | `https://www.thenewhumanitarian.org/rss/all.xml` | RSS | No | Daily |

GDACS API: `https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH` (event types: EQ, TC, FL, VO, WF, DR)

---

## 8. Technology & Cyber

| Source | Endpoint | Type | Key | Coverage |
|--------|----------|------|-----|----------|
| **NVD 2.0** | `https://services.nvd.nist.gov/rest/json/cves/2.0` | JSON API | Optional free | CVEs, CVSS scores |
| **CISA KEV** | `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` | Static JSON | No | Actively exploited CVEs |
| **CISA Advisories** | `https://www.cisa.gov/news-events/cybersecurity-advisories.xml` | RSS | No | Cybersecurity advisories |
| **AlienVault OTX** | `https://otx.alienvault.com/api/v1/` | JSON API | Free reg | IOCs, threat pulses, malware |

---

## 9. OSINT & News Aggregation

| Source | Endpoint | Type | Key | Limits |
|--------|----------|------|-----|--------|
| **NewsAPI.ai (Event Registry)** | `https://eventregistry.org/api/v1/` | JSON API | Free tier | 2k tokens, 30-day lookback |
| **NewsAPI.org** | `https://newsapi.org/v2/` | JSON API | Free tier | 100 req/day |
| **WorldNewsAPI** | `https://api.worldnewsapi.com/` | JSON API | Free tier | 500 req/day |
| **Common Crawl News** | `https://data.commoncrawl.org/crawl-data/CC-NEWS/` | WARC/S3 | No | Bulk archive |

---

## 10. Regional / Non-Western Sources (RSS)

### East Asia
| Source | Feed URL |
|--------|----------|
| Xinhua (China) | `http://www.xinhuanet.com/english/rss/index.htm` |
| NHK World (Japan) | `https://www3.nhk.or.jp/nhkworld/data/en/news/rss.xml` |
| Kyodo News (Japan) | `https://english.kyodonews.net/rss/news.xml` |
| Yonhap (S. Korea) | `https://en.yna.co.kr/RSS/news.xml` |
| SCMP (Hong Kong) | `https://www.scmp.com/rss/4/feed` |
| Channel News Asia | `https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml` |

### Middle East & Eurasia
| Source | Feed URL |
|--------|----------|
| Al Jazeera English | `https://www.aljazeera.com/xml/rss/all.xml` |
| Anadolu Agency (Turkey) | `https://www.aa.com.tr/en/rss/default?cat=*` |
| TASS (Russia) | `https://tass.com/rss/v2.xml` |
| Tehran Times (Iran) | `https://www.tehrantimes.com/rss` |

### Africa
| Source | Feed URL |
|--------|----------|
| AllAfrica | `https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf` |
| Africanews | `https://www.africanews.com/feed/` |
| Punch (Nigeria) | `https://rss.punchng.com/v1/category/latest_news` |
| Nation Africa (Kenya) | `https://nation.africa/kenya/rss.xml` |

### South & Southeast Asia
| Source | Feed URL |
|--------|----------|
| Times of India | `https://timesofindia.indiatimes.com/rssfeedstopstories.cms` |
| Hindustan Times | `https://www.hindustantimes.com/feeds/rss/topnews/rssfeed.xml` |

### Latin America & Multi-Regional
| Source | Feed URL |
|--------|----------|
| MercoPress | `https://en.mercopress.com/rss` |
| Global Voices | `https://globalvoices.org/feed/` |
| VOA | `https://www.voanews.com/api/` |

---

## 11. Social & Human Rights

| Source | Endpoint | Type |
|--------|----------|------|
| Human Rights Watch | `https://www.hrw.org/rss/news` | RSS |
| Amnesty International | `https://www.amnesty.org/en/feed/` | RSS |
| CIVICUS Monitor | `https://monitor.civicus.org/updates/feed/` | RSS |
| RSF Press Freedom | `https://rsf.org/en/index` | Annual CSV |
| HRMI Rights Tracker | `https://rightstracker.org/` | Web/download |

---

## Priority Tiers for Implementation

### Tier 1: Immediate (No auth, JSON/RSS, real-time)
GDELT DOC API, USGS Earthquakes, GDACS, NWS Alerts, NASA EONET, WHO GHO, World Bank, Frankfurter, ReliefWeb, CISA KEV, ProMED, AllAfrica, NHK World, HRW, Amnesty, CIVICUS, Global Voices, Crisis Group, New Humanitarian

### Tier 2: High Value (Free registration)
ACLED, FRED, NASA FIRMS, AlienVault OTX, NVD 2.0, OpenSanctions, NewsAPI.ai, UCDP, UN Comtrade

### Tier 3: Nice to Have
Common Crawl, SIPRI (scrape), V-Dem (annual), GTD (annual), BigQuery GDELT
