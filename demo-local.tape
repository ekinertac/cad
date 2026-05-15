# cad — VHS demo tape (everyday view: project picker → session picker)
#
# This is the "every day" flow — what most users will see 90% of the
# time — so this GIF arguably matters more for the launch than the
# `cad live` one. Pair it with demo.tape on the README and in posts.
#
# Run:    vhs demo-local.tape
# Output: demo-local.gif + demo-local.mp4
#
# Prereqs (same as demo.tape):
#   - Real sessions across several projects in your local agent storage,
#     ideally including at least one with a [live] indicator showing.
#   - The filter demo below types "rays" — swap that for a substring
#     that actually matches a project name you have locally, or delete
#     the filter block entirely if you'd rather keep it simple.
#
# Style is intentionally identical to demo.tape so the README feels
# coherent. If you tweak font/theme/size here, tweak it there too.

Output demo-local.gif
Output demo-local.mp4

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

# Start hidden so we can prep a clean shell at $HOME before the recording
# kicks in. cad auto-picks when launched inside a known project dir;
# launching from $HOME always shows the full project list, which is the
# frame we want to lead with.
Hide
Type "cd ~ && clear"
Enter
Sleep 400ms
Show

Sleep 500ms
Type "cad"
Sleep 600ms
Enter

# Hold on the project list — this is the moneyshot frame. Provider
# badges like `(6c+5o+1f)` and `[N live]` markers are the headline
# visual; viewers need a beat to actually scan them. 5s feels long
# while you're editing but reads as natural in playback.
Sleep 5s

# Type-to-filter — distinctive, and proves to viewers that this is a
# real interactive picker, not a static printout. Swap "rays" for a
# substring that matches one of your actual project names.

Down
Sleep 300ms
Down 
Sleep 200ms
Down
Sleep 500ms
Down

Type "/"
Sleep 400ms
Type "Glob"
Sleep 1s
Enter
Sleep 2s

Down
Sleep 300ms
Down 
Sleep 200ms
Down
Sleep 500ms
Down

Type "/"
Sleep 400ms
Type "servo"
Sleep 1s
Enter
Sleep 5s

