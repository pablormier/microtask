# Agent triage demo for the README: real Claude Code turns three raw captures
# into clear tasks with the mt-triage skill. Render from the repository root:
#   vhs docs/demo/triage.tape
# Requires VHS (brew install vhs) and a signed-in Claude Code (claude). The
# workspace is a throwaway copy in /tmp/mt-demo/todo.
#
# Claude's answers differ between runs. Hidden stretches skip the time Claude
# spends working, so the GIF shows each prompt and its result.

Output docs/images/triage.gif

Require rsync
Require claude

Set Shell "bash"
Set FontFamily "JetBrainsMono Nerd Font Mono"
Set FontSize 20
Set LineHeight 1.1
Set Width 1300
Set Height 800
Set Padding 32
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set WindowBar Colorful
Set WindowBarSize 48
Set BorderRadius 14
Set Margin 0
Set Framerate 15
Set PlaybackSpeed 1.0

Hide
Type "source docs/demo/triage-setup.sh"
Enter
Sleep 1s
Show

Type "# 1. Quick captures, typed in a hurry"
Enter
Type "mt list"
Sleep 300ms
Enter
Sleep 3s

Hide
Type "clear"
Enter
Show
Type "# 2. Ask Claude to triage them"
Enter
Type "mt run claude"
Sleep 300ms
Enter

# Accept Claude's one-time "trust this folder" prompt off camera. On later
# runs the prompt is gone, and Down and Enter on the empty input do nothing.
Hide
Wait+Screen@60s /Quick safety check|Try "/
Sleep 1s
Down
Enter
Wait+Screen@60s /Try "/
Sleep 1s
Show
Sleep 500ms

Type "Triage my inbox with the mt-triage skill."
Sleep 400ms
Enter
Sleep 3s

# Skip the time Claude spends working. While it works, the footer ends with
# "esc to interrupt"; when it is done, the footer ends after "to cycle)".
Hide
Wait+Screen@60s /esc to interrupt/
Wait+Screen@300s /to cycle\)( · ← for agents)?\s*$/
Sleep 1s
Show
Sleep 7s

Type "Apply them."
Sleep 300ms
Enter
Sleep 2s

Hide
Wait+Screen@60s /esc to interrupt/
Wait+Screen@300s /to cycle\)( · ← for agents)?\s*$/
Sleep 1s
Show
Sleep 4s

Hide
Ctrl+C
Sleep 500ms
Ctrl+C
Sleep 2s
Type "clear"
Enter
Show
Type "# 3. Clear tasks, ready to work on"
Enter
Type "mt list"
Sleep 300ms
Enter
Sleep 4s
