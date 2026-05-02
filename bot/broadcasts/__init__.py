"""
bot.broadcasts
~~~~~~~~~~~~~~
Mass-mailing engine: audience targeting, rate-limited sending,
A/B testing, scheduling, click tracking.

Submodules:
  - sender    : platform-specific send helpers + audience materialization
  - scheduler : background async loop that picks scheduled broadcasts
                and processes the per-recipient queue
"""
