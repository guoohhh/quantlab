# Third-party notices

QuantLab is distributed under the MIT License. Its Python dependency licenses remain those of
their respective projects.

| Component | Purpose | License / attribution action |
|---|---|---|
| pandas, NumPy | Data frames and numerical calculations | BSD-3-Clause; retain upstream notices |
| Pydantic | Domain and API validation | MIT |
| FastAPI, Typer, Rich | API and CLI | MIT |
| httpx | HTTP client | BSD-3-Clause |
| OpenAI Python SDK | OpenAI-compatible model access | Apache-2.0 |
| LangGraph | Optional agent orchestration | MIT |
| Streamlit, Plotly | Existing dashboard | Apache-2.0 / MIT |
| pytest, coverage.py, Ruff | Engineering quality tooling | MIT / Apache-2.0 / MIT |
| AKShare | Optional public-market data adapter | See upstream and each underlying source |
| BaoStock | Optional A-share data adapter | See BaoStock terms and upstream license |
| yfinance | Optional research adapter | Apache-2.0; Yahoo data terms still apply |

Reference repositories reviewed during product research are not bundled as QuantLab runtime
dependencies. Any future copied code must be identified file-by-file with its original license and
copyright notice.

Round 8 reviewed mechanism-level ideas from local copies of Qlib (experiment recorder),
TradingAgents (reflection/checkpoint/memory), ai-berkshire (thesis tracking), and
daily_stock_analysis (provider routing). QuantLab did not vendor those implementations. In
particular, no CC BY-NC or other non-commercial source code was copied into the runtime.

Round 9 continued only the same mechanism-level review while implementing the trust boundary,
checkpoint claim, Decision Run audit export, thesis revision and research-only scorecard in original
QuantLab code. No additional third-party runtime source was copied or vendored in this round.

This inventory is not legal advice. Before commercial distribution, pin dependencies, generate a
software bill of materials, and complete legal review.
