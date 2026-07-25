# "A week with ccvitals in 30 seconds" — simulated readings, real pipeline.
# Render (from repo root):
#   python3 videos/first-signal-prep.py .first-signal && vhs videos/first-signal.tape
Output videos/first-signal.gif

Set Shell "zsh"
Set FontSize 16
Set Width 1500
Set Height 640
Set Padding 20
Set Theme "Catppuccin Mocha"

Hide
Type "clear && sh .first-signal/play.sh" Enter
Show
Sleep 31s
