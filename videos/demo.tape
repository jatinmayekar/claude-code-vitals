# Render (from repo root):
#   python3 videos/demo-seed.py .demo-home && vhs videos/demo.tape
# Produces videos/demo.gif from deterministic seeded data (no real usage / network).
Output videos/demo.gif

Set Shell "zsh"
Set FontSize 16
Set Width 1100
Set Height 420
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 32ms

Hide
Type "export HOME=$PWD/.demo-home" Enter
Type "setopt interactive_comments" Enter
Type "clear" Enter
Show

Sleep 700ms
Type "# claude-code-vitals — know your limits before they know you" Enter
Sleep 1200ms
Type "ccvitals suggest" Enter
Sleep 5500ms
Type "clear && ccvitals budget" Enter
Sleep 5500ms
