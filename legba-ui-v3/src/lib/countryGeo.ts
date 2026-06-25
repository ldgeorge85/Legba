/**
 * Country-name → geo resolver (UI-side NER geo backfill).
 *
 * The NER + entity-linking pipeline classes country mentions as the generic
 * `entity` class and leaves `geo_lat/geo_lon/geo_country` unset, so countries
 * never place on the map and the entity graph paints them slate-grey alongside
 * every other un-typed mention. Until the backend gazetteer lands, this module
 * is the pragmatic fix: a deterministic name→{iso2, lat, lon} lookup over a
 * country gazetteer (UN members + common short-forms/aliases), so a recognized
 * country entity can be:
 *   - promoted to the `location` class (graph color + facet), and
 *   - given a representative centroid so it becomes a map marker.
 *
 * Pure + deterministic → unit-testable without WebGL. Centroids are coarse
 * country-representative points (not population-weighted); they only need to
 * land the marker in the right country for the source-first map overlay.
 */

/** One gazetteer entry: ISO-3166-1 alpha-2 + a representative centroid. */
export interface CountryFix {
  iso2: string
  /** Canonical English country name (for the display / count bucket). */
  name: string
  lat: number
  lon: number
}

/**
 * iso2 → {name, lat, lon}. Centroids are coarse country-representative points.
 * Keyed on the upper-case ISO-2 code; the name lookup below normalizes aliases
 * onto these codes.
 */
const BY_ISO2: Record<string, CountryFix> = {
  AF: { iso2: 'AF', name: 'Afghanistan', lat: 33.94, lon: 67.71 },
  AL: { iso2: 'AL', name: 'Albania', lat: 41.15, lon: 20.17 },
  DZ: { iso2: 'DZ', name: 'Algeria', lat: 28.03, lon: 1.66 },
  AO: { iso2: 'AO', name: 'Angola', lat: -11.2, lon: 17.87 },
  AR: { iso2: 'AR', name: 'Argentina', lat: -38.42, lon: -63.62 },
  AM: { iso2: 'AM', name: 'Armenia', lat: 40.07, lon: 45.04 },
  AU: { iso2: 'AU', name: 'Australia', lat: -25.27, lon: 133.78 },
  AT: { iso2: 'AT', name: 'Austria', lat: 47.52, lon: 14.55 },
  AZ: { iso2: 'AZ', name: 'Azerbaijan', lat: 40.14, lon: 47.58 },
  BH: { iso2: 'BH', name: 'Bahrain', lat: 25.93, lon: 50.64 },
  BD: { iso2: 'BD', name: 'Bangladesh', lat: 23.68, lon: 90.36 },
  BY: { iso2: 'BY', name: 'Belarus', lat: 53.71, lon: 27.95 },
  BE: { iso2: 'BE', name: 'Belgium', lat: 50.5, lon: 4.47 },
  BJ: { iso2: 'BJ', name: 'Benin', lat: 9.31, lon: 2.32 },
  BO: { iso2: 'BO', name: 'Bolivia', lat: -16.29, lon: -63.59 },
  BA: { iso2: 'BA', name: 'Bosnia and Herzegovina', lat: 43.92, lon: 17.68 },
  BW: { iso2: 'BW', name: 'Botswana', lat: -22.33, lon: 24.68 },
  BR: { iso2: 'BR', name: 'Brazil', lat: -14.24, lon: -51.93 },
  BG: { iso2: 'BG', name: 'Bulgaria', lat: 42.73, lon: 25.49 },
  BF: { iso2: 'BF', name: 'Burkina Faso', lat: 12.24, lon: -1.56 },
  KH: { iso2: 'KH', name: 'Cambodia', lat: 12.57, lon: 104.99 },
  CM: { iso2: 'CM', name: 'Cameroon', lat: 7.37, lon: 12.35 },
  CA: { iso2: 'CA', name: 'Canada', lat: 56.13, lon: -106.35 },
  CF: { iso2: 'CF', name: 'Central African Republic', lat: 6.61, lon: 20.94 },
  TD: { iso2: 'TD', name: 'Chad', lat: 15.45, lon: 18.73 },
  CL: { iso2: 'CL', name: 'Chile', lat: -35.68, lon: -71.54 },
  CN: { iso2: 'CN', name: 'China', lat: 35.86, lon: 104.2 },
  CO: { iso2: 'CO', name: 'Colombia', lat: 4.57, lon: -74.3 },
  CD: { iso2: 'CD', name: 'DR Congo', lat: -4.04, lon: 21.76 },
  CG: { iso2: 'CG', name: 'Congo', lat: -0.23, lon: 15.83 },
  CR: { iso2: 'CR', name: 'Costa Rica', lat: 9.75, lon: -83.75 },
  CI: { iso2: 'CI', name: "Côte d'Ivoire", lat: 7.54, lon: -5.55 },
  HR: { iso2: 'HR', name: 'Croatia', lat: 45.1, lon: 15.2 },
  CU: { iso2: 'CU', name: 'Cuba', lat: 21.52, lon: -77.78 },
  CY: { iso2: 'CY', name: 'Cyprus', lat: 35.13, lon: 33.43 },
  CZ: { iso2: 'CZ', name: 'Czechia', lat: 49.82, lon: 15.47 },
  DK: { iso2: 'DK', name: 'Denmark', lat: 56.26, lon: 9.5 },
  DO: { iso2: 'DO', name: 'Dominican Republic', lat: 18.74, lon: -70.16 },
  EC: { iso2: 'EC', name: 'Ecuador', lat: -1.83, lon: -78.18 },
  EG: { iso2: 'EG', name: 'Egypt', lat: 26.82, lon: 30.8 },
  SV: { iso2: 'SV', name: 'El Salvador', lat: 13.79, lon: -88.9 },
  EE: { iso2: 'EE', name: 'Estonia', lat: 58.6, lon: 25.01 },
  ET: { iso2: 'ET', name: 'Ethiopia', lat: 9.15, lon: 40.49 },
  FI: { iso2: 'FI', name: 'Finland', lat: 61.92, lon: 25.75 },
  FR: { iso2: 'FR', name: 'France', lat: 46.23, lon: 2.21 },
  GA: { iso2: 'GA', name: 'Gabon', lat: -0.8, lon: 11.61 },
  GE: { iso2: 'GE', name: 'Georgia', lat: 42.32, lon: 43.36 },
  DE: { iso2: 'DE', name: 'Germany', lat: 51.17, lon: 10.45 },
  GH: { iso2: 'GH', name: 'Ghana', lat: 7.95, lon: -1.02 },
  GR: { iso2: 'GR', name: 'Greece', lat: 39.07, lon: 21.82 },
  GT: { iso2: 'GT', name: 'Guatemala', lat: 15.78, lon: -90.23 },
  GN: { iso2: 'GN', name: 'Guinea', lat: 9.95, lon: -9.7 },
  HT: { iso2: 'HT', name: 'Haiti', lat: 18.97, lon: -72.29 },
  HN: { iso2: 'HN', name: 'Honduras', lat: 15.2, lon: -86.24 },
  HU: { iso2: 'HU', name: 'Hungary', lat: 47.16, lon: 19.5 },
  IS: { iso2: 'IS', name: 'Iceland', lat: 64.96, lon: -19.02 },
  IN: { iso2: 'IN', name: 'India', lat: 20.59, lon: 78.96 },
  ID: { iso2: 'ID', name: 'Indonesia', lat: -0.79, lon: 113.92 },
  IR: { iso2: 'IR', name: 'Iran', lat: 32.43, lon: 53.69 },
  IQ: { iso2: 'IQ', name: 'Iraq', lat: 33.22, lon: 43.68 },
  IE: { iso2: 'IE', name: 'Ireland', lat: 53.41, lon: -8.24 },
  IL: { iso2: 'IL', name: 'Israel', lat: 31.05, lon: 34.85 },
  IT: { iso2: 'IT', name: 'Italy', lat: 41.87, lon: 12.57 },
  JM: { iso2: 'JM', name: 'Jamaica', lat: 18.11, lon: -77.3 },
  JP: { iso2: 'JP', name: 'Japan', lat: 36.2, lon: 138.25 },
  JO: { iso2: 'JO', name: 'Jordan', lat: 30.59, lon: 36.24 },
  KZ: { iso2: 'KZ', name: 'Kazakhstan', lat: 48.02, lon: 66.92 },
  KE: { iso2: 'KE', name: 'Kenya', lat: -0.02, lon: 37.91 },
  KP: { iso2: 'KP', name: 'North Korea', lat: 40.34, lon: 127.51 },
  KR: { iso2: 'KR', name: 'South Korea', lat: 35.91, lon: 127.77 },
  KW: { iso2: 'KW', name: 'Kuwait', lat: 29.31, lon: 47.48 },
  KG: { iso2: 'KG', name: 'Kyrgyzstan', lat: 41.2, lon: 74.77 },
  LA: { iso2: 'LA', name: 'Laos', lat: 19.86, lon: 102.5 },
  LV: { iso2: 'LV', name: 'Latvia', lat: 56.88, lon: 24.6 },
  LB: { iso2: 'LB', name: 'Lebanon', lat: 33.85, lon: 35.86 },
  LY: { iso2: 'LY', name: 'Libya', lat: 26.34, lon: 17.23 },
  LT: { iso2: 'LT', name: 'Lithuania', lat: 55.17, lon: 23.88 },
  LU: { iso2: 'LU', name: 'Luxembourg', lat: 49.82, lon: 6.13 },
  MG: { iso2: 'MG', name: 'Madagascar', lat: -18.77, lon: 46.87 },
  MW: { iso2: 'MW', name: 'Malawi', lat: -13.25, lon: 34.3 },
  MY: { iso2: 'MY', name: 'Malaysia', lat: 4.21, lon: 101.98 },
  ML: { iso2: 'ML', name: 'Mali', lat: 17.57, lon: -4.0 },
  MR: { iso2: 'MR', name: 'Mauritania', lat: 21.01, lon: -10.94 },
  MX: { iso2: 'MX', name: 'Mexico', lat: 23.63, lon: -102.55 },
  MD: { iso2: 'MD', name: 'Moldova', lat: 47.41, lon: 28.37 },
  MN: { iso2: 'MN', name: 'Mongolia', lat: 46.86, lon: 103.85 },
  ME: { iso2: 'ME', name: 'Montenegro', lat: 42.71, lon: 19.37 },
  MA: { iso2: 'MA', name: 'Morocco', lat: 31.79, lon: -7.09 },
  MZ: { iso2: 'MZ', name: 'Mozambique', lat: -18.67, lon: 35.53 },
  MM: { iso2: 'MM', name: 'Myanmar', lat: 21.91, lon: 95.96 },
  NA: { iso2: 'NA', name: 'Namibia', lat: -22.96, lon: 18.49 },
  NP: { iso2: 'NP', name: 'Nepal', lat: 28.39, lon: 84.12 },
  NL: { iso2: 'NL', name: 'Netherlands', lat: 52.13, lon: 5.29 },
  NZ: { iso2: 'NZ', name: 'New Zealand', lat: -40.9, lon: 174.89 },
  NI: { iso2: 'NI', name: 'Nicaragua', lat: 12.87, lon: -85.21 },
  NE: { iso2: 'NE', name: 'Niger', lat: 17.61, lon: 8.08 },
  NG: { iso2: 'NG', name: 'Nigeria', lat: 9.08, lon: 8.68 },
  MK: { iso2: 'MK', name: 'North Macedonia', lat: 41.61, lon: 21.75 },
  NO: { iso2: 'NO', name: 'Norway', lat: 60.47, lon: 8.47 },
  OM: { iso2: 'OM', name: 'Oman', lat: 21.51, lon: 55.92 },
  PK: { iso2: 'PK', name: 'Pakistan', lat: 30.38, lon: 69.35 },
  PS: { iso2: 'PS', name: 'Palestine', lat: 31.95, lon: 35.23 },
  PA: { iso2: 'PA', name: 'Panama', lat: 8.54, lon: -80.78 },
  PG: { iso2: 'PG', name: 'Papua New Guinea', lat: -6.31, lon: 143.96 },
  PY: { iso2: 'PY', name: 'Paraguay', lat: -23.44, lon: -58.44 },
  PE: { iso2: 'PE', name: 'Peru', lat: -9.19, lon: -75.02 },
  PH: { iso2: 'PH', name: 'Philippines', lat: 12.88, lon: 121.77 },
  PL: { iso2: 'PL', name: 'Poland', lat: 51.92, lon: 19.15 },
  PT: { iso2: 'PT', name: 'Portugal', lat: 39.4, lon: -8.22 },
  QA: { iso2: 'QA', name: 'Qatar', lat: 25.35, lon: 51.18 },
  RO: { iso2: 'RO', name: 'Romania', lat: 45.94, lon: 24.97 },
  RU: { iso2: 'RU', name: 'Russia', lat: 61.52, lon: 105.32 },
  RW: { iso2: 'RW', name: 'Rwanda', lat: -1.94, lon: 29.87 },
  SA: { iso2: 'SA', name: 'Saudi Arabia', lat: 23.89, lon: 45.08 },
  SN: { iso2: 'SN', name: 'Senegal', lat: 14.5, lon: -14.45 },
  RS: { iso2: 'RS', name: 'Serbia', lat: 44.02, lon: 21.01 },
  SL: { iso2: 'SL', name: 'Sierra Leone', lat: 8.46, lon: -11.78 },
  SG: { iso2: 'SG', name: 'Singapore', lat: 1.35, lon: 103.82 },
  SK: { iso2: 'SK', name: 'Slovakia', lat: 48.67, lon: 19.7 },
  SI: { iso2: 'SI', name: 'Slovenia', lat: 46.15, lon: 14.99 },
  SO: { iso2: 'SO', name: 'Somalia', lat: 5.15, lon: 46.2 },
  ZA: { iso2: 'ZA', name: 'South Africa', lat: -30.56, lon: 22.94 },
  SS: { iso2: 'SS', name: 'South Sudan', lat: 6.88, lon: 31.31 },
  ES: { iso2: 'ES', name: 'Spain', lat: 40.46, lon: -3.75 },
  LK: { iso2: 'LK', name: 'Sri Lanka', lat: 7.87, lon: 80.77 },
  SD: { iso2: 'SD', name: 'Sudan', lat: 12.86, lon: 30.22 },
  SE: { iso2: 'SE', name: 'Sweden', lat: 60.13, lon: 18.64 },
  CH: { iso2: 'CH', name: 'Switzerland', lat: 46.82, lon: 8.23 },
  SY: { iso2: 'SY', name: 'Syria', lat: 34.8, lon: 38.997 },
  TW: { iso2: 'TW', name: 'Taiwan', lat: 23.7, lon: 120.96 },
  TJ: { iso2: 'TJ', name: 'Tajikistan', lat: 38.86, lon: 71.28 },
  TZ: { iso2: 'TZ', name: 'Tanzania', lat: -6.37, lon: 34.89 },
  TH: { iso2: 'TH', name: 'Thailand', lat: 15.87, lon: 100.99 },
  TG: { iso2: 'TG', name: 'Togo', lat: 8.62, lon: 0.82 },
  TT: { iso2: 'TT', name: 'Trinidad and Tobago', lat: 10.69, lon: -61.22 },
  TN: { iso2: 'TN', name: 'Tunisia', lat: 33.89, lon: 9.54 },
  TR: { iso2: 'TR', name: 'Turkey', lat: 38.96, lon: 35.24 },
  TM: { iso2: 'TM', name: 'Turkmenistan', lat: 38.97, lon: 59.56 },
  UG: { iso2: 'UG', name: 'Uganda', lat: 1.37, lon: 32.29 },
  UA: { iso2: 'UA', name: 'Ukraine', lat: 48.38, lon: 31.17 },
  AE: { iso2: 'AE', name: 'United Arab Emirates', lat: 23.42, lon: 53.85 },
  GB: { iso2: 'GB', name: 'United Kingdom', lat: 55.38, lon: -3.44 },
  US: { iso2: 'US', name: 'United States', lat: 37.09, lon: -95.71 },
  UY: { iso2: 'UY', name: 'Uruguay', lat: -32.52, lon: -55.77 },
  UZ: { iso2: 'UZ', name: 'Uzbekistan', lat: 41.38, lon: 64.59 },
  VE: { iso2: 'VE', name: 'Venezuela', lat: 6.42, lon: -66.59 },
  VN: { iso2: 'VN', name: 'Vietnam', lat: 14.06, lon: 108.28 },
  YE: { iso2: 'YE', name: 'Yemen', lat: 15.55, lon: 48.52 },
  ZM: { iso2: 'ZM', name: 'Zambia', lat: -13.13, lon: 27.85 },
  ZW: { iso2: 'ZW', name: 'Zimbabwe', lat: -19.02, lon: 29.15 },
}

/**
 * Alias → iso2. Maps common short-forms, demonyms, and historical/colloquial
 * names onto the canonical ISO-2 code. The lookup also accepts a bare ISO-2 or
 * the canonical name (lowercased) directly via {@link BY_ISO2}.
 */
const ALIAS_TO_ISO2: Record<string, string> = {
  usa: 'US',
  'u.s.': 'US',
  'u.s.a.': 'US',
  us: 'US',
  america: 'US',
  'united states of america': 'US',
  uk: 'GB',
  'u.k.': 'GB',
  britain: 'GB',
  'great britain': 'GB',
  england: 'GB',
  uae: 'AE',
  emirates: 'AE',
  drc: 'CD',
  'dr congo': 'CD',
  'democratic republic of the congo': 'CD',
  'democratic republic of congo': 'CD',
  'republic of the congo': 'CG',
  'republic of korea': 'KR',
  'south korea': 'KR',
  'north korea': 'KP',
  "democratic people's republic of korea": 'KP',
  'ivory coast': 'CI',
  burma: 'MM',
  'czech republic': 'CZ',
  macedonia: 'MK',
  holland: 'NL',
  'the netherlands': 'NL',
  russia: 'RU',
  'russian federation': 'RU',
  iran: 'IR',
  'islamic republic of iran': 'IR',
  syria: 'SY',
  'syrian arab republic': 'SY',
  vietnam: 'VN',
  'viet nam': 'VN',
  laos: 'LA',
  'south sudan': 'SS',
  swaziland: 'ZA', // dropped from gazetteer; nearest neighbour for coarse placement
  turkiye: 'TR',
  türkiye: 'TR',
  palestine: 'PS',
  'state of palestine': 'PS',
  'west bank': 'PS',
  gaza: 'PS',
  'bosnia and herzegovina': 'BA',
  bosnia: 'BA',
}

/** Name index built once from BY_ISO2 (canonical names, lowercased). */
const NAME_TO_ISO2: Record<string, string> = (() => {
  const out: Record<string, string> = {}
  for (const fix of Object.values(BY_ISO2)) out[fix.name.toLowerCase()] = fix.iso2
  return out
})()

/**
 * Resolve a free-text name (entity canonical_name, country string, or bare
 * ISO-2 code) to a {@link CountryFix}, or null if it is not a recognized
 * country. Matching is case-insensitive and trims surrounding whitespace /
 * the leading article "the".
 */
export function resolveCountry(name: string | null | undefined): CountryFix | null {
  if (!name) return null
  const raw = name.trim()
  if (!raw) return null
  const lower = raw.toLowerCase().replace(/^the\s+/, '')

  // bare ISO-2 code
  if (raw.length === 2) {
    const fix = BY_ISO2[raw.toUpperCase()]
    if (fix) return fix
  }
  const byName = NAME_TO_ISO2[lower]
  if (byName) return BY_ISO2[byName]
  const byAlias = ALIAS_TO_ISO2[lower]
  if (byAlias) return BY_ISO2[byAlias]
  return null
}

/** True when the name resolves to a known country. */
export function isCountry(name: string | null | undefined): boolean {
  return resolveCountry(name) !== null
}

export { BY_ISO2 as COUNTRY_BY_ISO2 }
