# Terminal demo for the README. Render from the repository root with:
#   vhs docs/demo/demo.tape
# Requires VHS: https://github.com/charmbracelet/vhs (brew install vhs)
#
# Rendered at 2x and shown at half width in the README, so text stays sharp
# on high-density screens.

Output docs/images/demo.gif

Require rsync

Set Shell "bash"
Set FontFamily "JetBrainsMono Nerd Font Mono"
Set FontSize 30
Set LineHeight 1.1
Set Width 1440
Set Height 690
Set Padding 40
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set WindowBar Colorful
Set WindowBarSize 56
Set BorderRadius 16
Set Margin 0
Set Framerate 30
Set PlaybackSpeed 1.0

Hide
Type "source docs/demo/setup.sh"
Enter
Sleep 1s
Show

# Capture a task in one line
Type `mt add "Renew passport" --priority high --status active`
Sleep 400ms
Enter
Sleep 1.2s

# See what is on your plate
Type "mt list --status active"
Sleep 400ms
Enter
Sleep 2.5s

# Ask what to do next
Hide
Type "clear"
Enter
Show
Type "mt focus"
Sleep 400ms
Enter
Sleep 3.5s

# Work the task through its statuses, matching it by title
Hide
Type "clear"
Enter
Show
Type "mt move passport doing"
Sleep 300ms
Enter
Sleep 1s
Type `mt comment passport -m "Appointment booked for Nov 20"`
Sleep 300ms
Enter
Sleep 1s
Type "mt done passport"
Sleep 300ms
Enter
Sleep 2s

# Every card is a plain Markdown file
Hide
Type "clear"
Enter
Show
Type "head -n 8 cards/*renew-passport.md"
Sleep 400ms
Enter
Sleep 4s
