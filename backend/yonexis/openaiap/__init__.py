"""OpenAIAP: web automation (RPA) for broker web terminals, driven by the YONEXIS AI zone rules.

Parallel to the MT5 tab. Instead of an Expert Advisor, orders are placed by replaying browser flows that the admin
records in the OpenAIAP tab against their broker's web login (web ID):
  * browser.py  - one headless Chromium (Playwright) inside the api container, with a persistent profile so the web
                  login survives restarts. The tab shows its screen; clicks / typing there are performed AND recorded.
  * player.py   - replays a recorded flow with variables ({symbol}, {qty}, {side}, {price}, {sl}, {target} ...)
                  and secrets ({secret:password}).
  * executor.py - looks like the MT5 bridge to yonexis.mt5.auto.AutoTrader: buy / sell / close / close_all run the
                  flows of the same name. Positions are tracked by YONEXIS (web terminals report nothing back).
  * config.py   - mode, flows, secrets, symbol mapping and automation settings (app_settings).
"""
