# Publishing

This script is hosted on GitHub.

It publishes calendars on GitHub Pages. To support that, it has a script to produce a HTML page with links to all calendars and a script to produce a debug page, in case a scraper fails.

To support that, it has 2 scripts:
- .github/workflows/update_ics.yml
    - runs main.py
    - triggers every Monday 00:00 local time
- .github/workflows/run_update_ics_on_main.yml
    - runs update_ics.yml
    - triggers whenever changes are commited to the main branch