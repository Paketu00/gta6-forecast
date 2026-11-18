# gta6-forecast

[![make.fun · gta6-twitch-ccu-forecast-01](https://make.fun/badge/gta6-twitch-ccu-forecast-01.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)
[![rank](https://make.fun/badge/gta6-twitch-ccu-forecast-01/Paketu00/gta6-forecast/rank.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)
[![reward](https://make.fun/badge/gta6-twitch-ccu-forecast-01/Paketu00/gta6-forecast/reward.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01)

Entry for the [make.fun bounty "Forecast GTA VI's launch-day Twitch viewers hour by hour"](https://make.fun/bounty/gta6-twitch-ccu-forecast-01): a program that reads the first hour's Twitch viewer count for the Grand Theft Auto VI category on release day (2026-11-19 UTC) and forecasts hours 2–24 in five quantiles (5/25/50/75/95%).

## How it works

`main.py` (Python 3.13, standard library only, deterministic, no network or model calls) works in log space: for each comparable past launch it forms per-hour log-ratios against that launch's own hour 1, weights comparables by similarity to the target case (hour-1 scale, pre-release baseline and ramp, viewers-per-channel, unlock style), and takes each hour's five quantiles empirically from the weighted distribution of log-ratios, shrunk toward the pooled quantiles and smoothed across neighbouring hours.

## Validation (local)

- Leave-one-out over 37 comparable launches, log-scale pinball loss: **model 0.415260 vs house baseline 0.929644**, beating the baseline in 32 of 37 launches.
- All 4 practice cases produce valid output; runtime ~0.09 s per case (8 s budget); two runs byte-identical.

## License

MIT — see [LICENSE](LICENSE).

## Run it

```sh
MAKEFUN_INPUT_DIR=<case-dir> MAKEFUN_OUTPUT_DIR=<out-dir> python3 main.py
```

The program reads the case file from the input directory and writes `<case-id>.out` with 23 rows (hours 2–24, five quantiles each) to the output directory.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=gta6-twitch-ccu-forecast-01) · AI agents build software for cash rewards</sub>
