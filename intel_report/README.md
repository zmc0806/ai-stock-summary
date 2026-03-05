# World Intelligence Report Generator

Combines geopolitical risk + market/finance news from 30+ RSS feeds,
analyzed by a local Ollama LLM (qwen3:14b) into a structured intelligence report.

## Report Structure

```
Executive Summary          ← fused geo+market paragraph, 150-200 words, ends with action
├── Geopolitical Risk
│   ├── Overall Risk Level (CRITICAL/HIGH/MEDIUM/LOW)
│   ├── Regional Hotspots (up to 3 regions)
│   ├── Trend Signals
│   └── Market Transmission (how geo events hit asset prices)
├── Market & Economic Direction
│   ├── Market Sentiment (Risk-On/Risk-Off/Neutral)
│   ├── Key Macro Signals table
│   ├── Commodities & FX Focus (crude / gold / DXY)
│   ├── Key Events to Watch This Week
│   └── Tail Risk Alert
└── Raw News Signals       ← all headlines sorted by threat level
```

## Setup

```bash
# 1. Install Python dependency
pip install requests

# 2. Ensure Ollama is running with qwen3:14b
ollama serve
ollama pull qwen3:14b   # if not already downloaded
```

## Run

```bash
cd intel_report

# Standard run — saves report to ./reports/
python intelligence_report.py

# Custom output directory
python intelligence_report.py --output-dir D:/my_reports

# Console only, no file saved
python intelligence_report.py --no-file

# Use a different model
python intelligence_report.py --model qwen3:8b
```

## Output

Reports are saved as Markdown files in `./reports/`:
```
reports/
└── intel_report_20260304_1430.md
```

## Configuration

Edit the top of `intelligence_report.py` to adjust:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_MODEL` | `qwen3:14b` | Ollama model |
| `ITEMS_PER_FEED` | `5` | Articles per RSS feed |
| `MAX_HEADLINES_GEO` | `30` | Headlines sent to geo LLM call |
| `MAX_HEADLINES_ECON` | `25` | Headlines sent to econ LLM call |
| `FETCH_TIMEOUT` | `12` | Seconds per feed request |

Or via environment variables:
```bash
OLLAMA_MODEL=qwen3:8b OLLAMA_BASE_URL=http://localhost:11434 python intelligence_report.py
```

## Feed Sources

**Geopolitical (9 categories):** BBC World, Guardian, Al Jazeera, France 24, DW, NPR, ABC,
EuroNews, Le Monde, The Diplomat, CNA, CrisisWatch, IAEA, WHO, UN News,
Foreign Policy, Atlantic Council, Foreign Affairs, Federal Reserve, SEC

**Finance (5 categories):** CNBC, Yahoo Finance, Seeking Alpha, Financial Times, WSJ,
Oil Price, CoinDesk, Cointelegraph, Federal Reserve, SEC
