# Data source permission and service-level inventory

QuantLab does not treat technical accessibility as permission to redistribute or commercialize
data. Every evidence block records its source and degradation state.

| Source / adapter | Current use | Permission state | SLA | Production claim boundary |
|---|---|---|---|---|
| User-supplied licensed point-in-time files/API | ETF/A股/可转债 master, status and outcomes | User contract | Contract-specific | Eligible only with source version and `available_at` |
| BaoStock | A-share bars and metadata fallback | Review terms before commercial use | None | Research fallback; disclose source |
| AKShare | Public-page aggregation | Underlying websites have separate terms | None | Research fallback; no redistribution guarantee |
| westock local tool | Market data fallback | Depends on local source terms | None | Research fallback; explicit degradation |
| OpenAI / DeepSeek APIs | Model inference | Provider API terms | Provider-specific | No keys or raw prompts exported |
| Weekday calendar fallback | Scheduling estimate | Internal estimate | None | `degraded`; may be wrong on holidays |

Not reliably licensed or stable in the default free configuration:

- official historical ETF shares and net subscriptions;
- margin financing and securities lending history;
- historical northbound/cross-border flows and Dragon-Tiger List redistribution;
- official historical sector membership;
- complete regulatory inquiries, litigation and penalties;
- point-in-time convertible-bond redemption, balance and rating panels;
- matched event-study control panels.

When absent, APIs return `unavailable` or `degraded`. Data must never be synthetically filled and
described as observed facts. Commercial deployment should replace free adapters with licensed
providers and record contract/version metadata in point-in-time tables.

