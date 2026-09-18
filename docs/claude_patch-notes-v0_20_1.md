# Patch notes v0.20.1 (2026-09-17, 21:35 CT): sector cap back to 1.5 percent

Operator decision after the v0.20.0 discussion: the cap is on stop risk, not position size, so 1.5 percent (1,500 dollars on a 100k account) does not limit a 15k position and is the intended concentration guard. `config/risk.yaml` `capital.max_sector_heat_pct` 0.03 to 0.015. Kept from v0.20.0: net of direction counting (`sector_heat_mode: net`) and the scanner lane exemption (`scanner.sector_clip: false`). `a3-risk` restarted after the close. Rollback: set 0.03 and restart `a3-risk`.
