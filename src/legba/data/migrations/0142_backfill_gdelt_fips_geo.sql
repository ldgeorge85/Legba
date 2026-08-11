-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0142_backfill_gdelt_fips_geo.sql
--
-- B-6 backfill -- re-country the GDELT rows the FIPS/ISO confusion mis-desked.
--
-- WHAT WAS WRONG:
--   `gdelt_files.row_to_signal` stamped GDELT's `ActionGeo_CountryCode` -- a
--   FIPS 10-4 code -- straight into `signals.geo`, which every country desk
--   subscribes on as ISO 3166-1 alpha-2 (`geo && ARRAY['XX']`). Both codelists
--   are two uppercase letters and agree about half the time, so this never
--   raised anything; it just delivered stories to the wrong desk. The handler is
--   fixed forward in the same commit train (`legba.data._fips_iso`); this
--   migration repairs the rows already carrying a FIPS code in their geo array.
--
-- MEASURED on the live substrate 2026-08-03, across 9,350 `source.gdelt.files`
-- rows:
--     7,878  carry the raw FIPS code in the indexed `geo` array
--     3,121  carry one that means a DIFFERENT country as ISO  <- repaired here
--     1,617    ... of those, a code that is a VALID ISO code for another country
--        90    ... of THOSE, delivered to a desk that EXISTS and is wrong:
--                 BG  45  Bangladesh stories on the Bulgaria desk
--                 AU  11  Austria      on Australia      NG  10  Niger  on Nigeria
--                 BF   9  Bahamas      on Burkina Faso   MN   4  Monaco on Mongolia
--                 BH   4  Belize       on Bahrain        SE   3  Seychelles on Sweden
--                 CD   2  Chad         on DR Congo       LT   1  Lesotho on Lithuania
--                 GB   1  Gabon        on the UK
--               (An earlier count of 129 also swept in codes where FIPS and ISO
--               AGREE and only the geometry differed — e.g. a TW-coded row whose
--               lat/lon resolved to the CN-TW subdivision. Those are not
--               misroutes and this migration does not touch them: the crosswalk
--               below carries non-identity entries only.)
--   Worst offenders: CH (China -> read as Switzerland, 186), RS (Russia ->
--   Serbia, 153), GM (Germany -> Gambia, 54), BG (Bangladesh -> Bulgaria, 44).
--   The quieter half never arrives anywhere: UK (487 rows; ISO says GB), SP
--   (Spain, 210), UP (Ukraine, 106) match no desk at all.
--
-- WHY THIS SET IS SAFE TO REWRITE, precisely:
--   The join is pinned to each row's OWN `payload.geo.country_code_fips` -- the
--   raw value the handler stamped -- so only a code this handler put there can
--   be removed. A code the geocode filter independently resolved from the row's
--   lat/lon is never touched, and the
--   `fips IS DISTINCT FROM payload.geo.country_iso2` guard additionally skips
--   any row where geometry INDEPENDENTLY agreed with the FIPS letters (a China
--   story that really is in Switzerland keeps its CH).
--
--   2,982 of the 3,121 already carry the correct ISO code alongside the wrong
--   one -- the geocode filter appended it and the promote step never removed the
--   original (the append-only semantics migration 0062 documented). For those
--   the repair is purely a REMOVAL. The remaining ~139 gain the crosswalked
--   code, so no row is left with an empty `geo`.
--
-- THE CROSSWALK BELOW IS GENERATED from `legba.data._fips_iso.FIPS_TO_ISO2`
-- (non-identity entries only -- an identity mapping has nothing to repair).
-- `tests/data_pkg/test_fips_iso_crosswalk_migration.py` re-derives it from the
-- module and fails if the two ever disagree, so the SQL copy cannot drift from
-- the code that will be applied to every future row.
--
-- IDEMPOTENT: the `fips = ANY(geo)` predicate is self-extinguishing. There is no
-- cascade risk either -- the join pins `fips` to the row's own stamped value, so
-- a second pass cannot re-translate an already-corrected code (e.g. AS -> AU
-- does not then read AU as Austria).

UPDATE public.signals s
   SET geo = CASE
                 WHEN x.iso2 = ANY(s.geo) THEN array_remove(s.geo, x.fips)
                 ELSE array_append(array_remove(s.geo, x.fips), x.iso2)
             END,
       updated_at = NOW()
  FROM (VALUES
    ('AA','AW'), ('AC','AG'), ('AG','DZ'), ('AJ','AZ'), ('AN','AD'), ('AQ','AS'), ('AS','AU'), ('AT','AU'),
    ('AU','AT'), ('AV','AI'), ('AY','AQ'), ('BA','BH'), ('BC','BW'), ('BD','BM'), ('BF','BS'), ('BG','BD'),
    ('BH','BZ'), ('BK','BA'), ('BL','BO'), ('BM','MM'), ('BN','BJ'), ('BO','BY'), ('BP','SB'), ('BQ','UM'),
    ('BU','BG'), ('BX','BN'), ('BY','BI'), ('CB','KH'), ('CD','TD'), ('CE','LK'), ('CF','CG'), ('CG','CD'),
    ('CH','CN'), ('CI','CL'), ('CJ','KY'), ('CK','CC'), ('CN','KM'), ('CQ','MP'), ('CR','AU'), ('CS','CR'),
    ('CT','CF'), ('CW','CK'), ('DA','DK'), ('DO','DM'), ('DQ','UM'), ('DR','DO'), ('EI','IE'), ('EK','GQ'),
    ('EN','EE'), ('ES','SV'), ('EZ','CZ'), ('FG','GF'), ('FP','PF'), ('FS','TF'), ('GA','GM'), ('GB','GA'),
    ('GG','GE'), ('GJ','GD'), ('GK','GG'), ('GM','DE'), ('GQ','GU'), ('GV','GN'), ('GZ','PS'), ('HA','HT'),
    ('HO','HN'), ('HQ','UM'), ('IC','IS'), ('IS','IL'), ('IV','CI'), ('IZ','IQ'), ('JA','JP'), ('JN','SJ'),
    ('JQ','UM'), ('KN','KP'), ('KQ','UM'), ('KR','KI'), ('KS','KR'), ('KT','CX'), ('KU','KW'), ('KV','XK'),
    ('LE','LB'), ('LG','LV'), ('LH','LT'), ('LI','LR'), ('LO','SK'), ('LS','LI'), ('LT','LS'), ('MA','MG'),
    ('MB','MQ'), ('MC','MO'), ('MF','YT'), ('MG','MN'), ('MH','MS'), ('MI','MW'), ('MJ','ME'), ('MN','MC'),
    ('MO','MA'), ('MP','MU'), ('MQ','UM'), ('MU','OM'), ('NE','NU'), ('NG','NE'), ('NH','VU'), ('NI','NG'),
    ('NS','SR'), ('NU','NI'), ('OD','SS'), ('PA','PY'), ('PC','PN'), ('PM','PA'), ('PO','PT'), ('PP','PG'),
    ('PS','PW'), ('PU','GW'), ('RI','RS'), ('RM','MH'), ('RN','MF'), ('RP','PH'), ('RQ','PR'), ('RS','RU'),
    ('SB','PM'), ('SC','KN'), ('SE','SC'), ('SF','ZA'), ('SG','SN'), ('SN','SG'), ('SP','ES'), ('ST','LC'),
    ('SU','SD'), ('SV','SJ'), ('SW','SE'), ('SX','GS'), ('SZ','CH'), ('TB','BL'), ('TD','TT'), ('TI','TJ'),
    ('TK','TC'), ('TL','TK'), ('TN','TO'), ('TO','TG'), ('TP','ST'), ('TS','TN'), ('TT','TL'), ('TU','TR'),
    ('TX','TM'), ('UK','GB'), ('UP','UA'), ('UV','BF'), ('VI','VG'), ('VM','VN'), ('VQ','VI'), ('VT','VA'),
    ('WA','NA'), ('WE','PS'), ('WI','EH'), ('WQ','UM'), ('WZ','SZ'), ('YM','YE'), ('ZA','ZM'), ('ZI','ZW')
       ) AS x(fips, iso2)
 WHERE s.source_id = 'source.gdelt.files'
   AND x.fips = s.payload->'geo'->>'country_code_fips'
   AND x.fips = ANY(s.geo)
   AND x.fips IS DISTINCT FROM s.payload->'geo'->>'country_iso2';
