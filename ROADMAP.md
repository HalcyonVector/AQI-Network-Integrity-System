# Roadmap

**Plan: do all the remaining work together in December.** By then the burning season has produced the data the
open questions need, and the pipelines will have been collecting for ~11 weeks. Nothing below is a build job
that must happen sooner. The only things that cannot wait are in *Before December*.

Everything already built is described in `README.md`. This file is only what is left and when.

---

## Before December (small, and some are time-sensitive)

| when | what | why it cannot wait |
|---|---|---|
| **by 26 Oct** | Decide about the **Copernicus CDS terms change on 28 Oct**. The CDS notice says continued use after 28 Oct counts as accepting the new terms, and to contact ECMWF before 27 Oct if you do not accept. | ERA5 is the weather input for the stubble and neighbour analyses. A hard deadline. |
| **~19 Oct** (optional) | Follow up on the data.gov.in outage report through a second channel. | The feed is back, so this is only about getting an explanation. Skip freely. |
| **ongoing** | **Keep the machine on at 12:00 and 19:00**, as much as possible. | The feed serves only its current snapshot, so a missed day is lost *permanently* from that feed. Every other source re-fetches a trailing window and heals itself. |
| **monthly glance** | Open `dashboard/index.html`. If a source shows **STALE** or **DOWN**, investigate. If **AOD** is stale, re-run `python -c "import ee; ee.Authenticate(auth_mode='localhost')"`. | Catches a silently broken pipeline within weeks, not at December. |
| optional | Regenerate the data.gov.in API key. | It appeared once in a log screenshot; logs now redact it. |

If none of this is done, the realistic loss is: feed days on which the machine was off.

---

## December work plan (in this order)

### Week 1 (1-7 Dec): pre-flight
| task | done when |
|---|---|
| Open the dashboard and audit every source for gaps. List the missing days. | A written list of holes, and which are recoverable. |
| Check whether the **OpenAQ archive resumed** past 12 Oct. | Know which PM source covers 13 Oct onward. |
| If it did, re-run `validate_feed_units.py` on the overlap from 13 Oct. | Confirms the feed's unit definition did not change after the outage (it returned with 13 new stations). |
| If it did not, validate the feed as Delhi's daily PM source: check its 24 h value equals OpenAQ's calendar-day mean on the pre-outage overlap. | Documented error; go / no-go on using the feed for dates OpenAQ lacks. |
| Confirm ERA5 files exist for all of 23 Sep onward, with `blh_m`, `u925_ms` and an `era5_precip_*` file per day. | No missing weather days. |

### Week 1-2: the stubble question (the real test)
The protocol is **fixed now**, so it cannot be tuned later:
- Exposure: 925 hPa winds, lag 0 (`hit_frp_925`).
- **Rain-free days only** (a day with >= 1 mm area-mean rain, plus the 2 days after, is excluded).
- Control: boundary-layer height + temperature (rank-linear partial correlation). Secondary: exposed days only.
- Judge with the **circular-shift test** (respects autocorrelation), not the iid permutation p.
- Needs **>= 20 rain-free days**, because the circular-shift p cannot go below ~1/n. At 10 days it cannot reach 0.05.
- Run it **once**. Do not re-run until one variant works.

| outcome | what to conclude |
|---|---|
| circular-shift p < 0.05 and rho stays positive | stubble transport contributes beyond rain and weather (still no magnitude) |
| not significant with >= 20 dry days | no evidence beyond rain/weather in this season; say so |
| fewer than 20 dry days | do not test; wait |

### Week 2: station health and the satellite
| task | done when |
|---|---|
| Re-run satellite consistency with a **4-week paired window** and FDR q < 0.10 (fixed in advance). | Result for IHBAS and the 8 borderline stations: confirmed, or dropped. |
| Run **drift detection** on the longest clean record (>= 6 weeks). The first run found only IHBAS on watch (a 4.5x apparent decline). | IHBAS confirmed or cleared; any new stations listed. |
| Re-run neighbour health. Re-check that "noisy" is judged against each station's isolation (a past flag was an artefact). | Flag list stable after the isolation adjustment. |

### Week 2-3: feed-only stations and fallback PM source
| task | done when |
|---|---|
| Daily-scale health checks for the **13 stations that exist only in the feed**, using `feed_concentrations.csv`. A weaker detector (no hourly neighbours); label it that way. | Each of the 13 has a verdict or "insufficient data". |
| If OpenAQ stayed stuck: wire the validated feed series in for dates OpenAQ lacks. | Delhi daily PM continuous through November. |

### Week 3: network-wide outage fill
| task | done when |
|---|---|
| Validate a **regional daily estimate from the satellite** for outages where no neighbours report, leave-one-day-out, on >= 30 clear-sky paired days. | A documented error, or "not good enough, do not publish". |

### Week 3-4: wrap-up
| task | done when |
|---|---|
| Update the dashboard with the December results. Decide whether to publish it (leave local / GitHub Pages / claude.ai artifact). | Decision recorded. |
| Maintenance: re-pull any ERA5T (preliminary, `expver 0005`) dates that now have final data; re-check the FIRMS sensor list after Suomi NPP stopped on 1 Nov. | Preliminary data replaced where available. |
| Revisit the unmatched stations (Pusa x2, Aya Nagar, North Campus, plus the 13 new ones) in case OpenAQ added them. | Mapping updated or confirmed unchanged. |
| Refresh `README.md`: results, retractions, new limits. | README matches the code and data. |

---

## Why December is a reasonable choice (and what it risks)

**In favour:** the stubble test needs ~20 dry days; station-health and drift need 4-8 weeks. All of that is simply time.
Running them once, late, with a fixed protocol also avoids the main statistical hazard here (re-testing as data
arrives and stopping when it looks good). Burning season also ends around late November, so December has the whole
season in hand.

**Risks of waiting, all manageable:**
1. **Feed days lost** if the machine is off (see *Before December*). The only irreversible one.
2. **OpenAQ stays stuck.** Hourly neighbour analysis then stops at 12 Oct; daily-scale work continues via the feed.
3. **Earth Engine login lapses.** AOD silently stops; the dashboard shows STALE.
4. **CDS terms** (deadline 27 Oct) if not decided.
5. **ERA5T revisions**: preliminary values can change slightly when final data replaces them.

## Definition of "done" for the project
- The stubble question is answered under the fixed protocol (yes / no / not enough data), with the answer stated plainly.
- Every flagged station is confirmed, cleared, or explicitly "unresolved".
- The dashboard shows every source OK or explains why not.
- The README records every retraction and limit alongside the findings.
