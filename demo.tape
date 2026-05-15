# cad live — VHS demo tape
#
# Produces a GIF + MP4 for the README, Show HN post, and launch tweet.
# Run:    vhs demo.tape          (needs `brew install vhs`)
# Output: demo.gif (~3-4 MB) and demo.mp4 (for X embeds and the blog post).
#
# Prereq for a believable capture:
#   - Have 3+ claude sessions actually running across 3+ projects, in a
#     mix of [working] / [input] / [idle] states. VHS records real cad
#     output — it can't fake state. Set the env up first, then record.
#   - Easiest setup: open 4 iTerm tabs, `cd` into 4 different repos, run
#     `claude` in each. Type a long prompt in one (working), submit and
#     wait in another (input), leave one stale 5+ min (idle).
#
# Tweak knobs below if the GIF feels too fast/slow or too tall/wide for
# the platform you're posting to. README likes square-ish; X likes wider.

Output demo.gif
Output demo.mp4

# Visual styling — Catppuccin Mocha reads well in dark-mode timelines
# and is a built-in VHS theme (no extra config needed).
Set Shell      "zsh"
Set FontFamily "JetBrains Mono"
Set FontSize   16
Set Width      1200
Set Height     720
Set Padding    24
Set Margin     16
Set MarginFill "#1e1e2e"
Set Theme      "Catppuccin Mocha"
Set TypingSpeed 90ms
Set CursorBlink false
Set Framerate  50

# Tiny pause so the GIF's first frame isn't mid-keystroke.
Sleep 600ms

# Show the command — viewers learn what to type, *then* see the payoff.
Type "cad live"
Sleep 800ms
Enter

# Let the dashboard render. The 2s auto-refresh tick should fire at least
# once inside this window, so any [working] → [input] state transitions
# show up on screen without manual nudging.
Sleep 4s

# Move the highlight down a few rows. Proves it's a real interactive
# picker, not a static printout — important framing for a TUI launch.
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 800ms

# Hold on the populated dashboard. For an autoplay-looping GIF (X,
# README), the final frame is what people stare at. Ending on the
# value prop (full dashboard) > ending on a blank shell.
Sleep 3s

# Leave the quit commented out — uncomment if you want the loop to end
# back at the prompt instead.
# Type "q"
